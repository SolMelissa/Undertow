# Split Tag Cleanup X into four modules

## Context

`undertow/scripts/Tag Cleanup X/tag_cleanup_x.py` is a single 1,732-line file that mixes four
unrelated concerns: text parsing, Hydrus API calls, terminal/HTML rendering, and the interactive
wizard. It has grown to the point where a change to one concern means reading all of it. This
splits it into four focused modules (plus a self-test module), drops the HTML report entirely, and
makes the renderer mirror all console output to a log file with a 5-second progress cadence.

The script was recently moved from `undertow/scripts/` into the `Tag Cleanup X/` subfolder, and
that move silently broke two things that must be fixed as part of this work (see Task 0). It is
**deliberately hidden from the dashboard's Scripts tab while under development** — the subfolder is
invisible to `scripts_runner.list_scripts()`, which globs `scripts/*.py` non-recursively. Do not
add a shim or touch `scripts_runner.py`; the user will re-activate it later. (The stale
`"tag_cleanup_x"` entry in `SCRIPT_META` is harmless — `meta_for()` is only consulted for names
that discovery already returned.)

Everything lives in `undertow/scripts/Tag Cleanup X/`. All paths below are relative to that folder
unless stated otherwise.

**Critical constraint:** `undertow/scripts/tag_cleanup.py` is FROZEN. Never edit it. This work
touches only the `Tag Cleanup X/` folder plus `undertow/__init__.py` and `CHANGELOG.md`.

## Target layout

| File | Contents | ~lines |
|---|---|---|
| `tag_cleanup_x.py` | Wizard + entry point. Owns `main()`, argparse, the saved-settings JSON, the `wizard_*` prompts, and `run_dry_run_then_apply`. | ~330 |
| `tag_cleanup_x_engine.py` | Text engine. Tags + `Config` in → deduped ordered tags out. No network, no `input()`, no `rich`. | ~560 |
| `tag_cleanup_x_hydrus.py` | `HydrusClient` only. | ~130 |
| `tag_cleanup_x_render.py` | `Renderer` (console+log tee), progress reporting, exploded-view rendering, all user prompts. | ~260 |
| `tag_cleanup_x_selftest.py` | `FIXTURES` + `run_self_test`. | ~150 |
| `tag_cleanup_x_lists.py` | Unchanged (except the `parents[3]` fix in Task 0). | 115 |

Sibling imports work with no boilerplate: Python puts a script's own directory on `sys.path[0]`,
so `tag_cleanup_x.py` and its siblings can `import tag_cleanup_x_engine` directly. Do **not** add
`sys.path` manipulation for sibling imports. (The existing `sys.path.insert(...parents[N]...)`
calls for `from undertow import config` stay — those reach outside the folder.)

---

## Task 0 — Fix the two move-induced breakages first

Do these before splitting, so you have a working baseline to verify against.

1. **`tag_cleanup_x.py` line 82-86**: the fallback `import tag_cleanup_lists` raises
   `ModuleNotFoundError` — the folder only contains `tag_cleanup_x_lists.py`. Replace the whole
   try/except block with a plain `import tag_cleanup_x_lists as tag_cleanup_lists`, and update the
   six `tag_cleanup_lists.load_lists()` references in `Config` accordingly (keeping the alias means
   they need no edit).
2. **`tag_cleanup_x.py` line 287**: `JSON_OUTPUT_DIR = Path(__file__).resolve().parent / "output" / "json"`
   now points inside `Tag Cleanup X/`, where nothing exists. The real 6.8 MB gazetteer cache that
   `performer_gazetteer.py` writes lives at `undertow/scripts/output/json/performer-gazetteer.json`.
   Change to `Path(__file__).resolve().parent.parent / "output" / "json"`.
3. **`parents[2]` → `parents[3]`** in `tag_cleanup_x.py::_config_dir` (line 98) and
   `tag_cleanup_x_lists.py::_default_lists_config_path` (line 29). One extra directory level was
   added by the move, so `from undertow import config` silently fails and falls through to the
   `USERPROFILE` fallback. The fallback happens to resolve to the same path, so this is a latent
   bug, not a live one — fix it anyway.

Verify: `python "undertow/scripts/Tag Cleanup X/tag_cleanup_x.py" --self-test` runs and prints all
regression checks as `[OK]`. **Save this output** — it is the before/after baseline for the split.

---

## Task 1 — `tag_cleanup_x_engine.py`

Move these verbatim from `tag_cleanup_x.py`, in this order. No logic changes except where noted.

- The `wordfreq` import guard (lines 69-73) and `import tag_cleanup_x_lists as tag_cleanup_lists`
- `normalize_token`, `CASE_BOUNDARIES`, `split_camel_case` (142-152, 227-230)
- `Config` (155-219) — keep every field and every comment
- `NUMBER_PREFIX_RE`, `RESOLUTION_RE`, `AGE_UNIT_TOKENS` (222-224)
- `strip_number_prefix`, `looks_truncated_legacy`, `looks_truncated_dictionary` (233-262)
- Gazetteer section (287-404): `JSON_OUTPUT_DIR`, `PERFORMER_GAZETTEER_CACHE_FILE`,
  `PerformerGazetteer`, `load_performer_gazetteer`, `_extract_name_spans` — including the long
  block comment above `JSON_OUTPUT_DIR` explaining why a lone gazetteer hit is not enough
- `ParsedTag`, `FilePreview` (407-431)
- `TRAILING_MARKER_RE`, `strip_trailing_marker`, `_tokenize_block`, `_tokenize_raw_tag`,
  `_classify_token`, `_process_block`, `_should_skip_processing` (434-626)
- `parse_filename_tag_batch`, `parse_filename_tag` (629-684)

**One change:** give `parse_filename_tag_batch` an optional progress callback so the wizard can
show movement while parsing a 100k-tag library.

```python
def parse_filename_tag_batch(raw_tags: List[str], cfg: Config,
                             on_progress=None) -> List[ParsedTag]:
```
Inside the existing `for raw_tag in raw_tags:` loop, after appending to `results`, add:
```python
        if on_progress and len(results) % 512 == 0:
            on_progress(len(results), len(raw_tags))
```
and call `on_progress(len(results), len(raw_tags))` once more just before `return results`. Do not
change batching — the single whole-batch call is deliberate (see `_build_plan`'s docstring).

Public surface: `Config`, `ParsedTag`, `FilePreview`, `PerformerGazetteer`,
`parse_filename_tag_batch`, `parse_filename_tag`, `load_performer_gazetteer`,
`normalize_token`, `split_camel_case`.

Module docstring: state that this is the content-agnostic text engine — no network, no terminal,
no prompts — and that its only file reads are the editable word lists and the gazetteer cache.

---

## Task 2 — `tag_cleanup_x_hydrus.py`

Move the `requests` import guard (63-67) and the entire `HydrusClient` class (691-811) verbatim.
No logic changes. Keep `add_tags` / `add_tags_multi` split as-is, and keep both comments explaining
why `"all known files"` and `"all known tags"` are excluded from the service lists.

Callers still need `requests` for exception handling (`except requests.RequestException`), so
`tag_cleanup_x.py` keeps its own `import requests`.

---

## Task 3 — `tag_cleanup_x_render.py`

This is the only module with substantial new code. **Delete all HTML output** — `HTML_OUTPUT_DIR`,
`_HTML_STYLE`, `_HTML_KIND_CSS`, `_render_exploded_html`, `write_html_report` (lines 1013-1179),
and the now-unused `import html`. Existing `.html` files under `scripts/output/html/` are left
alone (the frozen `tag_cleanup.py` still writes there).

### 3a. `Renderer` — console + log tee

Every line printed to the terminal is also appended to a timestamped log file. Log path:
`undertow/scripts/output/logs/tag-cleanup-x-<YYYYmmdd-HHMMSS>.log` — i.e.
`Path(__file__).resolve().parent.parent / "output" / "logs"`, the same `output/` root that
`json/` and `html/` already use. `mkdir(parents=True, exist_ok=True)` on construction. Open the
file with `encoding="utf-8"` and **no BOM** (see the project's BOM gotcha in CLAUDE.md).

```python
class Renderer:
    def __init__(self, log_path: Path | None = None) -> None: ...
    def out(self, text: str = "", style: str | None = None) -> None
        # rich Console print + timestamped plain line to the log
    def out_rich(self, renderable, plain: str) -> None
        # styled renderable to console, `plain` to the log
    def log_only(self, text: str) -> None
        # log file only — used for the full exploded detail the console truncates
    def error(self, text: str) -> None
        # console via stderr + log, prefixed "ERROR: "
    def close(self) -> None
```
Log lines are prefixed `[HH:MM:SS] `. Flush after every write — the run can be killed from the
dashboard's Stop button, and a buffered log would lose everything.

### 3b. Prompts — `Renderer` methods

Move `prompt_text`, `prompt_secret`, `prompt_int`, `prompt_yes_no`, `prompt_choice`
(1334-1391) onto `Renderer` as `text`, `secret`, `integer`, `yes_no`, `choice`. Keep their bodies
and the `prompt_secret` non-printable-stripping comment intact. Two changes:
- The prompt label goes to the log before `input()` is called.
- The answer goes to the log after. **Never log the value returned by `secret()`** — log
  `(api key entered)` instead.

### 3c. Progress — 5-second cadence

Rename `ProgressPrinter` → `ProgressReporter` and change `min_interval` from `0.5` to `5.0`. Keep
the `\r`-overwritten status line on the console exactly as it is, but on each actual print also
append a plain (non-`\r`) copy to the log via `log_only`, so the log holds a readable progress
trail rather than one smeared line. Constructor takes the `Renderer` as its first argument.

### 3d. Exploded-view rendering

Move verbatim: `KIND_STYLES_RICH`, `KIND_PLAIN_LABEL`, `render_exploded_rich`,
`render_exploded_plain`, `_detected_names`, `_render_tag_section`, `_render_tag_section_plain`,
`PREVIEW_INLINE_LIMIT` (851-970). Replace the module-level `_console = Console()` and the bare
`_console.print` / `print` calls inside them with the `Renderer` passed in as a parameter.

Rewrite `print_preview_table(previews, renderer)` (973-1010) — the `log_path` parameter goes away,
since the renderer already owns the log:
- Console: first `PREVIEW_INLINE_LIMIT` (40) tag sections inline, else the first 10 plus a
  `... N more tag(s) not shown here ...` line. Unchanged behavior.
- Log: **always** the full `_render_tag_section_plain` output for every non-skipped tag, via
  `log_only`. This replaces the old "only written past the 40-tag threshold" behavior.
- Keep the existing summary line and the skipped-tag counting logic exactly as-is.

---

## Task 4 — `tag_cleanup_x.py` (wizard)

What stays, in this order: module docstring (updated — see below), `import requests`, imports of
the three sibling modules, `_config_dir` / `_default_local_config_path` / `LOCAL_CONFIG_FILE` /
`LEGACY_CONFIG_FILE` / `load_local_config` / `save_local_config` (96-135), `build_arg_parser`
(1317-1327), `wizard_connect` (1398-1444), `ServiceSelection` + `wizard_pick_services`
(1447-1489), `wizard_build_config` (1492-1515), `load_performer_gazetteer_for_run` (1518-1529),
`DRY_RUN_SAMPLE_SIZE`, `_build_plan`, `_chunked` (1532-1560), `run_dry_run_then_apply`
(1563-1700), `main` (1703-1724), the `__main__` guard (1727-1732).

Wiring changes:
- `main()` constructs one `Renderer` and passes it down to every wizard function,
  `print_preview_table`, `run_self_test`, and `ProgressReporter`. Close it in a `finally` so the
  log is complete even on `KeyboardInterrupt`.
- Every bare `print(...)` / `print(..., file=sys.stderr)` becomes `renderer.out(...)` /
  `renderer.error(...)`.
- Every `prompt_yes_no(...)` etc. becomes `renderer.yes_no(...)`.
- Delete both `write_html_report(...)` calls and the two `print(f"HTML report written to: ...")`
  lines in `run_dry_run_then_apply` (1597-1602, and the dry-run one).
- Delete the `log_path = LOCAL_CONFIG_FILE.parent / "tag-cleanup-x-last-preview.txt"` line (1631)
  and pass the renderer to `print_preview_table` instead.
- In `_build_plan`, thread a progress callback into `parse_filename_tag_batch` and give
  `_build_plan` a `renderer` parameter so the full-library parse shows a `ProgressReporter`
  ("Parsing tags"). The 25-file dry-run sample is fast enough to skip progress — pass `None`.
- `main()`'s `--self-test` branch calls `tag_cleanup_x_selftest.run_self_test(Config(), renderer)`.
- `datetime` is only needed by the renderer now; drop it from this module's imports along with
  `html`. Keep `random`, `sys`, `argparse`, `json`, `os`, `time` (check each is still used and
  drop any that isn't).

Update the module docstring: keep the "Started life as a verbatim copy of the frozen
tag_cleanup.py" paragraph and the usage description, replace the HTML-report sentence with the
new mirrored-log behavior, and add a short paragraph naming the four modules and what each owns.

---

## Task 5 — `tag_cleanup_x_selftest.py`

Move `FIXTURES` (1182-1216) and `run_self_test` (1219-1314) including both regression-check blocks
and the synthetic-gazetteer setup. Changes:
- Signature `run_self_test(cfg: engine.Config, renderer: Renderer) -> None`.
- Delete the `write_html_report` call and its `print` (1223-1227).
- All `print(...)` → `renderer.out(...)`.
- Keep every check string and expected value byte-identical — these are the refactor's only
  safety net.

---

## Verification

There is no pytest suite; verify by running the script.

1. **Offline regression gate (required):**
   `python "undertow/scripts/Tag Cleanup X/tag_cleanup_x.py" --self-test`
   Every line must read `[OK]` — 11 parser checks plus 7 performer-gazetteer checks. Diff this
   against the Task 0 baseline: it must be identical apart from the removed HTML-report line.
2. **Log file:** confirm a new `undertow/scripts/output/logs/tag-cleanup-x-<stamp>.log` exists,
   contains the same content the terminal showed, is UTF-8 with no BOM (`head -c 3` must not be
   `EF BB BF`), and that no `.html` report was written.
3. **Import isolation:** `python -c "import sys; sys.path.insert(0, r'undertow/scripts/Tag Cleanup X'); import tag_cleanup_x_engine"`
   must succeed without `requests` or `rich` being touched — the engine should import cleanly on
   its own.
4. **Live smoke test (ask the user before running):** launch with no arguments, walk the wizard to
   the dry-run sample preview, confirm the sample renders and the 5-second progress cadence looks
   right, then **answer "no"** at the "Proceed to run on the full library?" prompt. Do not write
   tags to Hydrus without explicit approval.

## Wrap-up

- Bump `undertow/__init__.py` `__version__` `1.14.25` → `1.14.26` and add a matching `## 1.14.26`
  section at the top of `CHANGELOG.md`, sized **Significant** (module split + HTML removal + the
  two move-induced breakage fixes).
- `git add` the whole `Tag Cleanup X/` folder — it is currently untracked, and
  `undertow/scripts/tag_cleanup_x.py` shows as deleted from the move. Commit and push to `master`.
  If the push fails, say so explicitly rather than reporting the task done.
- Leave `The Pipeline.code-workspace` untracked and out of the commit.
