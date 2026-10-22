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

# Stored key -> label shown in the UI. Keys are persisted, so never rename them.
STATUSES = {
    "no_response_or_rejected": "Haven't heard back / rejected",
    "rejected_after_first_interview": "Rejected after first interview",
    "rejected_after_second_interview": "Rejected after second interview",
    "negotiations_broke_down": "Negotiations broke down",
    "accepted": "Accepted",
}
DEFAULT_STATUS = "no_response_or_rejected"

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


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release. Only ever adds, never rewrites."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(applications)")}
    if "status" not in columns:
        conn.execute(
            "ALTER TABLE applications ADD COLUMN status TEXT NOT NULL "
            f"DEFAULT '{DEFAULT_STATUS}'"
        )
        conn.commit()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    return re.sub(r"[\s_-]+", "-", s) or "unknown"


def application_folder(company: str, vacancy: str, on_date: date) -> Path:
    return APPS_DIR / f"{on_date.isoformat()}_{_slugify(company)}_{_slugify(vacancy)}"


def _inside_apps_dir(folder: Path) -> bool:
    """Guards file operations against hand-edited rows pointing elsewhere."""
    try:
        folder.resolve().relative_to(APPS_DIR.resolve())
        return True
    except ValueError:
        return False


def save_application(
    *,
    folder: Path,
    company: str,
    vacancy: str,
    on_date: date,
    job_description_english: str,
    job_description_original: str,
    resume_tex: str,
    letter_tex: str,
) -> None:
    """Write the job description and meta.json next to the compiled files, and index the row."""
    (folder / "job_description.txt").write_text(job_description_original, encoding="utf-8")
    if job_description_english != job_description_original:
        (folder / "job_description.en.txt").write_text(job_description_english, encoding="utf-8")

    meta = {
        "company": company,
        "vacancy": vacancy,
        "processed_at": on_date.isoformat(),
        "status": DEFAULT_STATUS,
    }
    (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    with closing(connect()) as conn, conn:
        conn.execute(
            """INSERT INTO applications
               (company, vacancy, processed_at, folder_path,
                job_description, resume_tex, letter_tex, meta_json, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                company,
                vacancy,
                on_date.isoformat(),
                str(folder),
                job_description_english,
                resume_tex,
                letter_tex,
                json.dumps(meta),
                DEFAULT_STATUS,
            ),
        )


def set_status(app_id: int, status: str) -> dict:
    """Update an application's status in the DB and its meta.json. Raises LookupError/ValueError."""
    if status not in STATUSES:
        raise ValueError(f"Unknown status {status!r}.")
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT id, company, vacancy, folder_path, meta_json FROM applications WHERE id = ?",
            (app_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"No application with id={app_id}.")
        meta = json.loads(row["meta_json"] or "{}")
        meta["status"] = status
        conn.execute(
            "UPDATE applications SET status = ?, meta_json = ? WHERE id = ?",
            (status, json.dumps(meta), app_id),
        )
    meta_path = Path(row["folder_path"]) / "meta.json"
    if _inside_apps_dir(meta_path.parent) and meta_path.exists():
        on_disk = json.loads(meta_path.read_text(encoding="utf-8"))
        on_disk["status"] = status
        meta_path.write_text(json.dumps(on_disk, indent=2), encoding="utf-8")
    return dict(row)


def delete_application(app_id: int) -> dict:
    """Remove an application row and its folder. Raises LookupError if the id is unknown."""
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT id, company, vacancy, processed_at, folder_path "
            "FROM applications WHERE id = ?",
            (app_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"No application with id={app_id}.")
        folder = Path(row["folder_path"])
        if _inside_apps_dir(folder) and folder.exists():
            shutil.rmtree(folder)
        conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
    return dict(row)


def _shingles(text: str, n: int = 3) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i + n]) for i in range(max(len(words) - n + 1, 1))}


def _containment(a: set, b: set) -> float:
    """Share of the smaller shingle set found in the other; tolerant of job-board boilerplate."""
    return len(a & b) / min(len(a), len(b)) if a and b else 0.0


def find_similar(texts: list[str], threshold: float) -> list[dict]:
    """Stored applications whose job description matches any of texts at >= threshold, best first.

    Compares against the stored English text and, when present, the original-language
    job_description.txt, so translated postings match on either version.
    """
    probes = [_shingles(t) for t in texts if t.strip()]
    if not probes:
        return []
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id, company, vacancy, processed_at, folder_path, status, job_description FROM applications"
        ).fetchall()
    matches = []
    for row in rows:
        stored = [row["job_description"]]
        original = Path(row["folder_path"]) / "job_description.txt"
        if _inside_apps_dir(original.parent) and original.exists():
            stored.append(original.read_text(encoding="utf-8"))
        score = max(_containment(p, _shingles(s)) for p in probes for s in stored)
        if score >= threshold:
            match = {k: row[k] for k in ("id", "company", "vacancy", "processed_at", "status")}
            matches.append(match | {"score": score})
    return sorted(matches, key=lambda m: m["score"], reverse=True)


def search(query: str, limit: int = 50) -> list[dict]:
    """Full-text search across stored applications; an empty query lists the newest."""
    columns = "a.id, a.company, a.vacancy, a.processed_at, a.folder_path, a.status"
    with closing(connect()) as conn:
        if not query.strip():
            rows = conn.execute(
                f"SELECT {columns} FROM applications a "
                "ORDER BY a.processed_at DESC, a.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT {columns}
                   FROM applications_fts f
                   JOIN applications a ON a.id = f.rowid
                   WHERE applications_fts MATCH ?
                   ORDER BY a.processed_at DESC, a.id DESC LIMIT ?""",
                (query, limit),
            ).fetchall()
    return [dict(r) for r in rows]
