# ShortForge — Phase 1 (core clipper)

Turn **your own** long-form videos into short vertical clips.

```
ingest ─▶ transcribe ─▶ detect hooks ─▶ select clips ─▶ reframe 9:16
       ─▶ burn captions ─▶ render ─▶ manifest.json
```

Phase 1 is captions-only (no dubbing/translation yet — that is Phase 3). It runs
headless from the CLI and produces watchable vertical clips from one source
video, plus a `manifest.json` describing each clip.

> **The one hard rule:** source content must be the operator's own channels or
> otherwise licensed. Ingestion refuses to run without `--owner-confirmed`.

## Install

```bash
# System dependency (provides ffmpeg + ffprobe):
sudo apt-get install -y ffmpeg      # or: brew install ffmpeg

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# optional, for Claude-backed hook detection:
pip install "anthropic>=0.40.0"     # then set ANTHROPIC_API_KEY
```

## Use

Interactive wizard (Section 7 order):

```bash
python cli.py wizard
```

Non-interactive:

```bash
# From a local file you own:
python cli.py run ./my_talk.mp4 --owner-confirmed \
    --duration 45 --num-clips 5 --aspect 9:16 --output out

# From one of your own YouTube videos:
python cli.py run "https://youtu.be/XXXX" --owner-confirmed
```

Key flags: `--duration`, `--tolerance`, `--num-clips` (0 = auto-recommend),
`--aspect` (`9:16` / `1:1` / `16:9` / `WxH`), `--fill` (`crop` / `blur`),
`--no-captions`, `--whisper-model`, `--transcript file.(srt|json)`,
`--no-llm` / `--use-llm`, `--work-dir`, `--output`. See `python cli.py run --help`.

## What each module does (Section 3)

| Dir | Module | Phase 1 scope |
|-----|--------|---------------|
| `ingest/`   | M1 | yt-dlp download (+ auth) or local file; ownership gate |
| `analyze/`  | M2 | audio extract + faster-whisper word-level transcript (cached) |
| `detect/`   | M3 | hook scoring — Claude rubric if available, else keyword heuristic |
| `select/`   | M4 | clip-count recommender + sentence-snapped clip building |
| `reframe/`  | M5 | center smart-crop 16:9 → 9:16 (subject tracking is Phase 2) |
| `captions/` | M7 | styled ASS captions with platform-safe margins |
| `render/`   | M13 | ffmpeg compose/export to H.264/AAC at target spec |

`pipeline.py` is the in-process orchestrator; `../cli.py` is the entry point.

## Caching

Transcription is cached under `<work_dir>/<source_hash>/`. Re-running to change
clip count, aspect, or captions reuses the transcript instead of re-running
Whisper — the biggest time saver at scale.

## Roadmap (later phases, not built here)

Phase 2 quality (subject-tracking reframe, karaoke captions, branding, loudness
norm, metadata, thumbnails) · Phase 3 localization (stems, translate + TTS dub,
QC gate) · Phase 4 scale (queue, DB, dedup) · Phase 5 publish + research.
