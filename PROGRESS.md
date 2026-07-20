# PROGRESS.md — ShortForge status

Single source of truth. Read this first every session. Keep it concise:
completed steps are one line (step · SHA · verified?); long detail lives in the
commit messages, not here. Statuses: ✅ done+verified · 🟡 done, unverified on
Windows · ⏳ in progress · ⬜ pending.

Branch: `claude/nifty-cray-n8l888` → PR #1 to `main`.

## Current position
STEP 3.5b (pluggable ASR) is **done, unverified**. Next up: **STEP 3.6 — UI fixes**.

## Steps
| # | Description | Status |
|---|---|---|
| 0 | Fix LLM provider detection (URL normalization, configured model, raw-response display) | ✅ |
| 1 | Cost controls (pre-flight estimate, per-job ceiling, spend tracking, synthesis cache, `--dry-run-cost`) | 🟡 |
| 2 | `benchmark-llm` command | ✅ |
| 3 | ElevenLabs language coverage (en, de, it, es, ja, ar) | 🟡 |
| 3.5 | Transcription quality — Whisper default `small`, min-confidence flagging | 🟡 |
| 3.5b | Pluggable ASR — provider category, local/API backends, detection, `benchmark-llm --task transcribe` | 🟡 |
| 3.6 | **UI fixes** — remove hardcoded 90s duration cap, audit other hardcoded limits, beginner-friendly Basic/Advanced layout with plain-language labels + inline help | ⬜ **NEXT** |
| 4 | H1 — translate for speech (spoken register, length-matched ±15%, glossary, separate dub vs caption translation) + wire LLM failover chain | ⬜ |
| 5 | H2 — prosody transfer onto ElevenLabs `voice_settings`, segment chunking mirroring original pauses | ⬜ |
| 6 | H4 — diarization, distinct voice per speaker, cloning path, feed active speaker into reframe | ⬜ |
| 7 | H3 — timing, formant-preserving stretch capped ±8% | ⬜ |
| 8 | J — LLM hook detection, boundary/semantic refinement, diversity, vision scoring, track re-ID | ⬜ |
| 9 | G2 — audio beds for genuinely dry scenes (licensed sources only) | ⬜ |
| 10 | I — transitions, visual accents, opening-hook treatment, editorial QC score | ⬜ |
| 11 | Studio Voice — audio-enhancement category, gRPC adapter, source-audio cleanup only | ⬜ |
| 12 | Phase 2 leftovers — karaoke captions, branding, loudness norm, metadata, thumbnails, style/lang caption rules | ⬜ |
| 13 | K — complete web UI (job/running/results, review-approve gate, side-by-side original vs dub) | ⬜ |
| 14 | M — batch mode: resume-on-crash, time/cost budgets, dry-run, profiles, graceful interrupt, repro record | ⬜ |

## Completed log (step · SHA · verified?)
- Phase 1 — full pipeline ingest→…→manifest · `ef97b24` · verified
- Phase 2 quality layer + caption templates · `28f7598`,`2e8f576` · verified
- Phase 3 localization (translate+dub) + QC · `31b483d` · verified
- Coherent clip selection · `8c3d213` · verified · Visual hooks (M3++) · `718b152` · verified
- Lip-sync toggle + .env loader · `3fad8aa` · implemented · Jump cuts wired · `c0d4535` · verified
- Lean Phase 4 sidecars/batch/resume · `38ded10` · verified · SEO metadata + niches · `2221f88` · verified
- A1 stems remove original voice, ambience −18 dB · `e0ca024` · **verified (bed is back)**
- A2 translation fail-loud + provenance cache · `7a2821f` · verified
- A3 single caption layer + burned-in handling · `35d0c00` · implemented
- A4 virtual-camera reframe · `69cf891` · verified
- B summary line + manifest provenance · `2bfa94a` · verified
- C doctor + robustness + wizard · `3cc41b5` · verified
- E configurable LLM provider · `7407592` · verified · D Streamlit dashboard · `2258739` · verified
- G1 lost music/SFX fixed · `cc1738c` · verified
- L multi-provider layer + Settings UI (masked keys, gitignored store) · `018bf32`,`c7a0ebc` · verified
- STEP 0 LLM detection (/v1/v1 + hardcoded model) · `3d46834`,`5dc0ae5` · **verified (NVIDIA 200)**
- STEP 1 cost controls · `f4f8509` · unverified
- STEP 2 benchmark-llm · `93b12ce` · verified (ran on real French source)
- STEP 3 ElevenLabs lang coverage + STEP 3.5 Whisper `small` default · `64403e6` · unverified
- STEP 3.5b pluggable ASR + `--task transcribe` · `f1d3cf2` · unverified (199 tests pass)

## Decisions (settled)
- **Primary LLM `minimaxai/minimax-m3`** — won STEP 2 (natural spoken English, 0 failures / 5 segs).
- **Failover chain: minimax → Vercel AI Gateway → `z-ai/glm-5.2`** — GLM hard-failed once and mistranslated *le plus chargé* as "most populated ship".
- **Vision slot: Vercel AI Gateway** (`https://ai-gateway.vercel.sh/v1`).
- **TTS: ElevenLabs** — emotion ✓ (`stability`/`similarity_boost`/`style`), cloning ✓, SSML ✗.
- **v0** (`https://api.v0.dev/v1`) **disabled** — web-code model, wrong for translation/vision.
- **Transcription: Whisper `small` minimum** — `base` garbled French and poisoned every translation. ASR is now pluggable (STEP 3.5b): local faster-whisper or an API backend.
- **NVIDIA endpoint** `https://integrate.api.nvidia.com/v1`, OpenAI-compatible, one provider entry per model. Canary ASR may live on a separate NIM/gRPC endpoint.
- **Studio Voice** = audio *enhancer* (gRPC), applied to **source** audio before transcription only — never to synthesized dub audio.

## Open threads
- **LLM failover not yet wired** — priority order honoured but errors don't cascade. Fold into STEP 4.
- STEP 2's translation benchmark ran on the **garbled `base` transcript** — re-run after a `small`/API transcript is confirmed; ranking may change.
- ElevenLabs multilingual (de/it/es/ja/ar), cost controls, STEP 3.5/3.5b — all **unverified on a real paid run**.
- Confirm ASR benchmark on real footage: `benchmark-llm --source <video> --owner-confirmed --task transcribe`.

## How to run (Windows)
```
cd $HOME\Desktop\viral-topics-tool
venv\Scripts\activate
git pull origin claude/nifty-cray-n8l888
pip install -r requirements.txt
python cli.py doctor
python cli.py ui        # settings + new-job dashboard
python cli.py wizard    # guided CLI run
python cli.py cache clear --all
```
