# ShortForge — Handoff / continue on your PC

This file is the bridge between the cloud chat where ShortForge was built and
your own computer. **The chat does not travel — the code does.** Everything the
assistant wrote lives in this git repo, so once you clone it you have the whole
tool, exactly as built. A fresh Claude Code session on your PC won't remember
the old conversation, but it can read this file and the code and pick right up.

---

## 1. "Will I get the same chat on my PC?"

**No — and you don't need it.** The conversation history stays on claude.ai. But:

- **All the work is the code in this repo**, which is committed and pushed to
  GitHub. Clone it and nothing is missing.
- A **new** Claude Code session on your PC starts fresh (no memory of the old
  chat). Point it at this repo and say *"read HANDOFF.md and continue"* — it will
  understand the state from this file + the code + `git log`.
- Your PC session **does** keep updating this same GitHub repo (you just
  `git commit && git push` like normal), so nothing is lost.

Think of it as: **chat = the conversation (stays in the cloud); repo = the
product (comes with you).**

---

## 2. Get it running on your PC

```bash
git clone <your-repo-url>
cd viral-topics-tool

# System tools:
#   ffmpeg + ffprobe   (Ubuntu: sudo apt-get install -y ffmpeg | macOS: brew install ffmpeg)
#   espeak-ng          (offline voice fallback: sudo apt-get install -y espeak-ng)

python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Sanity check (no video needed):

```bash
python -m pytest -q          # should be all green
python cli.py run --help
```

First real run on one of your own videos:

```bash
python cli.py run "https://youtu.be/YOUR_OWN_VIDEO" --owner-confirmed
# or a local file you own:
python cli.py run ./my_talk.mp4 --owner-confirmed --duration 45 --num-clips 5
```

Clips + a `manifest.json` land in `out/`.

---

## 3. Adding your API keys (do this on your PC, not in chat)

Never paste keys into a chat. ShortForge reads them from a local `.env`:

```bash
cp .env.example .env
# edit .env:  ANTHROPIC_API_KEY=sk-...
```

`.env` is git-ignored (stays on your machine, never committed). It's loaded
automatically on every run. Point elsewhere with `--env-file`.

**You don't strictly need any key** — with none, ShortForge runs fully offline
(heuristic hook detection, offline Argos translation, espeak voice). A Claude
key just makes hooks/translation/titles smarter and enables `--vision`. Cost is
a few cents per video, only when a key is set.

---

## 4. What's built (and what needs your GPU)

Runs anywhere (CPU-only is fine):

- **Phase 1** — ingest (your channels only, `--owner-confirmed` gate) → transcribe
  (faster-whisper) → hook detection → **coherent-story** clip selection → 9:16
  reframe → captions → render.
- **Phase 2** — subject-tracking reframe, caption templates + animations, logo,
  loudness norm, **jump cuts** (`--jumpcuts`, trims dead air), titles/hashtags,
  thumbnails, QC review gate.
- **Phase 3** — translate to any language + captions; **dub** with music/SFX
  preserved. Same-language = original audio kept, never re-recorded.
- **M3++** — visual hook signals (motion/scene-cut/faces) always on; optional
  `--vision` claude-watch scorer (needs Claude key + vision).

Better with a **GPU** on your PC (all optional, all degrade gracefully if absent):

| Feature | Flag | Needs |
|---------|------|-------|
| Voice cloning (your own voice on the dub) | `--tts xtts --voice-sample you.wav` | `pip install TTS` + GPU |
| Clean music/voice separation for dubs | (automatic) | `pip install demucs` + torch |
| **Lip-sync** dubbed clips to the mouth | `--lipsync` | cloned Wav2Lip + `wav2lip_gan.pth` + GPU |
| Natural neural TTS voices | `--tts edge` | network (blocked in the cloud sandbox, works on your PC) |

### Lip-sync setup (cross-language dubs)

Lip-sync only runs on **cross-language dubs** (new voice → mouth no longer
matches). Same-language clips are never touched.

```bash
git clone https://github.com/Rudrabha/Wav2Lip
# download wav2lip_gan.pth into Wav2Lip/checkpoints/ (see that repo's README)

python cli.py run ./my_talk.mp4 --owner-confirmed \
    --language es --dub --tts xtts --voice-sample me.wav \
    --lipsync --wav2lip-repo ./Wav2Lip --wav2lip-checkpoint ./Wav2Lip/checkpoints/wav2lip_gan.pth
```

Or set the `lipsync:` block in `config/settings.yaml` once and just pass
`--lipsync`. If Wav2Lip is missing or errors, the clip keeps its normal render.

---

## 5. Not built yet (pick up here)

- **Phase 4 — scale/ops**: job queue (Celery/Redis), a DB for clips/manifests,
  dedup across runs. Only worth it at high volume; the current in-process
  pipeline handles one video at a time fine.
- **Phase 5 — publish + research**: scheduled uploads via platform APIs after
  the QC gate approves, plus niche/RPM research helpers.

To continue with Claude Code on your PC: open this repo and say
*"Read HANDOFF.md, then let's build Phase 4"* (or whatever's next). Check
`git log --oneline` and `shortforge/README.md` for the fuller picture.

---

## 6. The one hard rule (don't lose this)

Source content must be **your own channels or otherwise licensed**. Ingestion
refuses to run without `--owner-confirmed`. Keep it that way.
