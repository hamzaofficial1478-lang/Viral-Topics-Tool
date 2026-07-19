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

**Any LLM provider (Part E).** ShortForge isn't tied to Anthropic. All LLM calls
(translation, hook detection, metadata) go through one abstraction configured in
`.env`:

```
LLM_PROVIDER=anthropic | openai | none   # openai = any OpenAI-compatible gateway
LLM_BASE_URL=https://your-gateway.example.com/v1
LLM_API_KEY=...
LLM_MODEL=...
```

`none` (or no key) → local/heuristic paths only. Setting just `ANTHROPIC_API_KEY`
still works (back-compat). Test the connection with `python cli.py check-llm`
(prints provider, base URL, model, and latency — the key is always redacted).

**Multi-provider TTS + audio library (Part L).** `shortforge/providers/` is a
capability-driven layer: `TTS_PROVIDERS` lists providers in priority order, each
declares what it can do (languages, SSML, emotion, cloning, cost) in `.env`, and
the router picks a capable one, **fails over** on error, tracks **cost per
provider**, chunks long input, and caches identical lines. `edge` is the built-in
free option; any other name maps to an OpenAI-compatible `/audio/speech` gateway.
An `AudioLibraryProvider` (`local` folder or `api`) supplies music/SFX beds (G2).
See `.env.example`, then run `python cli.py check-providers` to test everything
(capabilities, latency, a sample synthesis — keys redacted).

## Use

### Web dashboard (no terminal needed)

```bash
pip install streamlit
streamlit run app.py          # opens a dashboard in your browser
```

Fill in the form (URL or file upload, ownership, aspect, length, language, dub
mode, caption template, reframe, logo), click **Run**, watch live per-stage
progress, then preview each clip inline with its title/description/tags and
download buttons. It's a thin layer over the same pipeline — the CLI below still
works identically.

### Command line

Check your environment first (dependencies, disk, hardware estimates):

```bash
python cli.py doctor
```

Interactive wizard (Section 7 order — shows a plan and asks to confirm):

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
- **Translation (A2, fail-loud)**: Claude when keyed, else offline **Argos**
  (auto-installs the language pair). If the output language differs from the
  source and no backend is available, the run **aborts** with an actionable
  message rather than silently emitting source-language captions
  (`--allow-untranslated` overrides, loudly). Failed/passthrough translations
  are never cached; the cache key includes the backend + version, so installing
  a backend after a failed run recomputes instead of serving the poisoned entry.
  Controls: `--no-cache`, `--refresh-translation`, `shortforge cache clear
  [--translation|--transcript|--all]`.
- **TTS** (`--tts`): `espeak` (offline, robotic — the default fallback),
  `edge` (natural neural voices, needs network), `xtts` (clones *your* voice —
  needs `TTS` + a GPU + `--voice-sample`).
- **Original voice removed, music/SFX kept (A1 + G1)**: dubbing splits the source
  with **Demucs** `--two-stems=vocals` (once per source, cached), keeping
  `no_vocals` (= drums+bass+other) and mixing the new voice over it with a
  **sidechain duck** (`duck_db`, default −6 dB) so music breathes between
  sentences. Demucs mis-routes some ambience into the *vocals* stem, so
  `vocal_removal_strength` (default **partial −18 dB**) retains that stem quietly
  rather than discarding it, keeping scenes from going dead-silent (set `full` for
  the cleanest, bleed-free bed). `--debug-audio` exports `vocals/accompaniment/bed`
  WAVs to `out/audio_debug/` so you can hear exactly what's kept. Better
  separation: `--stem-model htdemucs_ft`; faster: `mdx_extra_q`. Without Demucs the
  run **aborts** rather than playing two voices (`--allow-voice-bleed` to override).
- **One caption layer only (A3)**: renders burn exactly one subtitle track (in
  the target language) and pass `-sn` so no soft subtitle stream from the source
  is carried through. Captions **baked into the source pixels** are detected
  (edge-density band scan) and treated with `--burned-in cover|blur|crop` so our
  captions never sit on top of the source text.
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

- **Virtual-camera reframe (A4)** (`reframe/vcam.py`): a crop path that follows
  the action instead of a fixed crop. Per-sample **saliency** (speaker face →
  dominant motion → center) drives the camera; it **re-detects at every scene
  cut** (never carries a box across a cut), and a **hold / pan / snap** state
  machine with a deadzone, velocity cap, and minimum dwell keeps it smooth —
  panning within a shot but *cutting* between subjects. Detection runs on
  downscaled frames and the saliency track is cached. `--debug-reframe` writes a
  source-aspect diagnostic video (detected point, crop box, scene-cut markers).
  No OpenCV/model, or nothing salient → center-crop fallback. Uses YuNet (not
  mediapipe, which lacks Python 3.13 wheels).
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
