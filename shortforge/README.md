# ShortForge — core clipper + localization (Phases 1–3)

Turn **your own** long-form videos into short vertical clips, optionally dubbed
into another language.

```
ingest ─▶ transcribe ─▶ detect hooks ─▶ select clips ─▶ reframe 9:16 (track speaker)
       ─▶ translate + dub (music/SFX preserved) ─▶ target-language captions ─▶ logo
       ─▶ loudnorm ─▶ render ─▶ metadata + thumbnail ─▶ QC gate ─▶ manifest.json
```

Runs headless from the CLI and produces watchable vertical clips from one source
video — captions (and optionally a voiceover) in the language you choose, each
with generated title/hashtags, a cover image, and a review record, plus a
`manifest.json`.

> **The one hard rule:** source content must be the operator's own channels or
> otherwise licensed. Ingestion refuses to run without `--owner-confirmed`.

## Install

```bash
# System dependency (provides ffmpeg + ffprobe):
sudo apt-get install -y ffmpeg      # or: brew install ffmpeg

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# optional, for Claude-backed hook detection:
pip install "anthropic>=0.40.0"     # then add your key (see below)
```

### API keys (optional — add them on your own machine)

ShortForge runs **fully offline** with no keys (heuristic hooks, offline Argos
translation, espeak voice). A Claude key unlocks smarter hook detection, the
vision scorer (`--vision`), better translation, and titles — a few cents/video,
only when set.

Add keys in a local **`.env`** file — never paste them into a chat, never commit
them:

```bash
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY=sk-...
```

`.env` is git-ignored and loaded automatically on every run (a real
`export ANTHROPIC_API_KEY=...` in your shell also works and takes precedence).
Point elsewhere with `--env-file path/to/.env`.

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

# Several of your own videos in one go (sequential batch):
python cli.py run-batch a.mp4 "https://youtu.be/XXXX" --owner-confirmed --resume
#   or:  python cli.py run-batch --from-file my_videos.txt --owner-confirmed
```

Every rendered clip gets a companion **`<video>.txt`** with a ready-to-paste
**Title / Description / Tags** (and a first-comment CTA), so publishing metadata
travels next to each video. `--num-clips 0` (default) makes the tool **recommend
how many strong clips** the source can yield; set a number to force it.

Not sure what to make? Ask for growing niches:

```bash
python cli.py niches                       # ranked by opportunity
python cli.py niches --low-competition     # easiest lanes to break into
python cli.py niches --category Finance --sort monetization
python cli.py niches --interests "I'm a nurse who likes budgeting"   # Claude-personalized
```

Key flags: `--duration`, `--tolerance`, `--num-clips` (0 = auto-recommend),
`--aspect` (`9:16` / `1:1` / `16:9` / `WxH`), `--fill` (`crop` / `blur`),
`--reframe-mode` (`track` / `center`),
`--caption-template` (`clean`/`bold_pop`/`karaoke_amber`/`reveal_green`/`boxed`/`minimal`),
`--caption-animation`, `--caption-font`, `--caption-size`, `--uppercase`,
`--language es`, `--dub`, `--tts` (`espeak`/`edge`/`xtts`), `--voice-sample`,
`--translate-backend`, `--lipsync` (`--wav2lip-repo` / `--wav2lip-checkpoint`),
`--logo path.png` `--logo-corner TR` `--logo-opacity 0.9`,
`--niche "..."`, `--jumpcuts`, `--resume`, `--no-sidecar`, `--no-captions`,
`--no-loudnorm`, `--no-metadata`,
`--no-thumbnail`, `--no-review`, `--whisper-model`, `--transcript file.(srt|json)`,
`--cookies cookies.txt`, `--no-llm` / `--use-llm`, `--work-dir`, `--output`.
See `python cli.py run --help`.

## What each module does (Section 3)

| Dir | Module | Scope |
|-----|--------|-------|
| `ingest/`    | M1  | yt-dlp download (+cookie auth) or local file; ownership gate |
| `analyze/`   | M2  | audio extract + faster-whisper word-level transcript (cached) |
| `analyze/audio` | M9 | loudness norm to −14 LUFS; **jump cuts** (dead-air trim, synced across video/audio/captions) |
| `detect/`    | M3  | hook scoring: transcript rubric (Claude/heuristic) **+ visual signals** (motion/cuts/faces), optional Claude-vision |
| `select/`    | M4  | clip-count recommender + **coherent-story** clip building (complete thoughts) |
| `reframe/`   | M5  | **subject-tracking** smart-crop 16:9 → 9:16 (YuNet); center-crop fallback |
| `captions/`  | M7  | caption **templates + animations** (fade/pop/karaoke/reveal); safe margins |
| `localize/`  | M6  | translate + **dub** (voiceover over preserved music/SFX); target-lang captions |
| `lipsync/`   | opt | Wav2Lip mouth-sync for cross-language dubs (GPU; graceful skip) |
| `brand/`     | M8  | logo overlay (corner / size / opacity) |
| `metadata/`  | M10 | **SEO** title / detailed description / analysis-based tags (heuristic or Claude) |
| `publish/`   | M14 | per-video title/description/tags **sidecar** + batch index |
| `research/`  | —   | growing-niche suggestions (`niches` command; opportunity-ranked) |
| `thumbnail/` | M11 | expressive-keyframe cover at output aspect |
| `qc/`        | M12 | review gate: pending-review status + brand-safety flags |
| `render/`    | M13 | ffmpeg compose/export to H.264/AAC (plain + tracked pipe) |

`pipeline.py` is the in-process orchestrator; `../cli.py` is the entry point.

### Finding the best parts (M3++)

Hook detection combines **what's said** and **what's shown**:

- **Transcript rubric** — scores each sentence for questions, numbers/stats,
  strong claims, curiosity phrases, emotional peaks, self-containedness
  (Claude when keyed, else a keyword heuristic).
- **Local visual signals** (`detect/visual.py`, no API) — per-segment motion
  energy, scene-cut density, and face presence (reusing YuNet). Static talking
  over dead footage scores low; a fast cut / reaction / b-roll payoff scores
  high. Fused into the score via `detect.visual_weight`.
- **Claude-vision** (`detect/vision_llm.py`, opt-in `--vision` / `detect.vision_llm`)
  — the *claude-watch* technique: dense-hook + sparse-body frames tiled into
  contact sheets and sent to Claude **with the transcript**, so it scores each
  moment on the pixels too. Needs an API key + a vision model.

### Phase 3 notes (localization)

- `--language es --dub` = Spanish captions **and** voiceover; `--language es`
  alone = Spanish captions only. Languages: en/de/it/es/ja/ar (+ more).
- **Translation**: Claude when `ANTHROPIC_API_KEY` is set, else offline **Argos**
  (`pip install argostranslate`), else source text kept with a warning.
- **TTS** (`--tts`): `espeak` (offline, robotic — the default fallback),
  `edge` (natural neural voices, needs network), `xtts` (clones *your* voice —
  needs `TTS` + a GPU + `--voice-sample`).
- **Music/SFX preserved**: Demucs splits vocals from music+SFX when installed
  (clean); otherwise the original is ducked under the dub (music/SFX survive,
  with some original-voice bleed). Sounds are never removed.
- **Arabic** and other RTL scripts fall back to the `fade` caption animation.
- **Lip-sync** (`--lipsync`, opt-in): only for **cross-language dubs** — when a
  new voiceover is synthesized the on-screen lips no longer match, so Wav2Lip
  re-renders the mouth to track the dub. Same-language clips keep the original
  audio + real lips and are never touched. Needs a GPU + a cloned
  [Wav2Lip](https://github.com/Rudrabha/Wav2Lip) (`--wav2lip-repo`,
  `--wav2lip-checkpoint`, or the `lipsync:` config block). Missing/failed →
  the clip keeps its normal render (graceful degradation).
- Each clip is marked `pending_review` in the manifest (M12) for approval
  before publishing (Phase 5).

### Phase 2 notes

- **Subject tracking** uses OpenCV's YuNet face detector
  (`reframe/models/*.onnx`, bundled). No face / OpenCV missing → center-crop.
  Detection quality is best judged on real talking-head footage.
- **Loudness** uses single-pass `loudnorm` (lands within ~1–2 LU of −14).
- **Jump cuts** (`--jumpcuts`, `edit.jumpcuts`): trims long silences / dead air.
  Video is cut with an ffmpeg `select`, the source audio with a matching
  `aselect`, and caption word-timings are remapped onto the compressed
  timeline, so video, audio, and captions stay in sync. Off by default;
  automatically disabled when dubbing (a synthesized voice would not line up).

### Phase 4 notes (lean scale)

Kept intentionally light — no job queue, no database, no worker pool (overkill
for a creator running on one machine):

- **SEO metadata** (`metadata/`, M10): titles/descriptions/tags are built from
  what each clip actually says — a hooked, keyword-front-loaded title, a
  detailed searchable description, and analysis-based tags (clean keyphrases,
  not a genre list) that differ per clip. Plain tags (YouTube) and hashtags
  (Shorts/TikTok/Reels) are produced separately. Claude sharpens all of it.
- **Publishing sidecars** (`publish/`, M14): each clip gets a `<video>.txt` with
  copy-paste-ready Title / Description / Tags + Hashtags. Toggle with `--no-sidecar`.
- **Niche suggestions** (`research/`, `niches` command): an opportunity-ranked
  guide to growing short-form niches — scored on growth, competition, and
  monetization, with lower-competition sub-niche angles. Curated + offline
  (a live trend feed would need an external data source); `--interests` adds a
  Claude-personalized pick.
- **Batch** (`run-batch`): process many of your own videos sequentially; keeps
  going if one fails and writes a combined `batch_<date>_index.json`.
- **Resume** (`--resume`): skip clips whose output already exists, so a re-run
  (or a batch that died partway) continues instead of redoing finished work.

## Caching

Transcription is cached under `<work_dir>/<source_hash>/`. Re-running to change
clip count, aspect, or captions reuses the transcript instead of re-running
Whisper — the biggest time saver at scale.

## Roadmap (later phases, not built here)

Phase 5 publish + research: scheduled uploads via platform APIs after the QC
gate approves, plus niche/RPM research helpers. (The heavy-ops side of Phase 4 —
a Celery/Redis queue and a database — is intentionally left out; the lean batch
runner above covers the real need without the overhead.)
