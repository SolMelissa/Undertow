# Handoff: Tag-Cleanup Parser — Phase 2 (fix phrase-segmentation & name-detection)

## Task

Continue work on `hydrus_tag_splitter.py` (you wrote the original). It parses tags
bulk-imported from filenames in a personal photo-library organizer (Hydrus), splits
each concatenated filename string into clean individual descriptive tags, and writes
them back via the Client API. Phase 1 shipped a working shell: config-driven behavior,
`--preview` / `--dry-run` / `--apply` CLI, wildcard targeting, file-domain restriction,
and a threaded Hydrus write layer.

**Phase 2 goal:** fix the phrase-segmentation and name-extraction bugs revealed by a
25-sample preview run. There are four root causes that must all be resolved. We have 25
labeled example runs as a golden test set; regression-test against them after each change.

Treat all examples as **data-shape fixtures for parser development**. This is a generic
string-parsing task; keep the tool content-agnostic. The example vocabulary below is
neutral photography-subject language (landscape/portrait/objects) purely to demonstrate
the grammatical patterns.

## Context: how the parser currently works

- Input tags look like `NAME:NN-keywords - description words...`. The source namespace
  (`NAME:`) is configurable and discarded. The first block starts with a useless
  number+hyphen (`NN-`) that's stripped deterministically.
- After prefix stripping, each word is one of three things: **glue** (drop),
  **content tag** (keep, possibly as a phrase), or a **name** (should become a
  `character:` tag). A truncated final token sometimes needs dropping too.
- **Core policy: dropping is high-precision.** Only delete what we can prove is glue
  or garbage; keep everything uncertain. Prevents data loss.
- The current pipeline is: strip namespace -> split blocks -> strip number prefix ->
  tokenize/camel-case -> **merge attribute phrases** -> route namespaces ->
  truncation drop.
- Names were supposed to be detected via the `wordfreq` library (Zipf-frequency outlier
  heuristic) but that was left as an *optional* soft-dependency and was never actually
  wired in. The only name logic today is uppercase detection (`title_case_run`), which
  does nothing because all source names are lowercase.

## Root cause #1 — Name detection does not exist in the runnable code

**Evidence (25-run preview):** zero `character:` tags emitted. Model/person names like
`linda`, `flora`, `emma fantazy`, `sara jaymes`, `piper perri` were emitted as plain
words (or swallowed by merges — see root cause #2). No frequency-based detector is active.

**Requirements for the fix:**
- Make `wordfreq` a **required** dependency (drop the soft-dep fallback for the detector).
- Design for the reality shown in the data: **many names are also common English words**
  (`emma`, `linda`, `madison` have normal wordfreq scores), so a naive
  "low-frequency -> name" rule will miss them. Add a **corpus-global inference** pass:
  aggregate across *all* matched tags (tens of thousands). A token that repeatedly
  appears in the "attribute-adjacent, verb-preceding" slot is likely a name, even if
  it's a common word.
- **Positional slot model:** the observed name pattern is a 2-token run sitting right
  after the leading attributes and almost always followed by a verb
  (e.g. `<attributes> <Name> <Name> <verb> ...`). Codify this as a primary positional signal.

## Root cause #2 — Pipeline ordering: phrase-merge runs before name detection, so both break

**Evidence:** phrasal merges fused names onto leading attributes before name detection
could isolate them (e.g. an attribute `young` gluing a following name onto it, producing
an incorrect merged token).

**Requirement:** reorder the pipeline so name detection runs **before** phrase assembly,
and let names **claim back** tokens that phrase-assembly has already bound. New fixed order:

```text
strip ns -&gt; split blocks -&gt; strip number prefix -&gt;

1. drop function/glue words
2. detect names (positional + corpus-global + wordfreq)   &lt;- moved up
3. reserved-standalone split (see root cause #3)
4. assemble attribute phrases (with stacking + compound detection, see #4)
5. route namespaces
6. truncation drop (dictionary-membership based)
```

## Root cause #3 — Reserved atomic tokens are being merged

**Evidence:** certain high-frequency standalone descriptors (notably `teen`) live in the
attribute lexicon, so they glue to whatever follows (e.g. producing fused tokens instead
of keeping `teen` separate). They should **always emit as their own tag**.

**Requirement:** add a **reserved-standalone set** (`always_split`). On detection, a token
in this set:
- is **removed from the attribute lexicon** (so the merge path can't touch it),
- emits as its own tag,
- **never merges** with a following or preceding token,
- and marks the phrase boundary so `teen first timer` splits at `teen`.

Keep it separate from ordinary attributes (which still merge fine).

## Root cause #4 — Truncation detection is a weak length rule, not a word-knowledge rule

**Evidence — what got dropped vs. kept as final tokens:**

- Dropped (correct): single-character tokens `b`, `i`, `c`, `p`, `s`, `&`.
- Kept (**wrong**): multi-character fragments like `black sto`, `pos`, `swe`,
  `librar`, `wa` — these are clipped ends of longer words (e.g. `sto` -> "stone",
  `librar` -> "library", `swe` -> "sweater"). A pure length rule can't catch these.

The current rule is effectively "drop the final token only if it's a single character,"
which is why it fails on real multi-character truncations.

**Requirement:** replace the length heuristic with a **dictionary-membership rule**. If
the final token of the description block is not a recognized English word per `wordfreq`
(zero/near-zero Zipf score), drop it as truncated. This catches every mis-kept fragment in
one principle and is far more robust than any length cutoff (which can't distinguish
`librar` from `library`). Keep this as a config toggle as before, surfaced in preview.

## Additional fixes confirming the four root causes

These failures trace to root cause #3 (the attribute-merge heuristic is both over- and
under-approximating). Fixing ordering + reserved set + stacking resolves them. Neutral
equivalents of the failed patterns:

| Sample pattern (neutralized) | Wrong current output | Correct output | Failure type |
|---|---|---|---|
| `18 year old redhead` | `18` `year` `old redhead` | `18 year old` `redhead` | age unit not grouped |
| `teen first timer` | `teen first` `timer` | `teen` `first timer` | reserved token didn't split; compound not detected |
| `massive black boulder` | `massive` `black boulder` | `massive black boulder` | no attribute stacking |

(The original corpus exhibited the same three failure types; the neutral words above are
placeholders preserving the exact grammatical structure.)

**Requirements that address these:**
- **Age-unit pattern:** add a specific rule so `\d+ year(s) old` groups into a single
  token `18 year old`, and the following adjective continues independently.
- **Compound-noun detection:** two content nouns where the second is a strong noun should
  group (`first timer`) so it survives and an intervening reserved token doesn't fuse with it.
- **Attribute stacking:** accumulate consecutive attributes (`massive black`) before the
  noun (`boulder`) -> `massive black boulder`, rather than stopping at one adjective+noun.

Structural conclusion to carry forward: a single "leading adjective in a fixed list ->
glue to next noun" rule cannot carry phrase segmentation on its own. It over-merges
(reserved words) and under-merges (attribute stacks) in the same run. The fix is to scope
the lexicon to what it's reliable at (single color/size + noun) and let **names +
reserved-set + age pattern + compound detection** handle the rest.

## Out of scope for this phase

- **Do not** add logic for filtering "garbage"/directory-segregation tags. The user
  explicitly filters those later themselves and does not want them handled here. No
  numeric-tag dropping, no source-directory ignore-lists.
- The Hydrus HTTP layer (search / metadata / add+delete with integer actions,
  service-key resolution, threaded apply, retry-with-backoff) is confirmed correct
  against the docs — leave it untouched.

## Acceptance criteria

Re-run the parser over the 25-sample golden fixture set and produce an `IN -> OUT`
preview table. Confirm these regressions no longer occur:

1. At least some names resolve to `character:` tags (previously: zero emitted).
2. `teen` always emits as its own tag, never merged.
3. `18 year old` groups as one token; the following adjective continues independently.
4. `first timer` survives as a compound.
5. `massive black boulder` stacks into one tag.
6. Truncated final tokens (`sto`, `pos`, `swe`, `librar`, `wa`) are dropped via
   dictionary-membership.
7. Pipeline order is verified in code: name-detection before phrase-assembly,
   reserved-set split before merge.

Return the updated script plus the preview table so we can eyeball remaining edge cases
before running `--apply`.

## Deliverables

- Updated `hydrus_tag_splitter.py` with all four root causes fixed.
- `wordfreq` promoted to a required dependency (add to requirements/install notes).
- New config keys: `always_split` set, age-pattern on/off, dictionary-truncation on/off,
  corpus-global name pass on/off, attribute-stacking on/off. Keep all existing keys.
- The 25-entry `IN -> OUT` regression preview.
