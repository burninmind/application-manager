"""Gradio UI for the application manager."""
from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv

load_dotenv()

from langdetect import DetectorFactory, detect

from app import latex, llm, storage

DetectorFactory.seed = 0

ROOT = Path(__file__).resolve().parent.parent
RESUME_PATH = ROOT / "resume.tex"
LETTER_TEMPLATE_PATH = ROOT / "templates" / "letter.tex"
APPLICANT_NAME = os.getenv("APPLICANT_NAME", "").strip()


def _file_stems() -> tuple[str, str]:
    """Return (resume_stem, letter_stem) for the generated files."""
    if APPLICANT_NAME:
        return f"{APPLICANT_NAME}-resume", f"{APPLICANT_NAME}-letter"
    return "resume", "letter"


# ---------- helpers ----------

def _resume_status() -> str:
    if not RESUME_PATH.exists():
        return "**No `resume.tex` in project root yet.** Upload one below."
    mtime = datetime.fromtimestamp(RESUME_PATH.stat().st_mtime)
    return f"**Current resume:** `resume.tex` (last modified {mtime:%Y-%m-%d %H:%M})"


def _load_resume_action(uploaded_file, choice: str) -> tuple[str, str]:
    """Returns (resume_tex, status_markdown)."""
    if choice == "Upload new resume":
        if uploaded_file is None:
            raise gr.Error("Choose a .tex file to upload, or switch to 'Use current resume'.")
        src = Path(uploaded_file if isinstance(uploaded_file, str) else uploaded_file.name)
        if src.suffix.lower() != ".tex":
            raise gr.Error("Resume must be a .tex file.")
        shutil.copyfile(src, RESUME_PATH)
    if not RESUME_PATH.exists():
        raise gr.Error("No resume.tex available. Upload one first.")
    return RESUME_PATH.read_text(encoding="utf-8"), _resume_status()


def _process_jd(raw_jd: str) -> str:
    raw_jd = (raw_jd or "").strip()
    if not raw_jd:
        raise gr.Error("Paste a job description first.")
    try:
        lang = detect(raw_jd)
    except Exception:
        lang = "und"
    if lang == "en":
        return raw_jd
    return llm.translate_job_description(raw_jd)


def _apply_removals(original: str, removals: list[dict], accepted_ids: set[str]) -> tuple[str, list[str]]:
    """Return (tailored_tex, warnings). Pure string deletion — no LLM call."""
    out = original
    warnings: list[str] = []
    for r in removals:
        if r.get("id") not in accepted_ids:
            continue
        snippet = r.get("removed_text") or ""
        if not snippet:
            continue
        if snippet not in out:
            warnings.append(
                f"Could not apply removal '{r.get('section') or r.get('id')}': "
                f"text not found verbatim in the resume."
            )
            continue
        out = out.replace(snippet, "", 1)
    return out, warnings


def _removals_to_rows(removals: list[dict], accepted_ids: set[str]) -> list[list]:
    rows: list[list] = []
    for r in removals:
        rows.append([
            r.get("id") in accepted_ids,
            r.get("section") or "",
            (r.get("removed_text") or "").strip(),
            r.get("reason") or "",
        ])
    return rows


def _accepted_ids_from_rows(rows, removals: list[dict]) -> set[str]:
    """Map dataframe rows (which carry only the bool) back to removal ids by position."""
    if rows is None:
        return {r["id"] for r in removals}
    try:
        accept_col = [bool(row[0]) for row in rows]
    except (TypeError, IndexError):
        return {r["id"] for r in removals}
    return {
        r["id"]
        for r, accept in zip(removals, accept_col)
        if accept
    }


def _tailor(
    resume_tex: str,
    jd_english: str,
    previous_removals: list[dict] | None = None,
    feedback: str = "",
) -> tuple[str, list[dict], list[list], str]:
    """Run LLM, then locally apply all proposed removals.

    Returns (tailored_tex, removals_state, dataframe_rows, status_md).
    """
    if not resume_tex.strip():
        raise gr.Error("Resume is empty. Load a resume in step 0.")
    if not jd_english.strip():
        raise gr.Error("Confirm the job description in step 1 first.")
    removals = llm.tailor_resume(
        resume_tex,
        jd_english,
        previous_removals=previous_removals or None,
        feedback=feedback.strip() or None,
    )
    accepted = {r["id"] for r in removals}
    tailored, warnings = _apply_removals(resume_tex, removals, accepted)
    rows = _removals_to_rows(removals, accepted)
    status = _removals_status(removals, warnings)
    return tailored, removals, rows, status


def _retailor(
    resume_tex: str,
    jd_english: str,
    current_removals: list[dict],
    feedback: str,
) -> tuple[str, list[dict], list[list], str]:
    if not feedback.strip():
        raise gr.Error("Type feedback first, e.g. 'keep the open-source section'.")
    return _tailor(
        resume_tex,
        jd_english,
        previous_removals=current_removals or None,
        feedback=feedback,
    )


def _reapply_local(
    resume_tex: str,
    removals: list[dict],
    rows,
) -> tuple[str, str]:
    """Recompute the tailored resume from the original + checkbox selection. No LLM."""
    if not removals:
        return resume_tex, ""
    accepted = _accepted_ids_from_rows(rows, removals)
    tailored, warnings = _apply_removals(resume_tex, removals, accepted)
    return tailored, _removals_status(removals, warnings)


def _removals_status(removals: list[dict], warnings: list[str]) -> str:
    if not removals:
        return "_No removals were proposed._"
    parts = [f"_{len(removals)} removal(s) proposed. Toggle 'Accept' to apply or skip each._"]
    if warnings:
        parts.append("")
        parts.extend(f"- {w}" for w in warnings)
    return "\n".join(parts)


def _draft_letter(
    tailored_resume: str,
    jd_english: str,
    company: str,
    previous: str = "",
    feedback: str = "",
) -> str:
    if not LETTER_TEMPLATE_PATH.exists():
        raise gr.Error(f"Missing letter template at {LETTER_TEMPLATE_PATH}.")
    company = (company or "").strip()
    if not company:
        raise gr.Error("Fill in the Company field (Step 1) before drafting the letter.")
    template = LETTER_TEMPLATE_PATH.read_text(encoding="utf-8")
    template = template.replace("<<COMPANY>>", company)
    return llm.motivation_letter(
        resume_tex=tailored_resume,
        job_description=jd_english,
        template_tex=template,
        company=company,
        previous_attempt=previous.strip() or None,
        feedback=feedback.strip() or None,
    )


def _redraft_letter(
    tailored_resume: str,
    jd_english: str,
    company: str,
    current_letter: str,
    feedback: str,
) -> str:
    if not current_letter.strip():
        raise gr.Error("Draft a letter first, then add feedback.")
    if not feedback.strip():
        raise gr.Error("Type feedback first, e.g. 'more concrete, fewer adjectives'.")
    return _draft_letter(
        tailored_resume,
        jd_english,
        company,
        previous=current_letter,
        feedback=feedback,
    )


def _extract_meta(jd_english: str) -> tuple[str, str]:
    meta = llm.extract_metadata(jd_english)
    return meta["company"], meta["vacancy"]


def _finalize(
    *,
    company: str,
    vacancy: str,
    tailored_resume: str,
    letter_tex: str,
    jd_english: str,
    jd_original: str,
):
    company = (company or "Unknown").strip()
    vacancy = (vacancy or "Unknown").strip()
    today = date.today()

    final_letter = letter_tex

    folder = storage.application_folder(company, vacancy, today)
    folder.mkdir(parents=True, exist_ok=True)
    resume_stem, letter_stem = _file_stems()
    resume_tex_path = folder / f"{resume_stem}.tex"
    letter_tex_path = folder / f"{letter_stem}.tex"
    resume_tex_path.write_text(tailored_resume, encoding="utf-8")
    letter_tex_path.write_text(final_letter, encoding="utf-8")

    try:
        resume_pdf = latex.compile_tex(resume_tex_path, folder)
        letter_pdf = latex.compile_tex(letter_tex_path, folder)
    except latex.TectonicError as e:
        raise gr.Error(f"{e}\n\n--- tectonic log ---\n{e.log[-4000:]}")

    storage.save_application(
        company=company,
        vacancy=vacancy,
        on_date=today,
        job_description_english=jd_english,
        job_description_original=jd_original,
        resume_tex=tailored_resume,
        letter_tex=final_letter,
        resume_pdf=resume_pdf,
        letter_pdf=letter_pdf,
        resume_stem=resume_stem,
        letter_stem=letter_stem,
    )
    status = f"Saved to `{folder}`."
    return status, str(resume_pdf), str(letter_pdf)


def _search(query: str) -> list[list]:
    rows = storage.search(query)
    return [
        [r["id"], r["processed_at"], r["company"], r["vacancy"], r["folder_path"]]
        for r in rows
    ]


def _delete(app_id, confirm: bool, current_query: str) -> tuple[str, list[list]]:
    if not confirm:
        raise gr.Error("Tick the confirmation box before deleting.")
    try:
        app_id_int = int(app_id)
    except (TypeError, ValueError):
        raise gr.Error("Enter a valid numeric application ID.")
    try:
        deleted = storage.delete_application(app_id_int)
    except LookupError as e:
        raise gr.Error(str(e))
    status = (
        f"Deleted #{deleted['id']} — {deleted['company']} / {deleted['vacancy']} "
        f"(folder: `{deleted['folder_path']}`)."
    )
    return status, _search(current_query)


# ---------- UI ----------

def build_ui() -> gr.Blocks:
    with gr.Blocks(title="Application Manager") as demo:
        gr.Markdown("# Application Manager")

        jd_original_state = gr.State("")
        removals_state = gr.State([])
        original_resume_state = gr.State("")

        with gr.Tab("New application"):
            # ---- Step 0: resume ----
            with gr.Accordion("Step 0 — Choose resume", open=True):
                resume_status = gr.Markdown(_resume_status())
                resume_choice = gr.Radio(
                    ["Use current resume", "Upload new resume"],
                    value="Use current resume",
                    label="Resume source",
                )
                resume_upload = gr.File(label="Upload resume.tex", file_types=[".tex"])
                load_resume_btn = gr.Button("Load resume", variant="primary")
                resume_tex = gr.Textbox(
                    label="Loaded resume (.tex) — read-only",
                    lines=8,
                    interactive=False,
                )

            # ---- Step 1: job description ----
            with gr.Accordion("Step 1 — Job description", open=True):
                jd_input = gr.Textbox(label="Paste the job posting", lines=10)
                process_jd_btn = gr.Button("Translate / accept", variant="primary")
                jd_english = gr.Textbox(
                    label="English job description (editable)",
                    lines=10,
                )
                extract_btn = gr.Button("Auto-fill company & vacancy")
                with gr.Row():
                    company_in = gr.Textbox(label="Company")
                    vacancy_in = gr.Textbox(label="Vacancy title")

            # ---- Step 2: tailored resume ----
            with gr.Accordion("Step 2 — Tailored resume", open=False):
                tailor_btn = gr.Button("Generate tailored resume", variant="primary")
                gr.Markdown(
                    "The model proposes **removals only** — it cannot add or "
                    "rewrite content. Untick any removal you want to keep, then "
                    "press **Apply changes** to rebuild the tailored resume "
                    "below (no LLM call)."
                )
                removals_df = gr.Dataframe(
                    headers=["Accept", "Section", "Removed", "Reason"],
                    datatype=["bool", "str", "str", "str"],
                    interactive=True,
                    wrap=True,
                    column_count=(4, "fixed"),
                    type="array",
                )
                apply_btn = gr.Button("Apply changes", variant="primary")
                omissions_md = gr.Markdown()
                tailored_resume = gr.Textbox(
                    label="Tailored resume (.tex, editable)",
                    lines=20,
                )
                resume_feedback = gr.Textbox(
                    label="Feedback for regeneration",
                    placeholder="e.g. also drop the hobbies section; "
                    "keep the open-source projects",
                    lines=3,
                )
                retailor_btn = gr.Button("Regenerate with feedback")

            # ---- Step 3: motivation letter ----
            with gr.Accordion("Step 3 — Motivation letter", open=False):
                draft_letter_btn = gr.Button("Draft motivation letter", variant="primary")
                letter_tex = gr.Textbox(
                    label="Motivation letter (.tex, editable)",
                    lines=20,
                )
                letter_feedback = gr.Textbox(
                    label="Feedback for regeneration",
                    placeholder="e.g. more concrete impact numbers; warmer opening; "
                    "shorter closing paragraph",
                    lines=3,
                )
                redraft_btn = gr.Button("Regenerate with feedback")

            # ---- Step 4: finalize ----
            with gr.Accordion("Step 4 — Finalize & compile", open=False):
                compile_btn = gr.Button("Compile & save", variant="primary")
                finalize_status = gr.Markdown()
                resume_pdf_out = gr.File(label="Resume PDF")
                letter_pdf_out = gr.File(label="Letter PDF")

            new_application_btn = gr.Button(
                "Start new application", variant="primary"
            )

        with gr.Tab("Search"):
            search_input = gr.Textbox(
                label="Search past applications (FTS5; empty = all)",
                placeholder='e.g. "python data" or "company:Acme"',
            )
            search_btn = gr.Button("Search")
            results_table = gr.Dataframe(
                headers=["ID", "Date", "Company", "Vacancy", "Folder"],
                interactive=False,
                wrap=True,
            )

            gr.Markdown("### Delete an application")
            gr.Markdown(
                "Copy the **ID** from the results above. This removes the "
                "database row **and** deletes the application folder from disk."
            )
            with gr.Row():
                delete_id = gr.Number(label="Application ID", precision=0)
                delete_confirm = gr.Checkbox(
                    label="I understand this is permanent",
                    value=False,
                )
            delete_btn = gr.Button("Delete application", variant="stop")
            delete_status = gr.Markdown()

        # ---- wiring ----
        def on_load_resume(uploaded_file, choice):
            tex, status = _load_resume_action(uploaded_file, choice)
            return tex, status, tex

        load_resume_btn.click(
            on_load_resume,
            inputs=[resume_upload, resume_choice],
            outputs=[resume_tex, resume_status, original_resume_state],
        )

        def on_process_jd(raw):
            english = _process_jd(raw)
            return english, raw  # english into editable box, raw into state

        process_jd_btn.click(
            on_process_jd,
            inputs=[jd_input],
            outputs=[jd_english, jd_original_state],
        )

        tailor_btn.click(
            _tailor,
            inputs=[resume_tex, jd_english],
            outputs=[tailored_resume, removals_state, removals_df, omissions_md],
        )
        retailor_btn.click(
            _retailor,
            inputs=[resume_tex, jd_english, removals_state, resume_feedback],
            outputs=[tailored_resume, removals_state, removals_df, omissions_md],
        )
        apply_btn.click(
            _reapply_local,
            inputs=[original_resume_state, removals_state, removals_df],
            outputs=[tailored_resume, omissions_md],
        )

        draft_letter_btn.click(
            _draft_letter,
            inputs=[tailored_resume, jd_english, company_in],
            outputs=[letter_tex],
        )
        redraft_btn.click(
            _redraft_letter,
            inputs=[tailored_resume, jd_english, company_in, letter_tex, letter_feedback],
            outputs=[letter_tex],
        )

        extract_btn.click(
            _extract_meta,
            inputs=[jd_english],
            outputs=[company_in, vacancy_in],
        )

        def on_compile(company, vacancy, tres, lt, jde, jdo):
            return _finalize(
                company=company,
                vacancy=vacancy,
                tailored_resume=tres,
                letter_tex=lt,
                jd_english=jde,
                jd_original=jdo or jde,
            )

        compile_btn.click(
            on_compile,
            inputs=[
                company_in,
                vacancy_in,
                tailored_resume,
                letter_tex,
                jd_english,
                jd_original_state,
            ],
            outputs=[finalize_status, resume_pdf_out, letter_pdf_out],
        )

        search_btn.click(_search, inputs=[search_input], outputs=[results_table])

        delete_btn.click(
            _delete,
            inputs=[delete_id, delete_confirm, search_input],
            outputs=[delete_status, results_table],
        )

        def _reset_application():
            return (
                "",   # jd_input
                "",   # jd_english
                "",   # jd_original_state
                "",   # tailored_resume
                [],   # removals_state
                [],   # removals_df
                "",   # omissions_md
                "",   # resume_feedback
                "",   # letter_tex
                "",   # letter_feedback
                "",   # company_in
                "",   # vacancy_in
                "",   # finalize_status
                None, # resume_pdf_out
                None, # letter_pdf_out
            )

        new_application_btn.click(
            _reset_application,
            inputs=[],
            js="() => { window.scrollTo({ top: 0, behavior: 'smooth' }); }",
            outputs=[
                jd_input,
                jd_english,
                jd_original_state,
                tailored_resume,
                removals_state,
                removals_df,
                omissions_md,
                resume_feedback,
                letter_tex,
                letter_feedback,
                company_in,
                vacancy_in,
                finalize_status,
                resume_pdf_out,
                letter_pdf_out,
            ],
        )

    return demo


def main() -> None:
    latex.ensure_tectonic()  # fail fast with a helpful message
    build_ui().launch()


if __name__ == "__main__":
    main()
