# CLAUDE.md

Operational notes for Claude Code working in this repo. End-user docs live in `README.md`; keep them in sync when behavior changes.

## What this is

A single-user Gradio desktop tool. Pipeline: paste a job description → translate it → tailor the resume (generated short summary + confirmable removals) → draft a cover letter → compile both with Tectonic → store everything in SQLite FTS5 → track each application's outcome. Local-only; no auth, no multi-user assumptions. The repo is public and meant to be reusable by others, so it must not contain the owner's personal data.

## Stack

- Python 3.13+, Gradio UI, Anthropic Python SDK
- Tectonic for LaTeX → PDF (single binary, downloads packages on first use)
- SQLite with FTS5 virtual table for search
- `python-dotenv` for `.env` loading, `langdetect` for translate-or-passthrough decision

## Layout

```
app/
  main.py           Gradio UI + step orchestration + deterministic resume rebuild
  llm.py            Claude API wrappers, one function per pipeline step
  latex.py          Tectonic subprocess wrapper
  storage.py        SQLite schema, migration, FTS5 triggers, save/search/status/delete
prompts/*.md        System prompts (one per LLM step); edit these, not code
templates/letter.tex  Letter template with <<COMPANY>>, <<NAME>>, <<BODY>> placeholders
resume.tex          Example resume for a fictional person (committed)
resume.local.tex    The user's real resume (gitignored); preferred over resume.tex when present
applications/       Per-application output folders (gitignored)
app.db              SQLite DB, created on first run (gitignored)
```

## Personal data

- Never commit the user's resume, letters, applications, or `app.db`. Uploads go to `resume.local.tex`, never `resume.tex`.
- `resume.tex` must stay fictional and must keep exercising every convention the code relies on (summary markers, `filecontents*` publications, more projects and publications than the default limits, "For more ..." lines, `lmodern`).
- Older commits still contain the owner's resume; rewriting history is the owner's call, don't do it unprompted.

## Pipeline architecture (most load-bearing detail)

The tailor call returns JSON `{"summary": str, "removals": [...]}`.

- **Summary**: the only generated text in the resume. The source resume marks its long summary with `% BEGIN SUMMARY` / `% END SUMMARY` lines (`SUMMARY_RE` in [app/main.py](app/main.py)); `_replace_summary` swaps the block body for the short summary and keeps the markers. The summary is shown in an editable textbox. `llm.escape_latex_specials` escapes bare `&`, `%`, `#` because a stray `%` silently truncates a line.
- **Removals**: each `removed_text` is a **verbatim substring** of the source. Optional `context` is a longer verbatim substring that occurs exactly once and contains `removed_text`; it disambiguates repeated snippets such as a single skill. Categories: `bullet`, `skill`, `project`, `publication`, `section`, `other`.
- **Top-N**: the model keeps at most `RESUME_MAX_PROJECTS` projects and `RESUME_MAX_PUBLICATIONS` publications by removing the rest. Publications are `@entries` inside the preamble's `filecontents*` block; that is the only preamble content the model may remove. `_check_tailored` warns if too many publications remain or a "for more ..." line was removed.

`_build_tailored` = `_replace_summary` first, then `_apply_removals`. `_apply_removals` resolves every accepted removal to a span against the same base text, refuses anchors that occur zero or several times (with a warning), merges overlapping spans, and deletes them. No LLM call happens when the user toggles checkboxes or edits the summary.

Why this matters for changes:

- Treat the resume as the candidate's full pool of experience. Outside the summary, letting the model *add*, *rewrite*, or *reorder* content is a regression. Don't relax those rules in `prompts/tailor_resume.md`.
- Don't "fix" unapplied removals with fuzzy matching or by picking an occurrence; that breaks the determinism guarantee.
- UI state: `original_resume_state` (loaded source), `removals_state` (model proposals), `summary_box` and `removals_df` (the user's edits). The tailored resume always comes from `original_resume_state + summary_box + accepted removals`, never from the editable tailored textbox.
- The letter gets the tailored resume plus the long summary (`_long_summary(original)`) as `<candidate_story>`, its main narrative source. Without that, the letter would never see the long summary, since it's replaced in the tailored resume.

## JSON-with-LaTeX repair

`_repair_latex_json` in [app/llm.py](app/llm.py) exists because models often emit `\item`, `\hfill`, etc. as single backslashes inside JSON strings, which is invalid JSON. It rewrites any `\X` where X is not in `"\\/bfnrtu` into `\\X` before `json.loads`. Don't remove it; the prompt asks for proper escaping but models are inconsistent.

Debugging tailor failures:
1. Raw model output → `_extract_fenced(raw, "json")`
2. `json.loads(...)`; if it raises, retry on `_repair_latex_json(...)`
3. Normalize each removal to `{id, category, section, reason, removed_text, context}` with string coercion

## LaTeX engine notes

Tectonic invocation is `tectonic -X compile --outdir ... file.tex` (v2 CLI). Defaults to pdfLaTeX.

Preambles use `[utf8]{inputenc}` + `[T1]{fontenc}` + **`lmodern`**. The `lmodern` line is non-negotiable: without it, T1 glyphs such as Ţ (U+0162) and ă (U+0103) render as empty space because Tectonic's default Computer Modern bitmaps don't cover them. Keep `\usepackage{lmodern}` after `fontenc` in every `.tex` template or preamble.

Publications use `\nocite{*}` + `\bibliographystyle{unsrt}`, so entries print in `.bib` order (newest first by convention). Non-standard styles such as `plainyr-rev` aren't available in Tectonic and produce an empty list.

Tailor and letter prompts both forbid em dashes (—) because the user dislikes them. Don't drop the rule when editing prompts.

## Storage

`storage.connect()` is idempotent: it runs `SCHEMA` (`CREATE ... IF NOT EXISTS`) and then `_migrate`, which only adds missing columns via `ALTER TABLE ... ADD COLUMN`. The user's `app.db` holds their real application history: migrations must never rewrite, drop, or recreate existing rows or tables. Back up `app.db` before testing schema changes, and test them on a copy.

`status` holds a key from `storage.STATUSES` (default `no_response_or_rejected`). Keys are persisted; never rename them, only add new ones. `set_status` updates the DB row, its `meta_json`, and the folder's `meta.json`. Status isn't in the FTS index.

The FTS5 table is `content='applications'` (external-content), kept in sync via `AFTER INSERT` and `AFTER DELETE` triggers. If you add a searchable column, update the table and both triggers. Updating a searchable column would also need an `AFTER UPDATE` trigger.

File operations on an application folder (`delete_application`'s `rmtree`, `set_status`'s `meta.json` write) only happen when the folder resolves inside `applications/` (`_inside_apps_dir`). Don't loosen that check; hand-edited rows could point anywhere.

`find_similar` powers the Step 1 duplicate warning. Metric: word 3-gram **containment** (`|A∩B| / min(|A|,|B|)`), not Jaccard, so a posting wrapped in extra job-board boilerplate still scores near 1. Each new posting (raw and English) is compared with every stored English description and, if present, the original-language `job_description.txt`. On the real data, true duplicates scored 0.93 to 1.00 and distinct postings 0.42 or below, hence the 0.90 default (`DUPLICATE_THRESHOLD`). It's a brute-force scan (about 0.15 s for 140 rows); add an index or cache only if it gets slow. It only warns, never blocks.

`application_folder` builds `YYYY-MM-DD_company-slug_vacancy-slug`. Two applications to the same company/vacancy on the same day collide on disk; the second overwrites the first. Acceptable for a single-user app.

## Model selection

Per-step model and effort are read from env at import time (so `load_dotenv()` must run before `app.llm` is imported):

- `CLAUDE_MODEL_TRANSLATE` → Haiku 4.5
- `CLAUDE_MODEL_TAILOR` → Sonnet 5.5 (judgment-heavy; bump to Opus 5.5 if quality matters more than cost)
- `CLAUDE_MODEL_LETTER` → Sonnet 5.5
- `CLAUDE_MODEL_METADATA` → Haiku 4.5
- `CLAUDE_EFFORT_TAILOR` / `CLAUDE_EFFORT_LETTER` → `high`; `CLAUDE_EFFORT_TRANSLATE` / `CLAUDE_EFFORT_METADATA` → `low`

Don't set small `max_tokens` (e.g. 512) even for tiny outputs: 5-series models think by default and thinking counts toward the cap. Billing is per token used, so a generous cap is free. `_call` defaults to 32000.

`_call` gates request parameters per model, by regex on the model ID:

- `temperature` is an **allowlist** (`_TEMPERATURE_OK_RE`): Opus 4.7+ and all 5-series models reject non-default sampling with a 400. Keep it an allowlist so unknown future models are safe by default.
- `output_config.effort` is sent only where accepted (`_EFFORT_OK_RE`); Haiku 4.5 rejects it.
- 5-series models with safety classifiers (`_FALLBACK_RE`) get `fallbacks: "default"` via `extra_body` plus the `server-side-fallback-2026-07-01` beta, so a refusal is retried on another model server-side. `extra_body` because the pinned SDK doesn't type the param yet.
- Calls stream (`beta.messages.stream` + `get_final_message`) because large non-streaming `max_tokens` trips the SDK's timeout guard. `stop_reason` `refusal` / `max_tokens` raise instead of returning partial text. Read response blocks by `type`; thinking and fallback blocks can appear before the text.

Don't hardcode model IDs in new code paths; read them from `app.llm` constants so `.env` overrides keep working. The tailor user message has `cache_control: ephemeral` so regenerations reuse the prompt cache.

## Adding pipeline steps

1. Put the system prompt in `prompts/<name>.md` and load it with `_load_prompt`
2. Add `MODEL_<NAME>` / `EFFORT_<NAME>` constants read from env
3. Call through `_call(...)`, which retries 429/529 with backoff
4. If the output contains LaTeX inside JSON, run it through `_repair_latex_json` before parsing

## UI quirks worth knowing

- `gr.Dataframe(type="array")`: keep it. Pandas mode passes a DataFrame to handlers, which would break `_accepted_ids_from_rows`.
- The "Apply changes" button (not a `.change` event) drives local rebuilds. `removals_df.change` was unreliable across Gradio versions; if you ever revert, test boolean-cell toggling specifically.
- "Start new application" clears everything in `reset_targets` in `build_ui`. Adding per-application state? Add it there.

## Things that look like bugs but aren't

- `langdetect` flagging short English text as another language: the seed is fixed (`DetectorFactory.seed = 0`), so behavior is reproducible. The user can edit the translation anyway.
- `meta.json` duplicates the DB row: both are sources of truth for different consumers (disk browsing vs. search). Don't dedupe.
- A removal whose text appears several times is skipped with a warning instead of applied: intentional, see above.

## Don'ts

- Don't add em dashes anywhere in prompts or LaTeX you generate.
- Don't replace the verbatim-substring removal mechanism with fuzzy matching or LLM-driven re-application.
- Don't let the model generate resume text other than the summary.
- Don't drop `\usepackage{lmodern}` from any `.tex` preamble.
- Don't introduce a second compile pass; Tectonic handles all passes internally.
- Don't commit personal data or modify existing rows in `app.db` during migrations.
