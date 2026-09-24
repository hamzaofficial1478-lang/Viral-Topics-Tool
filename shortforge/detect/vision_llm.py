"""M3++ — Claude-vision multimodal hook scorer (optional).

The "Claude actually watches the video" path (the claude-watch technique): sample
frames — densely over the hook, sparsely over the body — into tiled contact
sheets, then hand Claude the sheets *and* the timestamped transcript so it scores
each segment on what's shown as well as what's said. Needs an API key + a
vision-capable model; gated behind ``detect.vision_llm`` and used only when
available (``detect_hooks`` falls back to the transcript+local-visual path).
"""

from __future__ import annotations

import base64
import glob
import json
import os

from ..config import Config
from ..models import Candidate, Transcript
from ..utils import require_binary, run, log
from . import rubric


def available() -> bool:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def build_contact_sheets(
    source_path: str, duration: float, work_dir: str, cfg: Config
) -> list[str]:
    """Sample frames into tiled contact sheets (dense hook, sparse body)."""
    ffmpeg = require_binary("ffmpeg")
    os.makedirs(work_dir, exist_ok=True)
    hook_secs = float(cfg.get("detect.vision_hook_secs", 15))
    hook_fps = float(cfg.get("detect.vision_hook_fps", 6))
    body_interval = float(cfg.get("detect.vision_body_interval", 3.5))
    max_sheets = int(cfg.get("detect.vision_max_sheets", 6))

    sheets: list[str] = []
    # Hook: first N seconds, dense, tiled 5x6.
    run([
        ffmpeg, "-y", "-t", f"{hook_secs}", "-i", source_path,
        "-vf", f"fps={hook_fps},scale=200:-1,tile=5x6:margin=6:padding=4:color=white",
        "-an", os.path.join(work_dir, "hook_%02d.jpg"),
    ])
    sheets += sorted(glob.glob(os.path.join(work_dir, "hook_*.jpg")))
    # Body: sparse across the whole video, tiled 5x6.
    run([
        ffmpeg, "-y", "-i", source_path,
        "-vf", f"fps=1/{body_interval},scale=360:-1,tile=5x6:margin=6:padding=4:color=white",
        "-an", os.path.join(work_dir, "body_%02d.jpg"),
    ])
    sheets += sorted(glob.glob(os.path.join(work_dir, "body_*.jpg")))
    return sheets[:max_sheets]


def _image_block(path: str) -> dict:
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
    }


def detect(transcript: Transcript, source_path: str, cfg: Config) -> list[Candidate]:
    """Score each transcript segment multimodally. Raises on failure (caller falls back)."""
    import anthropic

    client = anthropic.Anthropic()
    model = cfg.get("detect.llm_model", "claude-opus-4-8")
    work = os.path.join(cfg.get("paths.work_dir", ".shortforge"), "vision")
    sheets = build_contact_sheets(source_path, transcript.duration, work, cfg)

    listing = "\n".join(
        f"{i}\t[{s.start:.1f}-{s.end:.1f}] {s.text}"
        for i, s in enumerate(transcript.segments)
    )
    schema = {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "score": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["index", "score", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["scores"],
        "additionalProperties": False,
    }
    prompt = (
        f"{rubric.LLM_RUBRIC}\n\n"
        "You are also shown contact sheets of sampled frames (dense over the "
        "first ~15s hook, sparse over the body). Read the frames like a flipbook "
        "alongside the transcript: reward moments that are strong on screen too "
        "(a visual hook, on-screen text, a reaction, a fast cut, a payoff), not "
        "just in the words. Score each numbered segment 0.0-1.0.\n\n"
        f"Segments (index<TAB>[start-end] text):\n{listing}"
    )
    content: list[dict] = [{"type": "text", "text": prompt}]
    content += [_image_block(p) for p in sheets]

    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
    )
    data = json.loads(next((b.text for b in resp.content if b.type == "text"), "{}"))
    scores = {int(x["index"]): (max(0.0, min(1.0, float(x["score"]))), str(x.get("reason", "")))
              for x in data.get("scores", [])}

    candidates: list[Candidate] = []
    for i, seg in enumerate(transcript.segments):
        score, reason = scores.get(i, (0.0, ""))
        candidates.append(Candidate(
            start=seg.start, end=seg.end, score=score,
            reason=reason or "scored by Claude (vision)",
            signals={"backend": "vision_llm"},
        ))
    candidates.sort(key=lambda c: c.score, reverse=True)
    log.info("Claude-vision scored %d segments over %d contact sheets",
             len(candidates), len(sheets))
    return candidates
