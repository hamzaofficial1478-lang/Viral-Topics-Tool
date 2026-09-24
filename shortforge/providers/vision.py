"""R7 — production vision adapter (image content blocks).

Scores sampled video frames with a vision LLM (e.g.
``meta/llama-3.2-11b-vision-instruct`` on the NVIDIA build endpoint) by sending
OpenAI-style chat-completions where the message content carries ``image_url``
blocks. Frame sampling → JPEG → base64 data URL → batched request.

The request goes through the SAME shared client (:func:`shortforge.llm.openai_chat_raw`)
and URL builder as the probe, so the two can never drift (the /v1/v1 rule).

Endpoint limits (image count per request, max resolution) vary by provider and
must be confirmed live — both are configurable here (``vision.max_images`` /
``vision.frame_width``); the batching respects the cap.
"""

from __future__ import annotations

import base64
import os

from ..llm import openai_chat_raw
from ..utils import log, require_binary, run

_DEFAULT_MAX_IMAGES = 6      # images per request (configurable; confirm real limit live)
_DEFAULT_FRAME_WIDTH = 768   # downscale cap so requests stay small


def sample_frames(video_path: str, start: float, end: float, n: int, out_dir: str,
                  frame_width: int = _DEFAULT_FRAME_WIDTH) -> list[str]:
    """Extract ``n`` evenly-spaced JPEG frames from ``[start, end]`` via ffmpeg.

    ffmpeg ``-ss`` seeking is used (reliable, unlike the cv2 msec seek), each
    frame downscaled to at most ``frame_width`` px wide to keep the payload small.
    """
    ffmpeg = require_binary("ffmpeg")
    os.makedirs(out_dir, exist_ok=True)
    dur = max(0.1, end - start)
    n = max(1, n)
    paths: list[str] = []
    for i in range(n):
        t = start + dur * (i + 0.5) / n          # centre of each slice
        p = os.path.join(out_dir, f"frame_{i:02d}.jpg")
        run([ffmpeg, "-y", "-ss", f"{t:.3f}", "-i", video_path, "-frames:v", "1",
             "-vf", f"scale='min({int(frame_width)},iw)':-2", "-q:v", "5", p])
        if os.path.isfile(p):
            paths.append(p)
    return paths


def encode_image(path: str) -> str:
    """A frame as a base64 ``data:`` URL (JPEG)."""
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def build_vision_messages(image_urls: list[str], prompt: str) -> list:
    """One user turn: the instruction text followed by N image blocks."""
    content = [{"type": "text", "text": prompt}]
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    return [{"role": "user", "content": content}]


def score_frames(model: dict, image_paths: list[str], prompt: str, *,
                 max_images: int = _DEFAULT_MAX_IMAGES, max_tokens: int = 512,
                 timeout: int = 120) -> list[dict]:
    """Send frames (batched to ``max_images`` per request) to the vision model.

    ``model`` is a flattened store model dict (base_url, api_key, model). Returns
    one raw result per batch: {status, body, error, url, model, images} — so the
    caller (C1) can parse scores and log/record diagnostics + any failover.
    """
    urls = [encode_image(p) for p in image_paths if os.path.isfile(p)]
    if not urls:
        return []
    import time
    results: list[dict] = []
    for i in range(0, len(urls), max(1, max_images)):
        batch = urls[i:i + max(1, max_images)]
        msgs = build_vision_messages(batch, prompt)
        t0 = time.time()
        r = openai_chat_raw(model.get("base_url", ""), model.get("api_key", ""),
                            model.get("model", ""), msgs, max_tokens=max_tokens, timeout=timeout,
                            auth_style=model.get("auth_style", "bearer"),
                            auth_header_name=model.get("auth_header_name"))
        r["images"] = len(batch)
        results.append(r)
        log.info("vision %s: %d frame(s) -> HTTP %s in %.1fs (timeout %ds)",
                 model.get("model", "?"), len(batch), r.get("status"), time.time() - t0, timeout)
    return results
