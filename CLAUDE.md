# CLAUDE.md — operating rules for ShortForge

## Project
ShortForge converts the operator's **own** long-form videos into multilingual
short-form clips (Reels/Shorts): ingest → transcribe → hook-detect → select →
reframe → translate/dub → captions → render → QC → publish sidecars. Python,
FFmpeg-based. The operator runs it locally on **Windows 11, Python 3.13,
CPU-only, 16 GB RAM — no NVIDIA GPU.** Assume CPU constraints in every decision.

## Session protocol — mandatory
1. **Read `PROGRESS.md` first, every session.** It is the single source of truth
   for what is done, what is next, and what has been decided. Do not ask the
   operator to re-explain the plan — it is in that file.
2. **Never skip ahead.** Work the steps in `PROGRESS.md` order. Complete one
   step, update `PROGRESS.md`, report, and stop.
3. **Update `PROGRESS.md` in the same commit as the work it describes.** A step
   is not finished until its status, commit SHA, and verification note are there.
4. **Distinguish "implemented" from "verified".** Code that passes unit tests in
   the sandbox is *implemented* (🟡). It is *verified* (✅) only when the operator
   confirms it on Windows. Never mark something verified on the operator's behalf.
5. **Report format after every step:** what changed (files), what already
   existed, anything skipped and why, the exact Windows commands to pull and
   test, and specifically what to look at or listen for to confirm it worked.

## Engineering rules — apply to all work
1. **Never silently degrade.** If the pipeline cannot deliver what was requested
   (target language, dub mode, caption language, reframe mode, voice), fail with
   a clear, actionable error. Never emit output that looks successful but isn't
   what was asked. (A run once produced French captions labelled English.)
2. **The end-of-run summary line is mandatory** and grows as stages land, e.g.:
   `done: 2 clips | fr→en (minimax) | dub voice (elevenlabs, emotion, 2 voices) |
   stems kept d+b+o | bed local | reframe track | QC 91 | cost $0.42`
3. **Every fallback taken appears in the log and the manifest.**
4. **Never log API keys** — including in tracebacks and probe output.
5. **Cache everything expensive**, keyed on content hash plus the settings that
   affect the result.
6. **Probe code and production code share one client path** — they drifted once
   and produced a `/v1/v1/` URL bug that took two rounds to find.
7. **Content source:** operator's own or licensed content only. No audio, music,
   or SFX from unlicensed sources — this content is monetized, and Content-ID
   claims would defeat the purpose. The `--owner-confirmed` gate enforces this.

## Environment quirks worth remembering
- Demucs runs ~8–12× realtime on this CPU; log an ETA before long stages.
- Whisper `base` produces garbled French — `small` is the minimum acceptable.
- Local voice cloning (XTTS) is impractical here; route cloning via ElevenLabs.
- ElevenLabs does **not** accept SSML — use `voice_settings` (`stability`,
  `similarity_boost`, `style`).
- The operator **cannot edit `.env` files** by hand. All configuration must be
  reachable through the Streamlit settings UI (`python cli.py ui`). Keys are
  stored in gitignored `config/providers.local.json`.
