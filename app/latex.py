"""Tectonic subprocess wrapper."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class TectonicError(RuntimeError):
    def __init__(self, message: str, log: str):
        super().__init__(message)
        self.log = log


def ensure_tectonic() -> str:
    """Return the tectonic path, or raise with an install hint."""
    path = shutil.which("tectonic")
    if not path:
        raise RuntimeError(
            "tectonic not found on PATH. Install it:\n"
            "  Windows: winget install TectonicTypesetting.Tectonic\n"
            "  macOS:   brew install tectonic\n"
            "  Linux:   cargo install tectonic  (or your distro package)\n"
        )
    return path


def compile_tex(tex_path: Path, outdir: Path) -> Path:
    """Compile tex_path into outdir and return the PDF path. Raises TectonicError with the log."""
    ensure_tectonic()
    outdir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["tectonic", "-X", "compile", "--outdir", str(outdir), str(tex_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    log = f"{proc.stderr or ''}\n{proc.stdout or ''}"
    if proc.returncode != 0:
        raise TectonicError(f"tectonic failed for {tex_path.name} (exit {proc.returncode})", log)
    pdf = outdir / f"{tex_path.stem}.pdf"
    if not pdf.exists():
        raise TectonicError("PDF not produced", log)
    return pdf
