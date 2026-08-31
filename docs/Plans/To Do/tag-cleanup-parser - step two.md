# Handoff: Tag-Cleanup Parser — Phase 3 (name-block-first detection)

## Task

Continue work on `hydrus_tag_splitter.py` (you wrote it). It parses tags bulk-imported
from filenames in a personal photo organizer (Hydrus), splits each concatenated filename
string into clean individual tags, and writes them back via the Client API. Phases 1-2
shipped: config-driven CLI (`--preview`/`--dry-run`/`--apply`), wildcard targeting,
file-domain restriction, threaded Hydrus write layer, and fixes for phrase-merge,
reserved-standalone tokens, age-grouping, truncation, and attribute-stacking.

**Phase 3 goal:** rework name detection. The current detector runs *after* glue-dropping
and phrase assembly, and only catches rare tokens — so common names are missed, and the
surname gets eaten by the truncation rule. The fix is to **find the name block FIRST, as
the very first step of parsing, before any other token handling**, and emit the name as
a **plain tag** (no `character:` namespace).

Treat all examples as data-shape fixtures for a generic string-parsing task. Keep the
tool content-agnostic. The sample vocabulary below is neutral portrait/landscape language
to demonstrate the grammatical patterns; names shown are real name-shape fixtures.

## New pipeline order (hard requirement)

Name detection moves to position 1, before everything else. The name block is carved out
("claimed") and protected for the rest of the run.

```text

1. DETECT NAME BLOCK  (first, before anything else)  &lt;- NEW
2. carve it out as a protected region, emit as ONE plain tag
3. strip namespace -&gt; split remaining blocks -&gt; strip number prefix
4. drop function/glue words
5. reserved-standalone split (e.g. &quot;teen&quot;)
6. assemble attribute phrases (stacking + compound detection)
7. route remaining namespaces
8. truncation drop (dictionary-membership + length rules)
```

Once a name block is detected and confirmed, parsing continues normally on the remainder.
The truncation rule (step 8) MUST be aware of the protected name region and never drop a
token inside it.

## How to detect the name block

The name sits at a predictable position and shape:

- **Position:** in the tag string, it appears right after the leading attributes (or
  directly after the primary-keyword block) and immediately before the descriptive
  verb phrase. In files where there is no verb, it is simply the descriptor region
  after the attributes.
- **Shape:** **two words** (given + family) is the most common form. A single word is
  also valid. Either the given name, the family name, or both may be reduced to a
  **single letter initial** (e.g. `anna r`, `milana k`).
- **Separators:** comma (` ,`) and ampersand (`&`) delimit **separate** name blocks.
  e.g. `chloe lacourt, vanessa staylon` => two distinct names
  `lolli moon & jennifer clark`      => two distinct names
- **Trailing marker:** a trailing `(N)`/`-N` suffix on a word is a filename artifact and
  must be stripped cleanly so it does not corrupt the name
  (e.g. `sabrina fox(1)` must resolve to name `sabrina fox`, not `fox1`).

### Detection signals (in priority order)

1. **Corpus-global positional inference (primary).** Aggregate across ALL matched tags
   (126k+ entries). A token that repeatedly occupies the "attribute-adjacent,
   verb-preceding" slot is very likely a name component, EVEN IF it is a common word
   (`naomi`, `jennifer`, `sabrina`, `zoey`, `chloe`, `madison`). This is the only signal
   that catches common names, and wordfreq cannot.
2. **Known-model list (secondary).** A config file of known names; directory-overlay /
   folder-name matching is a bonus.
3. **wordfreq rarity (tertiary).** Only as a supporting signal, never the primary one.
4. **Single-letter-initial rule.** A 1-letter token immediately after a candidate given
   name is an initial and belongs to the same name block (`anna r`, `milana k`).

A name is "confirmed" when it satisfies a combination of (1) positional evidence and (2)
the 1-2 word shape, or (3) it appears in the known list or is a strong rarity outlier.

## Emit the name as a plain tag (no namespace)

This is a behavior change from phase 2. **Do not** prefix with `character:` or any other
namespace. The name is added as a normal, unqualified tag:

- `paige owens`   -> tag `paige owens`   (one tag, space preserved)
- `scarlett pain` -> tag `scarlett pain`
- `naomi woods`   -> tag `naomi woods`
- `chloe lacourt` / `vanessa staylon` -> TWO separate plain tags
- `lolli moon` / `jennifer clark`      -> TWO separate plain tags
- `anna r`        -> tag `anna r` (initial absorbed, NOT dropped)
- `sabrina fox`   -> tag `sabrina fox` (trailing `(1)` stripped, NOT `fox1`)

## What this fixes (from the 25-run preview)

- Previously emitted as separate bare words: `paige`+`owens`, `scarlett`+`pain`,
  `sunny`+`leone`, `naomi`+`woods`, `jennifer`+`clark`, `madison`+`mia`, `alli`+`rae`.
  All must now produce ONE plain tag each.
- Previously the surname was dropped as "truncation": `staylon` (from
  `vanessa staylon`), `r` (from `anna r`), `d` (from `zuzana d`), `k` (from `milana k`).
  All must now be preserved inside their name block.
- Previously created a bogus tag: `fox1` from `sabrina fox(1)`. Must now be `sabrina fox`.
- Previously emitted only one member of a multi-name list (e.g. `chloe`+`lacourt`+`vanessa`
  with `staylon` dropped). The comma/`&` list must now yield complete separate names.

## Truncation rule changes (needed to stop eating names)

1. **Name-region protection:** tokens inside a confirmed name block are never
   truncation-dropped.
2. **≤2-char final-token rule:** a final token of length ≤2 that is NOT inside a
   protected name region -> drop as truncation (fixes leaked fragments like `st`, `mo`,
   `po`, `wa` that dictionary-membership glosses over because they are abbreviations).
   Exception: if the ≤2-char token forms a valid single-letter-initial name tail, it is
   protected.

## Acceptance criteria

Re-run over the 25-sample golden fixture set and produce an `IN -> OUT` preview table.
Confirm:

1. Name detection runs as the FIRST pass (verified in code ordering).
2. Every instance of a multi-word name produces ONE plain tag (`paige owens`,
   `scarlett pain`, `naomi woods`, `jennifer clark`, etc.) — not split tokens.
3. Multi-name lists split on `,` and `&` into separate plain tags.
4. Single-letter initials are absorbed and preserved (`anna r`, `milana k`), never dropped.
5. Trailing `(N)`/`-N` artifacts are stripped cleanly (`sabrina fox`, not `fox1`).
6. No `character:` namespace is emitted — names are plain unqualified tags.
7. Truncation never removes a token inside a protected name region; ≤2-char non-name
   final tokens are dropped.

Return the updated script plus the preview table before `--apply`.

## Deliverables

- Updated `hydrus_tag_splitter.py` with the name-block-first pipeline.
- Corpus-global positional name pass implemented (primary signal).
- Name result = plain unqualified single tag (space-joined).
- Comma/`&` multi-name splitting; single-letter initial absorption.
- Truncation name-region protection + ≤2-char final-token rule.
- The 25-entry `IN -> OUT` regression preview.
