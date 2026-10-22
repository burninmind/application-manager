"""Gradio UI and step orchestration."""
from __future__ import annotations

import os
import re
import shutil
from datetime import date, datetime
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv

load_dotenv()  # before importing app.llm, which reads model settings at import time

from langdetect import DetectorFactory, detect

from app import latex, llm, storage

DetectorFactory.seed = 0

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_RESUME_PATH = ROOT / "resume.tex"  # committed placeholder
LOCAL_RESUME_PATH = ROOT / "resume.local.tex"  # your own resume, gitignored
LETTER_TEMPLATE_PATH = ROOT / "templates" / "letter.tex"
APPLICANT_NAME = os.getenv("APPLICANT_NAME", "").strip()
MAX_PROJECTS = int(os.getenv("RESUME_MAX_PROJECTS", "3"))
MAX_PUBLICATIONS = int(os.getenv("RESUME_MAX_PUBLICATIONS", "3"))
DUPLICATE_THRESHOLD = float(os.getenv("DUPLICATE_THRESHOLD", "0.90"))

SUMMARY_RE = re.compile(r"(?P<begin>^[ \t]*% BEGIN SUMMARY[^\n]*\n)(?P<body>.*?)(?P<end>^[ \t]*% END SUMMARY)", re.S | re.M)
BIB_BLOCK_RE = re.compile(r"\\begin\{filecontents\*?\}.*?\\end\{filecontents\*?\}", re.S)
BIB_ENTRY_RE = re.compile(r"^\s*@\w+\s*\{", re.M)
POINTER_LINE_RE = re.compile(r"^.*\bfor more\b.*$", re.I | re.M)

STATUS_CHOICES = [(label, key) for key, label in storage.STATUSES.items()]


# ---------- resume ----------

def _active_resume_path() -> Path:
    return LOCAL_RESUME_PATH if LOCAL_RESUME_PATH.exists() else EXAMPLE_RESUME_PATH


def _resume_status() -> str:
    path = _active_resume_path()
    if not path.exists():
        return "**No resume found.** Upload one below."
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    note = "" if path == LOCAL_RESUME_PATH else " (the example resume; upload your own to replace it)"
    return f"**Current resume:** `{path.name}`{note}, last modified {mtime:%Y-%m-%d %H:%M}"


def _load_resume(uploaded_file, choice: str) -> tuple[str, str, str]:
    """Returns (resume_tex for display, status markdown, resume_tex for state)."""
    if choice == "Upload new resume":
        if uploaded_file is None:
            raise gr.Error("Choose a .tex file to upload, or switch to 'Use current resume'.")
        src = Path(uploaded_file if isinstance(uploaded_file, str) else uploaded_file.name)
        if src.suffix.lower() != ".tex":
            raise gr.Error("Resume must be a .tex file.")
        shutil.copyfile(src, LOCAL_RESUME_PATH)
    path = _active_resume_path()
    if not path.exists():
        raise gr.Error("No resume available. Upload one first.")
    tex = path.read_text(encoding="utf-8")
    status = _resume_status()
    if not SUMMARY_RE.search(tex):
        status += (
            "\n\n**Warning:** no `% BEGIN SUMMARY` / `% END SUMMARY` markers found. "
            "Wrap your long summary in them before tailoring (see `resume.tex`)."
        )
    return tex, status, tex


def _long_summary(resume_tex: str) -> str:
    m = SUMMARY_RE.search(resume_tex)
    return m.group("body").strip() if m else ""


def _replace_summary(resume_tex: str, summary: str) -> str:
    """Swap the marked long summary for the short one, keeping the markers."""
    return SUMMARY_RE.sub(lambda m: m.group("begin") + summary.strip() + "\n" + m.group("end"), resume_tex, count=1)


# ---------- tailoring ----------

def _locate(base: str, r: dict) -> tuple[tuple[int, int] | None, str | None]:
    """Find the (start, end) span a removal deletes in base, or explain why it can't be applied."""
    label = r.get("section") or r["id"]
    snippet, context = r["removed_text"], r.get("context") or ""
    if not snippet.strip():
        return None, None
    if context:
        if snippet not in context:
            return None, f"'{label}': removed text is not inside its context."
        anchor, offset = context, context.index(snippet)
    else:
        anchor, offset = snippet, 0
    count = base.count(anchor)
    if count == 0:
        return None, f"'{label}': text not found verbatim in the resume."
    if count > 1:
        return None, f"'{label}': text matches {count} places; not applied. Regenerate or edit by hand."
    start = base.index(anchor) + offset
    return (start, start + len(snippet)), None


def _apply_removals(base: str, removals: list[dict], accepted_ids: set[str]) -> tuple[str, list[str]]:
    """Delete every accepted removal from base. All spans are resolved against the same text."""
    spans, warnings = [], []
    for r in removals:
        if r["id"] not in accepted_ids:
            continue
        span, warning = _locate(base, r)
        if warning:
            warnings.append(f"Could not apply {warning}")
        if span:
            spans.append(span)
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out = base
    for start, end in reversed(merged):
        out = out[:start] + out[end:]
    return out, warnings


def _check_tailored(base: str, tailored: str) -> list[str]:
    warnings = []
    bib = BIB_BLOCK_RE.search(tailored)
    n_pubs = len(BIB_ENTRY_RE.findall(bib.group(0))) if bib else 0
    if n_pubs > MAX_PUBLICATIONS:
        warnings.append(f"{n_pubs} publications remain; the limit is {MAX_PUBLICATIONS}.")
    for line in POINTER_LINE_RE.findall(base):
        if line.strip() and line not in tailored:
            warnings.append(f"A 'for more ...' line was removed: `{line.strip()[:80]}`")
    return warnings


def _build_tailored(original: str, summary: str, removals: list[dict], accepted_ids: set[str]) -> tuple[str, list[str]]:
    """Original resume + short summary + accepted removals. Deterministic, no LLM call."""
    base = _replace_summary(original, summary)
    tailored, warnings = _apply_removals(base, removals, accepted_ids)
    return tailored, warnings + _check_tailored(base, tailored)


def _removals_to_rows(removals: list[dict], accepted_ids: set[str]) -> list[list]:
    return [
        [r["id"] in accepted_ids, r["category"], r["section"], r["removed_text"].strip(), r["reason"]]
        for r in removals
    ]


def _accepted_ids_from_rows(rows, removals: list[dict]) -> set[str]:
    """Rows carry only the checkbox, so map them back to removal ids by position."""
    try:
        accept_col = [bool(row[0]) for row in rows]
    except (TypeError, IndexError):
        return {r["id"] for r in removals}
    return {r["id"] for r, accept in zip(removals, accept_col) if accept}


def _status_md(removals: list[dict], warnings: list[str]) -> str:
    if removals:
        head = f"_{len(removals)} removal(s) proposed. Untick any you want to keep, then press Apply changes._"
    else:
        head = "_No removals were proposed._"
    return "\n".join([head, ""] + [f"- {w}" for w in warnings]) if warnings else head


def _tailor(original: str, jd_english: str, summary: str = "", removals: list[dict] | None = None, feedback: str = ""):
    """Returns (tailored_tex, removals_state, dataframe_rows, summary, status_md)."""
    if not original.strip():
        raise gr.Error("Resume is empty. Load a resume in step 0.")
    if not jd_english.strip():
        raise gr.Error("Confirm the job description in step 1 first.")
    if not SUMMARY_RE.search(original):
        raise gr.Error("The resume needs '% BEGIN SUMMARY' and '% END SUMMARY' lines around the long summary.")
    previous = {"summary": summary, "removals": removals} if removals or summary else None
    result = llm.tailor_resume(
        original,
        jd_english,
        max_projects=MAX_PROJECTS,
        max_publications=MAX_PUBLICATIONS,
        previous=previous,
        feedback=feedback.strip() or None,
    )
    accepted = {r["id"] for r in result["removals"]}
    tailored, warnings = _build_tailored(original, result["summary"], result["removals"], accepted)
    rows = _removals_to_rows(result["removals"], accepted)
    return tailored, result["removals"], rows, result["summary"], _status_md(result["removals"], warnings)


def _retailor(original: str, jd_english: str, summary: str, removals: list[dict], feedback: str):
    if not feedback.strip():
        raise gr.Error("Type feedback first, e.g. 'keep the forecasting project'.")
    return _tailor(original, jd_english, summary, removals, feedback)


def _reapply_local(original: str, summary: str, removals: list[dict], rows) -> tuple[str, str]:
    accepted = _accepted_ids_from_rows(rows, removals)
    tailored, warnings = _build_tailored(original, summary, removals, accepted)
    return tailored, _status_md(removals, warnings)


# ---------- letter ----------

def _draft_letter(tailored: str, original: str, jd_english: str, company: str, previous: str = "", feedback: str = "") -> str:
    if not LETTER_TEMPLATE_PATH.exists():
        raise gr.Error(f"Missing letter template at {LETTER_TEMPLATE_PATH}.")
    company = (company or "").strip()
    if not company:
        raise gr.Error("Fill in the Company field (Step 1) before drafting the letter.")
    template = (
        LETTER_TEMPLATE_PATH.read_text(encoding="utf-8")
        .replace("<<COMPANY>>", company)
        .replace("<<NAME>>", APPLICANT_NAME)
    )
    return llm.motivation_letter(
        resume_tex=tailored,
        long_summary=_long_summary(original),
        job_description=jd_english,
        template_tex=template,
        company=company,
        previous_attempt=previous.strip() or None,
        feedback=feedback.strip() or None,
    )


def _redraft_letter(tailored: str, original: str, jd_english: str, company: str, current_letter: str, feedback: str) -> str:
    if not current_letter.strip():
        raise gr.Error("Draft a letter first, then add feedback.")
    if not feedback.strip():
        raise gr.Error("Type feedback first, e.g. 'more concrete, fewer adjectives'.")
    return _draft_letter(tailored, original, jd_english, company, current_letter, feedback)


# ---------- other steps ----------

def _process_jd(raw_jd: str) -> tuple[str, str, str]:
    """Returns (English text for the editable box, original text for state, duplicate warning)."""
    raw_jd = (raw_jd or "").strip()
    if not raw_jd:
        raise gr.Error("Paste a job description first.")
    try:
        lang = detect(raw_jd)
    except Exception:
        lang = "und"
    english = raw_jd if lang == "en" else llm.translate_job_description(raw_jd)
    return english, raw_jd, _duplicate_warning([raw_jd, english])


def _duplicate_warning(texts: list[str]) -> str:
    matches = storage.find_similar(texts, DUPLICATE_THRESHOLD)
    if not matches:
        return ""
    gr.Warning("This job description looks like one you already applied to. See Step 1.")
    lines = [
        f"- **{m['score']:.0%}** match: #{m['id']} {m['company']} / {m['vacancy']}, "
        f"applied {m['processed_at']} ({storage.STATUSES.get(m['status'], m['status'])})"
        for m in matches
    ]
    return "**Possible duplicate application.** This posting closely matches:\n\n" + "\n".join(lines)


def _extract_meta(jd_english: str) -> tuple[str, str]:
    meta = llm.extract_metadata(jd_english)
    return meta["company"], meta["vacancy"]


def _file_stems() -> tuple[str, str]:
    if APPLICANT_NAME:
        return f"{APPLICANT_NAME}-resume", f"{APPLICANT_NAME}-letter"
    return "resume", "letter"


def _finalize(company: str, vacancy: str, tailored: str, letter: str, jd_english: str, jd_original: str):
    company = (company or "Unknown").strip()
    vacancy = (vacancy or "Unknown").strip()
    today = date.today()
    folder = storage.application_folder(company, vacancy, today)
    folder.mkdir(parents=True, exist_ok=True)

    resume_stem, letter_stem = _file_stems()
    resume_path = folder / f"{resume_stem}.tex"
    letter_path = folder / f"{letter_stem}.tex"
    resume_path.write_text(tailored, encoding="utf-8")
    letter_path.write_text(letter, encoding="utf-8")
    try:
        resume_pdf = latex.compile_tex(resume_path, folder)
        letter_pdf = latex.compile_tex(letter_path, folder)
    except latex.TectonicError as e:
        raise gr.Error(f"{e}\n\n--- tectonic log ---\n{e.log[-4000:]}")

    storage.save_application(
        folder=folder,
        company=company,
        vacancy=vacancy,
        on_date=today,
        job_description_english=jd_english,
        job_description_original=jd_original or jd_english,
        resume_tex=tailored,
        letter_tex=letter,
    )
    return f"Saved to `{folder}`.", str(resume_pdf), str(letter_pdf)


# ---------- search, status, delete ----------

def _search(query: str) -> list[list]:
    return [
        [r["id"], r["processed_at"], r["company"], r["vacancy"], storage.STATUSES.get(r["status"], r["status"]), r["folder_path"]]
        for r in storage.search(query)
    ]


def _parse_id(app_id) -> int:
    try:
        return int(app_id)
    except (TypeError, ValueError):
        raise gr.Error("Enter a valid numeric application ID.")


def _set_status(app_id, status: str, query: str) -> tuple[str, list[list]]:
    try:
        row = storage.set_status(_parse_id(app_id), status)
    except (LookupError, ValueError) as e:
        raise gr.Error(str(e))
    return f"#{row['id']} {row['company']} / {row['vacancy']}: {storage.STATUSES[status]}.", _search(query)


def _delete(app_id, confirm: bool, query: str) -> tuple[str, list[list]]:
    if not confirm:
        raise gr.Error("Tick the confirmation box before deleting.")
    try:
        row = storage.delete_application(_parse_id(app_id))
    except LookupError as e:
        raise gr.Error(str(e))
    return f"Deleted #{row['id']} {row['company']} / {row['vacancy']} (folder: `{row['folder_path']}`).", _search(query)


# ---------- UI ----------

def build_ui() -> gr.Blocks:
    with gr.Blocks(title="Application Manager") as demo:
        gr.Markdown("# Application Manager")

        jd_original_state = gr.State("")
        removals_state = gr.State([])
        original_resume_state = gr.State("")

        with gr.Tab("New application"):
            with gr.Accordion("Step 0: Choose resume", open=True):
                resume_status = gr.Markdown(_resume_status())
                resume_choice = gr.Radio(
                    ["Use current resume", "Upload new resume"],
                    value="Use current resume",
                    label="Resume source",
                )
                resume_upload = gr.File(label="Upload your resume (.tex)", file_types=[".tex"])
                load_resume_btn = gr.Button("Load resume", variant="primary")
                resume_view = gr.Textbox(label="Loaded resume (.tex, read-only)", lines=8, interactive=False)

            with gr.Accordion("Step 1: Job description", open=True):
                jd_input = gr.Textbox(label="Paste the job posting", lines=10)
                process_jd_btn = gr.Button("Translate / accept", variant="primary")
                duplicate_md = gr.Markdown()
                jd_english = gr.Textbox(label="English job description (editable)", lines=10)
                extract_btn = gr.Button("Auto-fill company & vacancy")
                with gr.Row():
                    company_in = gr.Textbox(label="Company")
                    vacancy_in = gr.Textbox(label="Vacancy title")

            with gr.Accordion("Step 2: Tailored resume", open=False):
                tailor_btn = gr.Button("Generate tailored resume", variant="primary")
                gr.Markdown(
                    "The model writes a short **summary** (replacing your long one) and proposes "
                    f"**removals**, keeping at most {MAX_PROJECTS} projects and {MAX_PUBLICATIONS} "
                    "publications. Edit the summary, untick removals you disagree with, then press "
                    "**Apply changes** to rebuild the resume below (no LLM call)."
                )
                summary_box = gr.Textbox(label="Short summary (LaTeX, editable)", lines=4)
                removals_df = gr.Dataframe(
                    headers=["Accept", "Type", "Section", "Removed", "Reason"],
                    datatype=["bool", "str", "str", "str", "str"],
                    interactive=True,
                    wrap=True,
                    column_count=(5, "fixed"),
                    type="array",
                )
                apply_btn = gr.Button("Apply changes", variant="primary")
                tailor_status = gr.Markdown()
                tailored_resume = gr.Textbox(label="Tailored resume (.tex, editable)", lines=20)
                resume_feedback = gr.Textbox(
                    label="Feedback for regeneration",
                    placeholder="e.g. keep the forecasting project; mention Azure in the summary",
                    lines=3,
                )
                retailor_btn = gr.Button("Regenerate with feedback")

            with gr.Accordion("Step 3: Motivation letter", open=False):
                draft_letter_btn = gr.Button("Draft motivation letter", variant="primary")
                letter_tex = gr.Textbox(label="Motivation letter (.tex, editable)", lines=20)
                letter_feedback = gr.Textbox(
                    label="Feedback for regeneration",
                    placeholder="e.g. more concrete impact numbers; warmer opening",
                    lines=3,
                )
                redraft_btn = gr.Button("Regenerate with feedback")

            with gr.Accordion("Step 4: Finalize & compile", open=False):
                compile_btn = gr.Button("Compile & save", variant="primary")
                finalize_status = gr.Markdown()
                resume_pdf_out = gr.File(label="Resume PDF")
                letter_pdf_out = gr.File(label="Letter PDF")

            new_application_btn = gr.Button("Start new application", variant="primary")

        with gr.Tab("Search"):
            search_input = gr.Textbox(
                label="Search past applications (FTS5; empty = newest)",
                placeholder='e.g. "python data" or "company:Acme"',
            )
            search_btn = gr.Button("Search")
            results_table = gr.Dataframe(
                headers=["ID", "Date", "Company", "Vacancy", "Status", "Folder"],
                interactive=False,
                wrap=True,
            )

            gr.Markdown("### Update status")
            with gr.Row():
                status_id = gr.Number(label="Application ID", precision=0)
                status_choice = gr.Dropdown(STATUS_CHOICES, value=storage.DEFAULT_STATUS, label="Status")
            status_btn = gr.Button("Update status")
            status_msg = gr.Markdown()

            gr.Markdown("### Delete an application")
            gr.Markdown("Removes the database row **and** the application folder on disk.")
            with gr.Row():
                delete_id = gr.Number(label="Application ID", precision=0)
                delete_confirm = gr.Checkbox(label="I understand this is permanent", value=False)
            delete_btn = gr.Button("Delete application", variant="stop")
            delete_status = gr.Markdown()

        # ---- wiring ----
        load_resume_btn.click(
            _load_resume,
            inputs=[resume_upload, resume_choice],
            outputs=[resume_view, resume_status, original_resume_state],
        )
        process_jd_btn.click(_process_jd, inputs=[jd_input], outputs=[jd_english, jd_original_state, duplicate_md])
        extract_btn.click(_extract_meta, inputs=[jd_english], outputs=[company_in, vacancy_in])

        tailor_outputs = [tailored_resume, removals_state, removals_df, summary_box, tailor_status]
        tailor_btn.click(_tailor, inputs=[original_resume_state, jd_english], outputs=tailor_outputs)
        retailor_btn.click(
            _retailor,
            inputs=[original_resume_state, jd_english, summary_box, removals_state, resume_feedback],
            outputs=tailor_outputs,
        )
        apply_btn.click(
            _reapply_local,
            inputs=[original_resume_state, summary_box, removals_state, removals_df],
            outputs=[tailored_resume, tailor_status],
        )

        draft_letter_btn.click(
            _draft_letter,
            inputs=[tailored_resume, original_resume_state, jd_english, company_in],
            outputs=[letter_tex],
        )
        redraft_btn.click(
            _redraft_letter,
            inputs=[tailored_resume, original_resume_state, jd_english, company_in, letter_tex, letter_feedback],
            outputs=[letter_tex],
        )

        compile_btn.click(
            _finalize,
            inputs=[company_in, vacancy_in, tailored_resume, letter_tex, jd_english, jd_original_state],
            outputs=[finalize_status, resume_pdf_out, letter_pdf_out],
        )

        search_btn.click(_search, inputs=[search_input], outputs=[results_table])
        status_btn.click(
            _set_status,
            inputs=[status_id, status_choice, search_input],
            outputs=[status_msg, results_table],
        )
        delete_btn.click(
            _delete,
            inputs=[delete_id, delete_confirm, search_input],
            outputs=[delete_status, results_table],
        )

        # Everything specific to one application; keep in sync when adding state.
        reset_targets = {
            jd_input: "", jd_english: "", jd_original_state: "", duplicate_md: "",
            summary_box: "", removals_state: [], removals_df: [], tailor_status: "",
            tailored_resume: "", resume_feedback: "",
            letter_tex: "", letter_feedback: "",
            company_in: "", vacancy_in: "",
            finalize_status: "", resume_pdf_out: None, letter_pdf_out: None,
        }
        new_application_btn.click(
            lambda: list(reset_targets.values()),
            inputs=[],
            outputs=list(reset_targets),
            js="() => { window.scrollTo({ top: 0, behavior: 'smooth' }); }",
        )

    return demo


def main() -> None:
    latex.ensure_tectonic()  # fail fast with an install hint
    build_ui().launch()


if __name__ == "__main__":
    main()
