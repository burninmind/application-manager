# CLAUDE.md

Operational notes for Claude Code working in this repo. End-user docs live in `README.md` — keep them in sync when behavior changes.

## What this is

A single-user Gradio desktop tool. Pipeline: paste a job description → translate it → generate a tailored resume (removal-only) → draft a cover letter → compile both with Tectonic → store everything in SQLite FTS5. Local-only; no auth, no multi-user assumptions.

## Stack

- Python 3.11+, Gradio UI, Anthropic Python SDK
- Tectonic for LaTeX → PDF (single binary, downloads packages on first use)
- SQLite with FTS5 virtual table for search
- `python-dotenv` for `.env` loading, `langdetect` for translate-or-passthrough decision

## Layout

```
app/
  main.py        Gradio UI + step orchestration
  llm.py         Claude API wrappers, one function per pipeline step
  latex.py       Tectonic subprocess wrapper
  storage.py     SQLite schema, FTS5 triggers, save/search/delete
prompts/*.md     System prompts (one per LLM step) — edit these, not code
templates/       LaTeX letter template with <<COMPANY>> and <<BODY>> placeholders
resume.tex       Canonical "current resume" at repo root, replaced via UI
applications/    Per-application output folders (slugged date_company_vacancy)
app.db           SQLite DB, created on first run
```

## Pipeline architecture (most load-bearing detail)

The tailor step is **removal-only by construction**. The LLM does NOT emit a rewritten resume; it emits a JSON list of proposed removals, where each `removed_text` is a **verbatim substring** of the original resume. Python then string-replaces each accepted removal locally — no second LLM call when the user toggles checkboxes.

Why this matters for changes:

- Treat the resume as the candidate's full pool of experience. Any change that lets the model *add*, *rewrite*, or *reorder* content is a regression. Don't relax the prompt rules in `prompts/tailor_resume.md`.
- `_apply_removals` in [app/main.py](app/main.py) does literal `str.replace(snippet, "", 1)`. If `removed_text` isn't byte-identical to a slice of the source, the removal silently fails and a warning shows up in `omissions_md`. Don't "fix" this by fuzzy-matching — that breaks the determinism guarantee.
- The UI has two state objects: `removals_state` (the model's proposals) and `original_resume_state` (the loaded source). The dataframe is the user's selection layer. *Do not* derive the tailored resume from the editable textbox — it must come from `original_resume_state + accepted removals` so the rebuild is reproducible.

## JSON-with-LaTeX repair

`_repair_latex_json` in [app/llm.py](app/llm.py) exists because models often emit `\item`, `\hfill`, etc. as single backslashes inside JSON strings, which is invalid JSON. The function rewrites any `\X` where X is not in `"\\/bfnrtu` into `\\X` before `json.loads`. Don't remove this; the prompt asks for proper escaping but models are inconsistent.

When debugging tailor failures, the path is:
1. Raw model output → `_extract_fenced(raw, "json")`
2. `json.loads(...)` — if it raises, try again on `_repair_latex_json(...)`
3. Normalize each item to `{id, section, reason, removed_text}` with string coercion

## LaTeX engine notes

Tectonic invocation is `tectonic -X compile --outdir ... file.tex` (v2 CLI). Defaults to pdfLaTeX.

Preambles in this repo use `[utf8]{inputenc}` + `[T1]{fontenc}` + **`lmodern`**. The `lmodern` line is non-negotiable: without it, T1 font slots for Romanian/Vietnamese/etc. glyphs (e.g. Ţ U+0162, ă U+0103) render as empty space because Tectonic's default Computer Modern bitmaps don't cover them. If you add a new `.tex` template or modify a preamble, keep `\usepackage{lmodern}` after `fontenc`.

Tailor and letter prompts both forbid em dashes (—). The constraint exists because they survived from earlier model output in a way the user disliked. Don't drop the rule when editing prompts.

## Storage

`storage.connect()` is idempotent — it runs `SCHEMA` every time, so `CREATE ... IF NOT EXISTS` is required for everything. The FTS5 table is `content='applications'` (external-content), kept in sync via `AFTER INSERT` and `AFTER DELETE` triggers. If you add a searchable column, update both the table and both triggers.

`delete_application` only `shutil.rmtree`s a folder when it resolves inside `applications/`. Don't loosen that check — hand-edited rows could point anywhere.

`application_folder` builds `YYYY-MM-DD_company-slug_vacancy-slug`. Two applications to the same company/vacancy on the same day will collide on disk; the second write overwrites the first. Acceptable for now (it's a single-user app), but if you change the slug shape, check `_finalize` in [app/main.py](app/main.py) for assumptions.

## Model selection

Per-step model is read from env at import time:

- `CLAUDE_MODEL_TRANSLATE` → Haiku 4.5
- `CLAUDE_MODEL_TAILOR` → Sonnet 4.6 (this is the judgment-heavy step; bump to Opus if quality matters more than cost)
- `CLAUDE_MODEL_LETTER` → Sonnet 4.6
- `CLAUDE_MODEL_METADATA` → Haiku 4.5

Don't hardcode model IDs in new code paths — read them from `app.llm` module-level constants so `.env` overrides keep working. The tailor user message has `cache_control: ephemeral` so repeated regenerations reuse the prompt cache.

## Adding pipeline steps

If you add a new LLM step:
1. Drop the system prompt in `prompts/<name>.md` and load it with `_load_prompt`
2. Add a `MODEL_<NAME>` constant pulling from env
3. Wrap the call with `_call(...)`, which handles 429/529 retry with backoff
4. If the output contains LaTeX inside JSON, run it through `_repair_latex_json` before parsing

## UI quirks worth knowing

- `gr.Dataframe(type="array")` — keep this. Pandas-mode passes a DataFrame to handlers, which would break `_accepted_ids_from_rows`.
- The "Apply changes" button (not a `.change` event) drives local rebuilds. Earlier we tried `removals_df.change` but it was unreliable across Gradio versions; if you ever revert, test boolean-cell toggling specifically.
- The "Start new application" button must clear `removals_state`, `removals_df`, and all letter/resume textboxes — see `_reset_application`. Adding new state? Add it to the reset tuple too.

## Things that look like bugs but aren't

- `langdetect` flagging short English text as another language → seed is fixed (`DetectorFactory.seed = 0`), behavior is intentional. The user can edit the translation output anyway.
- `meta.json` is written redundantly with the DB row → both are sources of truth for different consumers (disk browsing vs. search). Don't dedupe.
- Compiled application folders under `applications/` are checked into git → intentional, this is the user's portfolio of past applications.

## Don'ts

- Don't add em dashes anywhere in prompts or LaTeX you generate.
- Don't replace the verbatim-substring removal mechanism with fuzzy matching or LLM-driven re-application.
- Don't loosen the "remove only" rule in `prompts/tailor_resume.md`.
- Don't drop `\usepackage{lmodern}` from any `.tex` preamble.
- Don't introduce a second compile pass — Tectonic handles all passes internally.
