# PROGRESS.md — ShortForge status

Single source of truth. Read this first every session. Keep it concise:
completed steps are one line (step · SHA · verified?); long detail lives in the
commit messages, not here. Statuses: ✅ done+verified · 🟡 done, unverified on
Windows · ⏳ in progress · ⬜ pending.

Branch: `claude/nifty-cray-n8l888` → PR #1 to `main`.

## Current position
Executing the **REVISION 2 routing plan** (see below). Item 1 → **R6 (multi-model
schema) done, unverified**. Next up: **R1 — per-task provider binding**.

## Done foundation (steps 0–3.6)
| # | Description | Status |
|---|---|---|
| 0 | Fix LLM provider detection (URL normalization, configured model, raw-response display) | ✅ |
| 1 | Cost controls (pre-flight estimate, per-job ceiling, spend tracking, synthesis cache, `--dry-run-cost`) | 🟡 |
| 2 | `benchmark-llm` command (`--task translate/hooks/transcribe`, `--models`) | ✅ |
| 3 | ElevenLabs language coverage (en, de, it, es, ja, ar) | 🟡 |
| 3.5 | Transcription quality — Whisper default `small`, min-confidence flagging | ✅ |
| 3.5b | Pluggable ASR — provider category, local/API backends, detection | 🟡 |
| 3.6 | Dashboard UI fixes — no 90s/20-clip caps, Basic/Advanced, live plan, ownership gate | 🟡 |

## Routing matrix (REVISION 2 — the plan of record)
Principle: **spend where impact is high AND token volume is low; save where volume
is high AND stakes are low.** Free tier is *enforced* (hard-fail, not warn) on
frame-level work (tasks 7, 8). Per-task binding — no global LLM setting.

| # | Task | Primary → fallback | Tier |
|---|---|---|---|
| 1 | Transcription (ASR) | AgentRouter `/audio/transcriptions` → local Whisper `small` | paid, 1× cached |
| 2 | Hook detection ⭐ | best reasoning model (fuse transcript + free vision) → nemotron-omni → minimax | PAID (justified) |
| 3 | Clip completeness | same as #2 → minimax | paid, 1× batched |
| 4 | Emotion labelling | strong model, batched 1 call → minimax | paid, 1× |
| 5 | Dub translation | minimax **and** Forge (per-chunk auto-select) → Vercel | paid, 30–60× |
| 6 | Caption translation | minimax → Forge → Vercel (separate cache key) | paid, 30–60× |
| 7 | Vision / frame scoring | `llama-3.2-11b-vision` → `nemotron-3-nano-omni` | **FREE enforced** |
| 8 | OCR / burned-in | `nemotron-ocr-v2` → edge heuristic | **FREE enforced** |
| 9 | Metadata (title/desc/tags) | cheapest free text → AgentRouter cheap | free preferred |
| 10 | TTS quality tier | ElevenLabs → Chatterbox | paid |
| 11 | TTS volume tier | `chatterbox-multilingual-tts` (23 langs) → ElevenLabs | free |
| 12 | Voice cloning | ElevenLabs only | paid, rare |
| 13 | Lip sync | NVIDIA LipSync | free, off by default |
| 14 | Source audio enhance | Maxine Studio Voice (gRPC) on source only | free, off by default |

## Order of work (REVISION 2 — supersedes old steps 4–14)
| # | Item | Status |
|---|---|---|
| 1 | **R6** multi-model-per-credential schema + fetch-models | 🟡 |
| 1 | R1 per-task provider binding (14 tasks: primary/secondary/fallback/toggle in UI) | ⬜ **NEXT** |
| 1 | R2 cost-tier guard (`paid_allowed:false` on tasks 7,8 — hard fail) | ⬜ |
| 2 | R7 add gateways (AgentRouter, Forge AI) + NVIDIA models with R3 adapters | ⬜ |
| 3 | **C1 hook-detection fusion** (transcript LLM + free vision + audio/heuristic, weighted) — top feature | ⬜ |
| 4 | C2 ASR routing (AgentRouter primary, word-timestamps required) + C3/C4 translation routing | ⬜ |
| 5 | R8 benchmarks (dub-translation quality-per-second; hook premium vs minimax) → operator confirms | ⬜ |
| 6 | R4 failover (per-task cascade, visible in log/manifest/summary) + R5 per-task cost tracking | ⬜ |
| 7 | **STEP 4 (H1)** segment merging into 5–12s chunks + translate-for-speech | ⬜ |
| 8 | STEP 5 (H2 prosody) → STEP 6 (H4 speakers) → STEP 7 (H3 timing) | ⬜ |
| 9 | STEP 8 vision-assisted selection (largely delivered by C1) | ⬜ |

Adapters still needed (R3): OCR shape, TTS shape, LipSync (video+audio), Studio
Voice (**gRPC**), vision (image content blocks), ASR (multipart) — several are
not chat-completions. Gateways are untrusted for production: always keep a
fallback and fail over on auth/credit errors.

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
- STEP 2 benchmark-llm (+ `--models` candidate flag) · `93b12ce` · verified (ran on real French source)
- STEP 3.5 Whisper `small` default · `64403e6` · **verified** — pirate source: base 42 seg/511 words garbled → small 76 seg/534 words correct French; translations improved
- STEP 3 ElevenLabs lang coverage · `64403e6` · unverified
- STEP 3.5b pluggable ASR + `--task transcribe` · `f1d3cf2` · unverified (199+ tests pass)
- STEP 3.6 dashboard: removed 90s/20-clip caps (explicit num_clips now honoured in `select.py` — was a silent-degrade bug), Basic/Advanced layout, plain labels + help, live plan + cost/speed flags, ownership-gated Run, headless AppTest smoke tests · unverified (208 tests pass)
- R6 multi-model-per-credential store: one credential holds many models (each with category/toggle/priority/caps); `models_in()` flattens credentials + legacy providers so all runtime consumers are unchanged; `detect.fetch_models()` (/v1/models); Settings UI credentials section with Fetch-models + per-model add/toggle/priority/Test&Detect; credit-note field · unverified (217 tests pass)

## Decisions (settled)
- **Routing is per-task (REVISION 2)** — no global LLM. Each of the 14 tasks binds its own primary/secondary/fallback. Free tier **enforced** (hard-fail) on tasks 7 (vision) & 8 (OCR); paid allowed where volume is low & impact high.
- **Hook detection = best-resourced task** — multi-signal fusion (best reasoning LLM on transcript + free `llama-3.2-11b-vision` on frames + existing audio/heuristics), 1–2 calls/video. Highest-priority feature work (C1).
- **Two gateways** — AgentRouter (`https://agentrouter.org/v1`, OpenAI-compatible, also `/audio/speech` + `/audio/transcriptions`) and Forge AI (`https://www.forge-ai.space/`, shape TBD via Test & Detect). Both **untrusted for production** — always keep a fallback; fail over on auth/credit errors.
- **ASR primary flips to AgentRouter `/audio/transcriptions`** (offloads CPU), local Whisper `small` the free fallback. Must return **word-level timestamps** or fall back + log why.
- **Dub translation** = minimax **and** Forge, per-chunk auto-selected by quality-per-second (R8). **Caption translation** stays minimax → Forge (separate cache key).
- **Primary LLM `minimaxai/minimax-m3`** (STEP 2 winner). **`z-ai/glm-5.2` dropped & disabled** — timed out 5/5, earlier hard fail, "most populated ship".
- **TTS: ElevenLabs** (emotion ✓, cloning ✓, SSML ✗) quality tier; **Chatterbox** (23 langs, free) volume tier.
- **NVIDIA endpoint** `https://integrate.api.nvidia.com/v1`, OpenAI-compatible. Canary/Nemotron ASR & Studio Voice may live on separate NIM/gRPC endpoints.
- **Studio Voice** = audio *enhancer* (gRPC), applied to **source** audio before transcription only — never to synthesized dub audio.
- **v0** (`https://api.v0.dev/v1`) **disabled** — web-code model, wrong for translation/vision.

## Open threads
- **LLM failover not yet wired** — priority order honoured but errors don't cascade. STEP 4.
- **STEP 4 must fix segment length-matching**: on the `small` transcript, dub drift was +40/−6/+131/+22/−52% (1 of 5 in ±15%). Root cause is **segmentation, not translation** — `small` yields 1.3–4.4s fragments that can't be length-matched ("Un sentier du capitaine!" 2.5s → "A captain's path!" 1.2s is correct but unfittable). Merge into 5–12s chunks before translating.
- **Latency is a scaling risk**: MiniMax 19/39/43/66/35s per segment — prohibitive at 200 clips/day. STEP 4 needs concurrent translation + per-job wall-clock logging.
- Re-run the translation benchmark on the corrected transcript — now easy via `benchmark-llm --models minimaxai/minimax-m3,...` (no per-model provider rows needed).
- ElevenLabs multilingual (de/it/es/ja/ar), cost controls, STEP 3.5b ASR — all **unverified on a real paid run**. Confirm ASR on footage: `benchmark-llm --source <video> --owner-confirmed --task transcribe`.

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
