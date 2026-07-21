# PROGRESS.md — ShortForge status

Single source of truth. Read this first every session. Keep it concise:
completed steps are one line (step · SHA · verified?); long detail lives in the
commit messages, not here. Statuses: ✅ done+verified · 🟡 done, unverified on
Windows · ⏳ in progress · ⬜ pending.

Branch: `claude/nifty-cray-n8l888` → PR #1 to `main`.

## Current position
**C1 (LLM hook detection) done, unverified — STOP for operator evaluation** on the
French source (compare modes with `python cli.py hooks`). Also landed this turn:
the zero-cost original-audio default path and the export-resolution selector.
After the operator evaluates C1, verify MSS-2…7 (they already exist) and ship.

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

## Plan of record — routing foundation → MINIMUM SHIPPABLE SET
Foundation (routing) so tasks can call real providers:
| Item | Status |
|---|---|
| R6 multi-model-per-credential schema + fetch-models | 🟡 verified in UI |
| R1 per-task provider binding (14 tasks) + built-ins (edge-tts, local-whisper) | 🟡 verified in UI |
| R2 cost-tier guard (model free/paid tier; free_only tasks hard-fail on paid) | 🟡 verified in UI |
| R7 (scoped) production adapters — Forge/minimax LLM via shared client + failover; **NVIDIA vision image-block adapter** (frame sampling→JPEG→batched request) | 🟡 |

**MINIMUM SHIPPABLE SET** — the operator's cut to publishable clips at volume.
Most already exist (Phases 1–4); only C1 is a real build:
| # | Item | Status |
|---|---|---|
| MSS-1 | **C1 — LLM hook detection** (transcript + llama-3.2 frames fused; mode a) — STOP for eval | 🟡 |
| MSS-2 | Karaoke / word-highlight captions | ✅ exists (`captions/ass.py`, templates+animations, word timings) — verify |
| MSS-3 | Logo overlay (corner, size, opacity) | ✅ exists (`brand/`, `brand.size` frac/px); gap: expose `--logo-size` flag |
| MSS-4 | Loudness norm ~-14 LUFS, TP ≤ -1 dBTP | ✅ exists (`render.loudnorm_i=-14`, `tp=-1.5` ⇒ satisfies ≤ -1) |
| MSS-5 | Metadata (title/desc/hashtags) per clip per language | ✅ exists (`metadata.generate`, driven by localized caption text); verify multilingual |
| MSS-6 | Thumbnail / cover-frame selection | ✅ exists (`thumbnail/`) — verify |
| MSS-7 | Batch mode (queue sources, resume on crash) | ✅ exists (`run-batch`, `--resume`) — verify |

Deferred until after the MSS ships (operator's call): STEP 4/5/6/7 dubbing polish
(prosody, diarization, timing — publish original-language first), STEP 9 audio
beds, STEP 10 transitions/effects, STEP 13 full web UI (CLI works), C1 mode (b)
`nemotron-omni` raw-media + mode (c) all-three fusion, `nemotron-nano-12b-v2-vl`,
Studio Voice, LipSync. R3 remaining adapters (OCR/TTS/gRPC/multipart) as needed.

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
- R6 bug-fixes (from operator's NVIDIA re-test) · unverified (235 tests pass):
  - BUG1 URL doubling regressed → one shared `normalize_base_url`/`normalize_api_url` used by probe + production + ASR + TTS; base normalized **on save** (a pasted `…/chat/completions` is stripped to base and shown); UI shows stored base → chat endpoint.
  - BUG2 model ids mangled → stored/sent **verbatim**; separate `display_name` field; UI shows the exact model id that will be sent.
  - BUG3 wrong adapter → `detect()` probes by **category**: TTS does a real `/audio/speech` synth (not "assume"), new **`ocr`** category probes with an image; vision/LLM stay chat. (Detection-level R3; production adapters still pending in R7.)
  - BUG4 fetch fails silently → `fetch_models_diag()` surfaces HTTP status + URL + body; UI shows the raw `/models` response and keeps manual entry available (fetch is optional).
  - GENERAL: every probe now records status + full URL + exact payload + response body, shown in the UI.
- R1 per-task provider binding + R2 cost-tier guard · unverified (245 tests pass): 14 tasks in `store.TASKS`, each with primary/secondary/fallback bindings, enable toggle, and paid_allowed; `resolve_task()` (explicit chain, else category priority order) and `task_paid_violation()`. Models gained a `tier` (free/paid/unknown). `free_only` tasks (vision-scoring, OCR, lip-sync, audio-enhance) can never be flipped to paid and only resolve `tier==free` models — else a hard-fail message. Settings UI "🎛️ Task routing" section + per-model tier selector.
- R1 built-ins: `edge-tts` (TTS) and `local-whisper` (ASR) are built-in free local backends, selectable in task routing (last-resort fallbacks); empty store resolves ASR→local Whisper, TTS-volume→edge-tts · unverified (249 tests pass).
- C1 LLM hook detection · unverified (276 tests pass): `detect/provider_hooks.py` — `score_transcript()` (batched LLM scores 0–1 via the hook_detection LLM contributor + failover), `score_frames_for()` (top-K candidates → `vision.score_frames`), `detect()` fuses them (`detect.frame_weight`). Wired into `detect_hooks` (auto-used when a hook LLM is bound; visible fallback to heuristic on error). `python cli.py hooks --source … --top N` prints candidates per mode (heuristic / transcript_llm / fusion) with timestamps, scores, justifications. **Fixes "0 strong standalone moments".** Modes (b) omni / (c) all-three deferred.
- Zero-cost original-audio default · unverified: translation/dub/Demucs already gate on `dub_on` (default off) — confirmed. Summary now reads `dub: none (original audio)`; hard `assert localize_on` guards the dub branch (no TTS resolved/called otherwise); `--no-dub` flag forces it. Default-mode paid calls = **hook detection + metadata only**.
- Export resolution selector · unverified: `reframe/resolution.py` — short-side px (1080p/720p/480p) or WxH, **separate from aspect**; `apply_resolution()` in the pipeline; `estimate_export()` (≈MB/clip + CPU render-speed) shown in wizard + UI. `--resolution` on run/run-batch, wizard prompt, UI selectbox. Default 1080p. Also exposed `--logo-size`.
- R7 (scoped) production adapters · unverified (264 tests pass): `providers/vision.py` — `sample_frames()` (ffmpeg, verified extracting 6 frames from a test clip), `encode_image()` (JPEG data URL), `build_vision_messages()` (text + image_url blocks), `score_frames()` (batched by `vision.max_images`, default 6) — all via the shared `openai_chat_raw`. `call_model_chat()`/`call_task_chat()` (resolve_task + failover) are the production LLM path for Forge `gpt-5.6-luna` + `minimax-m3`. **Image count/size limits are configurable** (`vision.max_images`/`frame_width`); the real NVIDIA limit must be confirmed on a live call. Deferred: nemotron-omni raw-media, nemotron-12b, Studio Voice, LipSync.
- R1 follow-ups (5 items) · unverified (258 tests pass): (1) hook_detection is a **fusion** task — `resolve_fusion()` treats the secondary as a parallel contributor whose score combines with the primary (not failover); UI labels it. (2) emotion_labelling flagged **batched** (one call/all segments). (3) `migrate_legacy()` folds legacy flat providers into credentials (grouped by URL+key, ids/priorities/toggles/bindings preserved, NVIDIA→"NVIDIA build"); UI legacy editing removed → one config path via a migrate button. (4) `run_failover()` cascade primitive (returns result + failovers list, raises if all fail) — confirms the chain cascades on error (R4 wires call sites). (5) `edge-tts` always registered as the TTS router's free final fallback (never ElevenLabs-only); volume-vs-quality *selection* still needs the dub path to consume task routing (R4).

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
- **Forge 401 (probe OK, CLI failed) — shared-code-path fix, awaiting operator re-run:** there were 5 separate `Bearer {key}` builders and none stripped the key. Now ONE `llm.bearer_header()` used by probe/CLI/UI/vision/TTS/ASR; keys stripped at the single load point (`_flatten`) and on save (`update_*`). UI model-probe persists before probing (no session-vs-disk drift). A 401 now names `credential=… key=••••1234 model=… url=… store=…` (redacted) so the exact key/store used is visible. Model id corrected to **`gpt-5.6-luna`**. If it still 401s, the diagnostic line will show whether the loaded key/store is wrong.
- **AgentRouter BLOCKED (optional, nothing depends on it):** HTTP 401 "unauthorized client detected" at the provider end (reproduced with a direct `requests.post` outside ShortForge; $125 balance, 0 requests ever recorded). Operator is raising it with their support. Treat as unavailable. Re-routed roster: premium reasoning/hook → **Forge `gpt-5.6-luna`**; translation → `minimax-m3` + Forge; ASR → **local Whisper `small`** (cached); TTS → **ElevenLabs** (quality) + **edge-tts** (volume). ⇒ the matrix's task-1 ASR primary and task-2 hook primary shift off AgentRouter accordingly.
- **Confirmed working roster** (bind these in Task routing): Forge `gpt-5.6-luna` (LLM), NVIDIA `meta/llama-3.2-11b-vision-instruct` + `nemotron-3-nano-omni-30b-a3b-reasoning` (video+audio) + `nemotron-nano-12b-v2-vl` (video-native) — all Vision/free, ElevenLabs (TTS quality), edge-tts built-in (TTS volume), `minimaxai/minimax-m3` (LLM), `z-ai/glm-5.2` (LLM, disabled).
- **Hook detection has NEVER run with an LLM** — every run so far reported "0 strong standalone moments" from the heuristic scorer. C1 must wire the LLM hook scorer (+ fusion) and then benchmark the 3 modes on the French source.
- **C1 hook-detection fusion (operator requirement, build in C1):** make the mode configurable so the operator can compare (a) transcript LLM + `meta/llama-3.2-11b-vision-instruct` frame scoring fused, (b) `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` on the RAW media (video+audio, the only model that ingests both — use it as the hook media-analysis path, 1–2 calls/video, never per-segment/frame), or (c) all three fused. Vision frame-scoring fallback: `nvidia/nemotron-nano-12b-v2-vl` (video-native) behind llama-3.2 (image-only). All vision stays FREE (`vision_scoring`/`ocr` are free_only). **Rule:** only add a model that appears in the credential's fetched `/v1/models`; if it doesn't, it's a NIM container — skip. Skipped by operator: cosmos3-nano-reasoner (robotics), paligemma (older, beaten by llama-3.2).
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
