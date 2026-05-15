"""Claude API wrappers for the four LLM steps."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from anthropic import Anthropic, APIStatusError

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Per-step model selection. Defaults aim for the cheapest model that does each
# job well; override any of these in .env to tune cost vs. quality.
MODEL_TRANSLATE = os.environ.get("CLAUDE_MODEL_TRANSLATE", "claude-haiku-4-5-20251001")
MODEL_TAILOR = os.environ.get("CLAUDE_MODEL_TAILOR", "claude-sonnet-4-6")
MODEL_LETTER = os.environ.get("CLAUDE_MODEL_LETTER", "claude-sonnet-4-6")
MODEL_METADATA = os.environ.get("CLAUDE_MODEL_METADATA", "claude-haiku-4-5-20251001")

_client: Anthropic | None = None


def client() -> Anthropic:
    global _client
    if _client is None:
        _client = Anthropic()
    return _client


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _call(
    *,
    system: str,
    user_blocks: list[dict],
    model: str,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> str:
    """Single Claude call with simple retry on 429/529."""
    for attempt in range(4):
        try:
            resp = client().messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": user_blocks}],
            )
            return "".join(b.text for b in resp.content if b.type == "text")
        except APIStatusError as e:
            if e.status_code in (429, 529) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("unreachable")


def translate_job_description(text: str) -> str:
    """Return English version. If already English, returns text unchanged via caller."""
    system = _load_prompt("translate")
    out = _call(
        system=system,
        user_blocks=[{"type": "text", "text": text}],
        model=MODEL_TRANSLATE,
        temperature=0.1,
    )
    return out.strip()


def tailor_resume(
    resume_tex: str,
    job_description: str,
    *,
    previous_removals: list[dict] | None = None,
    feedback: str | None = None,
) -> list[dict]:
    """Return a list of proposed removals.

    Each removal is {"id": str, "section": str, "reason": str, "removed_text": str},
    where removed_text is a verbatim substring of resume_tex. The caller applies
    accepted removals locally via string deletion — no further LLM call needed.

    If previous_removals and/or feedback are supplied, the model revises the
    proposed list to incorporate the feedback.
    """
    system = _load_prompt("tailor_resume")
    user_text = (
        f"<job_description>\n{job_description}\n</job_description>\n\n"
        f"<resume_tex>\n{resume_tex}\n</resume_tex>"
    )
    if previous_removals:
        user_text += (
            "\n\n<previous_removals>\n"
            + json.dumps(previous_removals, indent=2)
            + "\n</previous_removals>"
        )
    if feedback:
        user_text += f"\n\n<user_feedback>\n{feedback}\n</user_feedback>"
    raw = _call(
        system=system,
        user_blocks=[
            # Cache the resume — reused across regenerations and step 3.
            {"type": "text", "text": user_text, "cache_control": {"type": "ephemeral"}},
        ],
        model=MODEL_TAILOR,
        temperature=0.2,
        max_tokens=8192,
    )
    removals_raw = _extract_fenced(raw, "json") or raw
    try:
        removals = json.loads(removals_raw)
    except json.JSONDecodeError:
        # LaTeX-in-JSON often has lone backslashes (e.g. \item, \hfill) that
        # aren't valid JSON string escapes. Repair and retry.
        try:
            removals = json.loads(_repair_latex_json(removals_raw))
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Model did not return valid JSON removals: {e}\n\n{raw[:2000]}"
            )
    if not isinstance(removals, list):
        raise ValueError(f"Removals JSON is not a list:\n\n{raw[:2000]}")
    normalized: list[dict] = []
    for i, r in enumerate(removals):
        if not isinstance(r, dict):
            continue
        normalized.append({
            "id": str(r.get("id") or f"r{i+1}"),
            "section": str(r.get("section") or "").strip(),
            "reason": str(r.get("reason") or "").strip(),
            "removed_text": str(r.get("removed_text") or ""),
        })
    return normalized


def motivation_letter(
    *,
    resume_tex: str,
    job_description: str,
    template_tex: str,
    company: str,
    previous_attempt: str | None = None,
    feedback: str | None = None,
) -> str:
    """Return a complete .tex letter by filling the template.

    If previous_attempt and/or feedback are supplied, the model revises the
    previous attempt to incorporate the feedback.
    """
    system = _load_prompt("motivation_letter")
    user_text = (
        f"<company>{company}</company>\n\n"
        f"<job_description>\n{job_description}\n</job_description>\n\n"
        f"<resume_tex>\n{resume_tex}\n</resume_tex>\n\n"
        f"<letter_template>\n{template_tex}\n</letter_template>"
    )
    if previous_attempt:
        user_text += f"\n\n<previous_attempt>\n{previous_attempt}\n</previous_attempt>"
    if feedback:
        user_text += f"\n\n<user_feedback>\n{feedback}\n</user_feedback>"
    raw = _call(
        system=system,
        user_blocks=[{"type": "text", "text": user_text}],
        model=MODEL_LETTER,
        temperature=0.7,
        max_tokens=4096,
    )
    tex = _extract_fenced(raw, "latex") or _extract_fenced(raw, "tex") or raw
    return tex.strip()


def extract_metadata(job_description: str) -> dict:
    """Return {'company': str, 'vacancy': str}. Uses cheap model."""
    system = _load_prompt("extract_metadata")
    raw = _call(
        system=system,
        user_blocks=[{"type": "text", "text": job_description}],
        model=MODEL_METADATA,
        temperature=0.0,
        max_tokens=512,
    )
    js = _extract_fenced(raw, "json") or raw
    try:
        data = json.loads(js)
    except json.JSONDecodeError:
        data = {}
    return {
        "company": (data.get("company") or "Unknown").strip(),
        "vacancy": (data.get("vacancy") or "Unknown").strip(),
    }


_FENCE_RE = re.compile(r"```(?P<lang>\w+)?\s*\n(?P<body>.*?)```", re.DOTALL)
_VALID_JSON_ESCAPES = set('"\\/bfnrtu')


def _repair_latex_json(s: str) -> str:
    """Escape lone backslashes that aren't part of a valid JSON escape.

    The model often emits LaTeX commands like `\\item` inside JSON strings as
    `\\item` (one backslash), which json.loads rejects. This rewrites each
    `\\X` where X is not a valid JSON escape character into `\\\\X`, preserving
    already-correct escapes like `\\\\`, `\\n`, `\\"`.
    """
    def fix(m: re.Match) -> str:
        nxt = m.group(1)
        if nxt in _VALID_JSON_ESCAPES:
            return m.group(0)
        return "\\\\" + nxt
    return re.sub(r"\\(.)", fix, s, flags=re.DOTALL)


def _extract_fenced(text: str, lang: str) -> str | None:
    for m in _FENCE_RE.finditer(text):
        if (m.group("lang") or "").lower() == lang.lower():
            return m.group("body")
    return None
