# ShortForge — core clipper (Phases 1–2)

Turn **your own** long-form videos into short vertical clips.

```
ingest ─▶ transcribe ─▶ detect hooks ─▶ select clips ─▶ reframe 9:16 (track speaker)
       ─▶ karaoke captions ─▶ logo ─▶ loudnorm ─▶ render ─▶ metadata + thumbnail ─▶ manifest.json
```

Captions-only for now (dubbing/translation is Phase 3). Runs headless from the
CLI and produces watchable vertical clips from one source video, each with
generated title/hashtags and a cover image, plus a `manifest.json`.

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
`--reframe-mode` (`track` / `center`), `--caption-style` (`karaoke` / `simple`),
`--logo path.png` `--logo-corner TR` `--logo-opacity 0.9`, `--niche "..."`,
`--no-captions`, `--no-loudnorm`, `--no-metadata`, `--no-thumbnail`,
`--whisper-model`, `--transcript file.(srt|json)`, `--cookies cookies.txt`,
`--no-llm` / `--use-llm`, `--work-dir`, `--output`. See `python cli.py run --help`.

## What each module does (Section 3)

| Dir | Module | Scope |
|-----|--------|-------|
| `ingest/`    | M1  | yt-dlp download (+cookie auth) or local file; ownership gate |
| `analyze/`   | M2  | audio extract + faster-whisper word-level transcript (cached) |
| `analyze/audio` | M9 | loudness norm to −14 LUFS; silence-trim planning |
| `detect/`    | M3  | hook scoring — Claude rubric if available, else keyword heuristic |
| `select/`    | M4  | clip-count recommender + sentence-snapped clip building |
| `reframe/`   | M5  | **subject-tracking** smart-crop 16:9 → 9:16 (YuNet); center-crop fallback |
| `captions/`  | M7  | **karaoke** word-highlight ASS (or simple); platform-safe margins |
| `brand/`     | M8  | logo overlay (corner / size / opacity) |
| `metadata/`  | M10 | per-clip title / description / hashtags (heuristic or Claude) |
| `thumbnail/` | M11 | expressive-keyframe cover at output aspect |
| `render/`    | M13 | ffmpeg compose/export to H.264/AAC (plain + tracked pipe) |

`pipeline.py` is the in-process orchestrator; `../cli.py` is the entry point.

### Phase 2 notes

- **Subject tracking** uses OpenCV's YuNet face detector
  (`reframe/models/*.onnx`, bundled). No face / OpenCV missing → center-crop.
  Detection quality is best judged on real talking-head footage.
- **Loudness** uses single-pass `loudnorm` (lands within ~1–2 LU of −14).
- **Silence trim** (jump cuts): the planning logic (`analyze/audio.py`,
  `edit.jumpcuts`) is implemented and tested; wiring it into the render is the
  next step (kept off by default so it can't desync captions).

## Caching

Transcription is cached under `<work_dir>/<source_hash>/`. Re-running to change
clip count, aspect, or captions reuses the transcript instead of re-running
Whisper — the biggest time saver at scale.

## Roadmap (later phases, not built here)

Phase 2 quality (subject-tracking reframe, karaoke captions, branding, loudness
norm, metadata, thumbnails) · Phase 3 localization (stems, translate + TTS dub,
QC gate) · Phase 4 scale (queue, DB, dedup) · Phase 5 publish + research.
