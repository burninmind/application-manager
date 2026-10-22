# application-manager

A small Gradio app that turns a pasted job description plus your LaTeX resume into a tailored resume and motivation letter, compiles both with Tectonic, and keeps every application in a searchable local database with its outcome.

## What it does

For each job posting you process:

1. **Translate** the job description into English if needed (otherwise pass it through), and **warn** if you already applied to a near-identical posting, e.g. the same job found on another board.
2. **Tailor your resume.** The model writes a short summary for this job, replacing the long career story in your resume, and proposes removals: irrelevant bullets, skills, and sections, plus all but the best-matching projects and publications. You edit the summary and accept or reject each removal with a checkbox; the resume is rebuilt locally without another LLM call.
3. **Draft a motivation letter** from your long career story, the tailored resume, and the job description, using your LaTeX letter template.
4. **Compile** both `.tex` files to PDF with Tectonic and **store** everything (resume, letter, job description, metadata) in a SQLite FTS5 index.
5. **Track the outcome** of each application from the Search tab.

You review and edit the output of every step. Only the final accepted versions are saved.

## Prerequisites

- **Python 3.13+**
- **Tectonic** (LaTeX engine, single binary, no system TeX required)
- An **Anthropic API key**

### Install Tectonic

- **Windows** (PowerShell): `winget install TectonicTypesetting.Tectonic`, or with Scoop: `scoop install tectonic`
- **macOS**: `brew install tectonic`
- **Linux**: your distro package, or `cargo install tectonic`

Verify with `tectonic --version`.

## Setup

```powershell
# from the project root
conda create -n app_m python=3.13 -y
conda activate app_m
pip install -r requirements.txt

copy .env.example .env                # then edit .env: set ANTHROPIC_API_KEY and APPLICANT_NAME
```

## Your resume

`resume.tex` in the repo is an **example for a fictional person**. Use it as a template for your own:

- Save your resume as **`resume.local.tex`** in the project root, or upload it in Step 0 of the app (which saves it there). The file is gitignored, so it never ends up in the repo. When it exists, the app uses it instead of the example.
- Make it your **complete pool of experience**: every bullet, skill, project, and publication you might want. Tailoring only removes material; it never adds bullets.
- Wrap a **long summary** in marker lines. Write it as your career story: what you did, why, what drives you, what you look for in an employer. Tailoring replaces it with a one-paragraph summary for each job, and the letter uses it as its main source:

  ```latex
  \section*{Summary}
  % BEGIN SUMMARY
  Your long career story ...
  % END SUMMARY
  ```

- Put **publications** as BibTeX entries inside a `\begin{filecontents*}{publications.bib}` block, rendered with `\nocite{*}` and `\bibliographystyle{unsrt}` (entries print in file order, so list them newest first). The app keeps the best `RESUME_MAX_PUBLICATIONS` of them. It does the same for projects, using `RESUME_MAX_PROJECTS`.
- Lines like "For more projects, please visit ..." are always kept.
- Keep `\usepackage{lmodern}` after `\usepackage[T1]{fontenc}`; without it, accented characters (e.g. Ţ, ă) render as blanks.

`templates/letter.tex` is the letter template. Replace it with your own design if you like. The app fills `<<COMPANY>>`, `<<NAME>>` (from `APPLICANT_NAME`), and `<<BODY>>`.

## Run

```powershell
python -m app.main
```

Or double-click `run_server.bat` on Windows. Gradio prints a local URL (`http://127.0.0.1:7860`). Open it in a browser.

## Usage walkthrough

1. **Step 0, Resume**: choose *Use current resume* or *Upload new resume*, then *Load resume*. Uploading saves the file as `resume.local.tex`. A warning appears if the summary markers are missing.
2. **Step 1, Job description**: paste the posting and click *Translate / accept*. English text passes through unchanged; anything else is translated into an editable box. If the posting matches one you already applied to (at least `DUPLICATE_THRESHOLD`), a warning lists the earlier applications with their match score, date, and status. The check is local and takes a fraction of a second. *Auto-fill company & vacancy* extracts both fields; correct them if needed.
3. **Step 2, Tailored resume**: click *Generate tailored resume*. You get:
   - a **short summary** (editable), which replaces the long one;
   - a table of **proposed removals**, each with its type (bullet, skill, project, publication, section), the exact text, and a reason. All are accepted by default.

   Edit the summary or untick removals, then click *Apply changes*. The tailored `.tex` is rebuilt from your original resume, the summary, and the accepted removals, with no LLM call. Warnings appear if a removal can't be applied, if more publications remain than the limit, or if a "for more ..." line would be lost. Use the feedback box and *Regenerate with feedback* for a different proposal.
4. **Step 3, Motivation letter**: click *Draft motivation letter*. The model fills only the body of your template. Edit freely, or regenerate with feedback.
5. **Step 4, Finalize**: click *Compile & save*. Tectonic produces two PDFs and the application is saved to `applications/` and `app.db`.

Click *Start new application* to clear everything except the loaded resume.

### Search and status

The **Search** tab queries every stored application via SQLite FTS5 (company, vacancy title, job description, resume, and letter text). An empty search lists the newest. Each row shows the application's **status**:

- Haven't heard back / rejected (default)
- Rejected after first interview
- Rejected after second interview
- Negotiations broke down
- Accepted

To change one, enter its ID, pick the status, and click *Update status*. The same section lets you delete an application (database row and folder).

## Storage layout

```
application-manager/
  resume.tex                          # example resume (fictional person)
  resume.local.tex                    # your resume (gitignored)
  templates/letter.tex                # letter template
  applications/                       # gitignored
    2026-05-11_acme_data-engineer/
      <APPLICANT_NAME>-resume.tex/.pdf
      <APPLICANT_NAME>-letter.tex/.pdf
      job_description.txt             # original-language text
      job_description.en.txt          # only if translation happened
      meta.json                       # company, vacancy, date, status
  app.db                              # SQLite index (FTS5), gitignored
```

Existing databases are upgraded automatically on startup: the `status` column is added to the `applications` table, and existing rows get the default status. Nothing else is changed.

## Configuration

`.env` keys (all optional except the API key):

| Key                       | Default             | Purpose                                                                 |
| ------------------------- | ------------------- | ----------------------------------------------------------------------- |
| `ANTHROPIC_API_KEY`       | *(required)*        | Your Anthropic API key                                                  |
| `APPLICANT_NAME`          | *(empty)*           | Signs the letter and prefixes file names, e.g. `Jane Doe-resume.pdf`. Empty gives `resume.pdf` / `letter.pdf`. |
| `RESUME_MAX_PROJECTS`     | `3`                 | Projects kept in the tailored resume                                    |
| `RESUME_MAX_PUBLICATIONS` | `3`                 | Publications kept in the tailored resume                                |
| `DUPLICATE_THRESHOLD`     | `0.90`              | Duplicate-posting warning: minimum share (0 to 1) of the shorter posting's word 3-grams found in a stored one |
| `CLAUDE_MODEL_TRANSLATE`  | `claude-haiku-4-5`  | Step 1: translate job description                                       |
| `CLAUDE_MODEL_TAILOR`     | `claude-sonnet-5-5` | Step 2: summary and removals (the judgment-heavy step)                  |
| `CLAUDE_MODEL_LETTER`     | `claude-sonnet-5-5` | Step 3: motivation letter                                               |
| `CLAUDE_MODEL_METADATA`   | `claude-haiku-4-5`  | Step 1: extract company and vacancy                                     |
| `CLAUDE_EFFORT_TAILOR`    | `high`              | Effort for step 2 (`low`/`medium`/`high`/`xhigh`/`max`)                 |
| `CLAUDE_EFFORT_LETTER`    | `high`              | Effort for step 3                                                       |
| `CLAUDE_EFFORT_TRANSLATE` | `low`               | Effort for translation                                                  |
| `CLAUDE_EFFORT_METADATA`  | `low`               | Effort for metadata extraction                                          |

Defaults pick the cheapest model that does each job well. Switch tailor and letter to `claude-opus-5-5` for top quality.

Newer models (Opus 4.7+ and every 5-series model) don't accept `temperature`; the app only sends it to models that do. On those models, `effort` is the quality knob: higher effort means more thinking, at more cost and latency. Models that don't support effort (Haiku 4.5) ignore it.

Prompts live in `prompts/*.md`. Edit them to change tone or rules without touching code.

## Troubleshooting

- **`tectonic not found`**: install it (see Prerequisites) and make sure it's on `PATH`. The app fails fast at startup without it.
- **LaTeX compile errors**: the error and the tail of the Tectonic log appear in the UI. Fix the `.tex` in the editor and click *Compile & save* again.
- **First compile is slow**: Tectonic downloads packages on first use, then caches them.
- **"Could not apply ... matches N places"**: the removed text appears more than once in your resume, so the app won't guess which one to delete. Regenerate, or edit the tailored `.tex` by hand.
- **`langdetect` flags English text as another language**: paste at least a few sentences; very short inputs are unreliable. The seed is fixed, so behavior is reproducible.
- **Rate limits (429 / 529)**: the client retries with exponential backoff, up to four attempts. Persistent failures usually mean you're out of credits or hitting tier limits.

## Project layout

```
app/
  main.py        # Gradio UI and step orchestration
  llm.py         # Claude API wrappers, one function per step
  latex.py       # Tectonic subprocess wrapper
  storage.py     # SQLite + FTS5, status tracking
prompts/         # system prompts (.md), one per LLM step
templates/       # LaTeX letter template
```
