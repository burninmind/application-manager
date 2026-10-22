"""Claude API wrappers, one function per pipeline step."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from anthropic import Anthropic, APIStatusError

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Per-step model and effort (low | medium | high | xhigh | max); override in .env.
MODEL_TRANSLATE = os.environ.get("CLAUDE_MODEL_TRANSLATE", "claude-haiku-4-5")
MODEL_TAILOR = os.environ.get("CLAUDE_MODEL_TAILOR", "claude-sonnet-5-5")
MODEL_LETTER = os.environ.get("CLAUDE_MODEL_LETTER", "claude-sonnet-5-5")
MODEL_METADATA = os.environ.get("CLAUDE_MODEL_METADATA", "claude-haiku-4-5")
EFFORT_TAILOR = os.environ.get("CLAUDE_EFFORT_TAILOR", "high")
EFFORT_LETTER = os.environ.get("CLAUDE_EFFORT_LETTER", "high")
EFFORT_TRANSLATE = os.environ.get("CLAUDE_EFFORT_TRANSLATE", "low")
EFFORT_METADATA = os.environ.get("CLAUDE_EFFORT_METADATA", "low")

# Opus 4.7+ and all 5-series models reject non-default sampling, so this is an allowlist.
_TEMPERATURE_OK_RE = re.compile(r"^claude-(haiku-4-5|sonnet-4-[56]|opus-4-[56])")
# Haiku 4.5 and Sonnet 4.5 reject `effort`.
_EFFORT_OK_RE = re.compile(r"^claude-(opus-4-[5-9]|sonnet-4-6|opus-5|sonnet-5|haiku-5|fable-5|mythos-5)")
# Models with safety classifiers: retry a refusal on another model server-side.
_FALLBACK_RE = re.compile(r"^claude-(opus-5|sonnet-5-5|fable-5)")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

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
    effort: str | None = None,
    max_tokens: int = 32000,
) -> str:
    """One streamed Claude call with retry on 429/529. Returns the concatenated text blocks.

    Streams because 5-series thinking counts toward max_tokens, and large non-streaming
    max_tokens trips the SDK timeout guard.
    """
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_blocks}],
    }
    if _TEMPERATURE_OK_RE.match(model):
        kwargs["temperature"] = temperature
    if effort and _EFFORT_OK_RE.match(model):
        kwargs["output_config"] = {"effort": effort}
    if _FALLBACK_RE.match(model):
        kwargs["betas"] = [_FALLBACK_BETA]
        kwargs["extra_body"] = {"fallbacks": "default"}  # not typed by the pinned SDK yet
    for attempt in range(4):
        try:
            with client().beta.messages.stream(**kwargs) as stream:
                resp = stream.get_final_message()
            break
        except APIStatusError as e:
            if e.status_code in (429, 529) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
    if resp.stop_reason == "refusal":
        details = getattr(resp, "stop_details", None)
        raise RuntimeError(
            f"{model} declined the request (category: {getattr(details, 'category', None)})."
        )
    if resp.stop_reason == "max_tokens":
        raise RuntimeError(f"{model} hit max_tokens={max_tokens}; output was truncated.")
    # Thinking and fallback blocks can precede the text.
    return "".join(b.text for b in resp.content if b.type == "text")


def translate_job_description(text: str) -> str:
    out = _call(
        system=_load_prompt("translate"),
        user_blocks=[{"type": "text", "text": text}],
        model=MODEL_TRANSLATE,
        temperature=0.1,
        effort=EFFORT_TRANSLATE,
    )
    return out.strip()


def tailor_resume(
    resume_tex: str,
    job_description: str,
    *,
    max_projects: int,
    max_publications: int,
    previous: dict | None = None,
    feedback: str | None = None,
) -> dict:
    """Return {"summary": str, "removals": [{id, category, section, reason, removed_text, context}]}.

    `summary` is a new LaTeX paragraph that replaces the long summary. Each removal's
    `removed_text` (and `context`, when set) is a verbatim substring of resume_tex.
    `previous` is an earlier result to revise according to `feedback`.
    """
    user_text = (
        f"<limits>\nmax_projects: {max_projects}\nmax_publications: {max_publications}\n</limits>\n\n"
        f"<job_description>\n{job_description}\n</job_description>\n\n"
        f"<resume_tex>\n{resume_tex}\n</resume_tex>"
    )
    if previous:
        user_text += f"\n\n<previous_result>\n{json.dumps(previous, indent=2)}\n</previous_result>"
    if feedback:
        user_text += f"\n\n<user_feedback>\n{feedback}\n</user_feedback>"
    raw = _call(
        system=_load_prompt("tailor_resume"),
        # Cached so regenerations with feedback reuse the prompt.
        user_blocks=[{"type": "text", "text": user_text, "cache_control": {"type": "ephemeral"}}],
        model=MODEL_TAILOR,
        temperature=0.2,
        effort=EFFORT_TAILOR,
    )
    body = _extract_fenced(raw, "json") or raw
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        # Models often emit LaTeX like \item with one backslash, which is invalid JSON.
        try:
            data = json.loads(_repair_latex_json(body))
        except json.JSONDecodeError as e:
            raise ValueError(f"Model did not return valid JSON: {e}\n\n{raw[:2000]}")
    if not isinstance(data, dict) or not isinstance(data.get("removals"), list):
        raise ValueError(f"Expected an object with 'summary' and 'removals':\n\n{raw[:2000]}")

    removals = []
    for i, r in enumerate(x for x in data["removals"] if isinstance(x, dict)):
        removals.append({
            "id": str(r.get("id") or f"r{i + 1}"),
            "category": str(r.get("category") or "other").strip().lower(),
            "section": str(r.get("section") or "").strip(),
            "reason": str(r.get("reason") or "").strip(),
            "removed_text": str(r.get("removed_text") or ""),
            "context": str(r.get("context") or ""),
        })
    return {"summary": escape_latex_specials(str(data.get("summary") or "").strip()), "removals": removals}


def motivation_letter(
    *,
    resume_tex: str,
    long_summary: str,
    job_description: str,
    template_tex: str,
    company: str,
    previous_attempt: str | None = None,
    feedback: str | None = None,
) -> str:
    """Return the complete letter .tex, with the template's <<BODY>> filled in."""
    user_text = (
        f"<company>{company}</company>\n\n"
        f"<job_description>\n{job_description}\n</job_description>\n\n"
        f"<candidate_story>\n{long_summary}\n</candidate_story>\n\n"
        f"<resume_tex>\n{resume_tex}\n</resume_tex>\n\n"
        f"<letter_template>\n{template_tex}\n</letter_template>"
    )
    if previous_attempt:
        user_text += f"\n\n<previous_attempt>\n{previous_attempt}\n</previous_attempt>"
    if feedback:
        user_text += f"\n\n<user_feedback>\n{feedback}\n</user_feedback>"
    raw = _call(
        system=_load_prompt("motivation_letter"),
        user_blocks=[{"type": "text", "text": user_text}],
        model=MODEL_LETTER,
        temperature=0.7,
        effort=EFFORT_LETTER,
    )
    tex = _extract_fenced(raw, "latex") or _extract_fenced(raw, "tex") or raw
    return tex.strip()


def extract_metadata(job_description: str) -> dict:
    """Return {'company': str, 'vacancy': str}, with 'Unknown' for missing fields."""
    raw = _call(
        system=_load_prompt("extract_metadata"),
        user_blocks=[{"type": "text", "text": job_description}],
        model=MODEL_METADATA,
        temperature=0.0,
        effort=EFFORT_METADATA,
        max_tokens=8000,  # thinking counts toward the cap on 5-series models
    )
    try:
        data = json.loads(_extract_fenced(raw, "json") or raw)
    except json.JSONDecodeError:
        data = {}
    return {
        "company": (data.get("company") or "Unknown").strip(),
        "vacancy": (data.get("vacancy") or "Unknown").strip(),
    }


_FENCE_RE = re.compile(r"```(?P<lang>\w+)?\s*\n(?P<body>.*?)```", re.DOTALL)
_VALID_JSON_ESCAPES = set('"\\/bfnrtu')


def _repair_latex_json(s: str) -> str:
    r"""Double every backslash that doesn't start a valid JSON escape (e.g. `\item` -> `\\item`)."""
    def fix(m: re.Match) -> str:
        return m.group(0) if m.group(1) in _VALID_JSON_ESCAPES else "\\\\" + m.group(1)
    return re.sub(r"\\(.)", fix, s, flags=re.DOTALL)


def escape_latex_specials(s: str) -> str:
    """Escape bare &, % and # so generated prose can't break or truncate a LaTeX line."""
    return re.sub(r"(?<!\\)([&%#])", r"\\\1", s)


def _extract_fenced(text: str, lang: str) -> str | None:
    for m in _FENCE_RE.finditer(text):
        if (m.group("lang") or "").lower() == lang.lower():
            return m.group("body")
    return None
