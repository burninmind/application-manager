# application-manager

A small Gradio app that turns a pasted job description plus your LaTeX resume into a tailored resume and motivation letter, compiles both with Tectonic, and stores everything in a searchable local database.

## What it does

For each job posting you process:

1. **Translate** the job description into English if needed (otherwise pass through).
2. **Tailor your resume** to the posting — the model proposes *removals only* (verbatim substrings to delete), which you accept or reject individually with checkboxes; the tailored resume is rebuilt locally without a second LLM call.
3. **Draft a motivation letter** from your tailored resume and the job description, using your LaTeX letter template.
4. **Compile** both `.tex` files to PDF with Tectonic and **store** the artifacts (resume, letter, job description, metadata) in a SQLite FTS5 index for later search.

You review and edit the output of every step before it's accepted. Only the final accepted versions are saved.

## Prerequisites

- **Python 3.13+**
- **Tectonic** (LaTeX engine, single binary, no system TeX required)
- An **Anthropic API key** with access to Claude Opus 4.7 (or override the model — see below)

### Install Tectonic

- **Windows** (PowerShell): `winget install TectonicTypesetting.Tectonic`
  Or with Scoop: `scoop install tectonic`
- **macOS**: `brew install tectonic`
- **Linux**: your distro package, or `cargo install tectonic`

Verify with `tectonic --version`.

## Setup

```powershell
# from the project root
conda create -n app_m python=3.11 -y
conda activate app_m
pip install -r requirements.txt

copy .env.example .env                # then edit .env to set ANTHROPIC_API_KEY
```

Place two files in the project root:

- **`resume.tex`** — your current full resume in LaTeX. The app keeps the latest version here and lets you replace it from the UI.
- **`templates/letter.tex`** — your motivation letter template. The shipped file is a placeholder; replace it with your own design. The app fills two placeholders inside it: `<<BODY>>` and `<<COMPANY>>`. Your template handles date and any address block on its own.

## Run

```powershell
python -m app.main
```

Gradio prints a local URL (`http://127.0.0.1:7860`). Open it in a browser.

## Usage walkthrough

1. **Step 0 — Resume**: choose *Use current resume* or *Upload new resume*. Uploading replaces `resume.tex` in the project root, so it becomes the new "current" next time.
2. **Step 1 — Job description**: paste the posting and click *Translate / accept*. If it's already English you see it unchanged; otherwise the English translation appears in an editable box.
3. **Step 2 — Tailored resume**: click *Generate*. The model returns a table of proposed removals (each with the exact verbatim text it would delete and why). All are accepted by default. Untick any you want to keep, then click *Apply changes* — the tailored `.tex` is rebuilt locally from your original resume minus the accepted removals (no second LLM call). The model is constrained to **only remove** content; it cannot add or rewrite. Use the feedback box + *Regenerate* if you want a different set of proposed removals.
4. **Step 3 — Motivation letter**: click *Draft*. The model fills only the body of your template. Edit freely.
5. **Step 4 — Finalize**: click *Auto-fill company & vacancy* to extract them from the job description (correct them if needed), then *Compile & save*. Tectonic produces two PDFs, and everything is indexed in `app.db`.

The **Search** tab queries every stored application via SQLite FTS5 (works on company, vacancy title, job description, resume, and letter text).

## Storage layout

```
application-manager/
  resume.tex                          # canonical latest resume
  templates/letter.tex                # your letter template
  applications/
    2026-05-11_acme_data-engineer/
      resume.tex
      resume.pdf
      letter.tex
      letter.pdf
      job_description.txt             # original-language text
      job_description.en.txt          # only if translation happened
      meta.json
  app.db                              # SQLite index (FTS5)
```

## Configuration

`.env` keys (all optional except the API key):

| Key                       | Default                       | Purpose                                          |
| ------------------------- | ----------------------------- | ------------------------------------------------ |
| `ANTHROPIC_API_KEY`       | *(required)*                  | Your Anthropic API key                           |
| `APPLICANT_NAME`          | *(empty)*                     | Prefixes generated file names, e.g. `Ali Mahmoudi-resume.pdf`. Empty falls back to `resume.pdf` / `letter.pdf`. |
| `CLAUDE_MODEL_TRANSLATE`  | `claude-haiku-4-5`            | Step 1: translate job description                |
| `CLAUDE_MODEL_TAILOR`     | `claude-sonnet-5-5`           | Step 2: tailor resume (hardest step, judgment)   |
| `CLAUDE_MODEL_LETTER`     | `claude-sonnet-5-5`           | Step 3: draft motivation letter                  |
| `CLAUDE_MODEL_METADATA`   | `claude-haiku-4-5`            | Step 4: extract company + vacancy from JD        |
| `CLAUDE_EFFORT_TAILOR`    | `high`                        | Effort for step 2 (`low`/`medium`/`high`/`xhigh`/`max`) |
| `CLAUDE_EFFORT_LETTER`    | `high`                        | Effort for step 3                                |
| `CLAUDE_EFFORT_TRANSLATE` | `low`                         | Effort for step 1                                |
| `CLAUDE_EFFORT_METADATA`  | `low`                         | Effort for step 4                                |

Defaults pick the cheapest model that does each job well. Bump tailor/letter to `claude-opus-5-5` if you want top quality; bump translate to Sonnet for very long or jargon-heavy postings.

Newer models (Opus 4.7+ and every 5-series model) don't accept `temperature`; the app only sends it to models that do. On those models, `effort` is the quality knob instead: higher effort means more thinking before answering, at more cost and latency. Effort is ignored by models that don't support it (Haiku 4.5).

Prompts live in `prompts/*.md` — edit them to change tone or rules without touching code.

## Troubleshooting

- **`tectonic not found`** — install it (see Prerequisites) and ensure it's on `PATH`. The app fails fast at startup if it can't find it.
- **LaTeX compile errors** — the error and the tail of the Tectonic log appear in the UI. Fix the `.tex` in the editor and click *Compile & save* again.
- **First compile is slow** — Tectonic downloads required packages on first use, then caches them.
- **`langdetect` flags English text as something else** — paste at least a few sentences; very short inputs are unreliable. The seed is fixed so behavior is reproducible.
- **Rate limits (429 / 529)** — the client retries with exponential backoff up to four attempts. Persistent failures usually mean you're out of credits or hitting tier limits.

## Project layout

```
app/
  main.py        # Gradio UI
  llm.py         # Claude API wrappers
  latex.py       # Tectonic compile + template filling
  storage.py     # SQLite + FTS5
prompts/         # Prompt templates (.md)
templates/       # LaTeX letter template
```
