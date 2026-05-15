"""SQLite + FTS5 storage for processed applications."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "app.db"
APPS_DIR = ROOT / "applications"


SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company TEXT NOT NULL,
  vacancy TEXT NOT NULL,
  processed_at TEXT NOT NULL,
  folder_path TEXT NOT NULL,
  job_description TEXT NOT NULL,
  resume_tex TEXT NOT NULL,
  letter_tex TEXT NOT NULL,
  meta_json TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS applications_fts USING fts5(
  company, vacancy, job_description, resume_tex, letter_tex,
  content='applications', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS applications_ai AFTER INSERT ON applications BEGIN
  INSERT INTO applications_fts(rowid, company, vacancy, job_description, resume_tex, letter_tex)
  VALUES (new.id, new.company, new.vacancy, new.job_description, new.resume_tex, new.letter_tex);
END;

CREATE TRIGGER IF NOT EXISTS applications_ad AFTER DELETE ON applications BEGIN
  INSERT INTO applications_fts(applications_fts, rowid, company, vacancy, job_description, resume_tex, letter_tex)
  VALUES ('delete', old.id, old.company, old.vacancy, old.job_description, old.resume_tex, old.letter_tex);
END;
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    return re.sub(r"[\s_-]+", "-", s) or "unknown"


def application_folder(company: str, vacancy: str, on_date: date) -> Path:
    slug = f"{on_date.isoformat()}_{_slugify(company)}_{_slugify(vacancy)}"
    return APPS_DIR / slug


def save_application(
    *,
    company: str,
    vacancy: str,
    on_date: date,
    job_description_english: str,
    job_description_original: str,
    resume_tex: str,
    letter_tex: str,
    resume_pdf: Path,
    letter_pdf: Path,
) -> Path:
    """Persist artifacts on disk and index in the DB. Returns folder path."""
    folder = application_folder(company, vacancy, on_date)
    folder.mkdir(parents=True, exist_ok=True)

    (folder / "resume.tex").write_text(resume_tex, encoding="utf-8")
    (folder / "letter.tex").write_text(letter_tex, encoding="utf-8")
    (folder / "job_description.txt").write_text(job_description_original, encoding="utf-8")
    if job_description_english != job_description_original:
        (folder / "job_description.en.txt").write_text(job_description_english, encoding="utf-8")

    # Copy PDFs into the canonical folder if they were produced elsewhere.
    for pdf in (resume_pdf, letter_pdf):
        target = folder / pdf.name
        if pdf.resolve() != target.resolve():
            target.write_bytes(pdf.read_bytes())

    meta = {
        "company": company,
        "vacancy": vacancy,
        "processed_at": on_date.isoformat(),
    }
    (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    with closing(connect()) as conn, conn:
        conn.execute(
            """INSERT INTO applications
               (company, vacancy, processed_at, folder_path,
                job_description, resume_tex, letter_tex, meta_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company,
                vacancy,
                on_date.isoformat(),
                str(folder),
                job_description_english,
                resume_tex,
                letter_tex,
                json.dumps(meta),
            ),
        )
    return folder


def delete_application(app_id: int) -> dict:
    """Remove an application row and its on-disk folder.

    Returns a dict with the deleted row's metadata. Raises LookupError if the
    id is not found.
    """
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT id, company, vacancy, processed_at, folder_path "
            "FROM applications WHERE id = ?",
            (app_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"No application with id={app_id}.")
        folder = Path(row["folder_path"])
        # Only remove the folder if it's inside our managed APPS_DIR — guards
        # against acting on a hand-edited row that points somewhere unexpected.
        try:
            folder.resolve().relative_to(APPS_DIR.resolve())
            inside_apps_dir = True
        except ValueError:
            inside_apps_dir = False
        if inside_apps_dir and folder.exists():
            shutil.rmtree(folder)
        conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
    return dict(row)


def search(query: str, limit: int = 50) -> list[dict]:
    """Full-text search across stored applications."""
    if not query.strip():
        with closing(connect()) as conn:
            rows = conn.execute(
                "SELECT id, company, vacancy, processed_at, folder_path "
                "FROM applications ORDER BY processed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    else:
        with closing(connect()) as conn:
            rows = conn.execute(
                """SELECT a.id, a.company, a.vacancy, a.processed_at, a.folder_path
                   FROM applications_fts f
                   JOIN applications a ON a.id = f.rowid
                   WHERE applications_fts MATCH ?
                   ORDER BY a.processed_at DESC LIMIT ?""",
                (query, limit),
            ).fetchall()
    return [dict(r) for r in rows]
