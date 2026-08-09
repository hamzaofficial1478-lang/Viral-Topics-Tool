#!/usr/bin/env python3
"""ShortForge CLI — Phase 1 core clipper.

    python cli.py run <video-url-or-path> --owner-confirmed
    python cli.py wizard        # interactive, Section 7 order

Also runnable as `python -m shortforge`.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time

from shortforge.captions.templates import ANIMATIONS, list_templates
from shortforge.config import Config
from shortforge.pipeline import run_pipeline
from shortforge.publish import write_index
from shortforge.research import categories as niche_categories, personalize, suggest
from shortforge.utils import ShortForgeError, load_env_file, setup_logging, log


def _apply_common_overrides(cfg: Config, args: argparse.Namespace) -> None:
    cfg.override("select.target_duration", getattr(args, "duration", None))
    cfg.override("select.tolerance", getattr(args, "tolerance", None))
    cfg.override("select.num_clips", getattr(args, "num_clips", None))
    cfg.override("reframe.aspect", getattr(args, "aspect", None))
    cfg.override("reframe.resolution", getattr(args, "resolution", None))
    cfg.override("render.encoder", getattr(args, "encoder", None))
    cfg.override("brand.size", getattr(args, "logo_size", None))
    cfg.override("reframe.fill", getattr(args, "fill", None))
    cfg.override("transcribe.model", getattr(args, "whisper_model", None))
    cfg.override("transcribe.language", getattr(args, "source_lang", None))
    cfg.override("transcribe.min_confidence", getattr(args, "min_confidence", None))
    cfg.override("paths.work_dir", getattr(args, "work_dir", None))
    cfg.override("paths.output_dir", getattr(args, "output", None))
    cfg.override("ingest.cookies", getattr(args, "cookies", None))
    cfg.override("ingest.cookies_from_browser", getattr(args, "cookies_from_browser", None))
    cfg.override("reframe.mode", getattr(args, "reframe_mode", None))
    cfg.override("captions.style", getattr(args, "caption_style", None))
    cfg.override("captions.template", getattr(args, "caption_template", None))
    cfg.override("captions.animation", getattr(args, "caption_animation", None))
    cfg.override("captions.font", getattr(args, "caption_font", None))
    cfg.override("captions.font_size", getattr(args, "caption_size", None))
    cfg.override("captions.highlight_color", getattr(args, "highlight_color", None))
    cfg.override("captions.burned_in", getattr(args, "burned_in", None))
    if getattr(args, "uppercase", False):
        cfg.override("captions.uppercase", True)
    cfg.override("brand.logo", getattr(args, "logo", None))
    cfg.override("brand.corner", getattr(args, "logo_corner", None))
    cfg.override("brand.opacity", getattr(args, "logo_opacity", None))
    cfg.override("page.niche", getattr(args, "niche", None))
    if getattr(args, "vision", False):
        cfg.override("detect.vision_llm", True)
    if getattr(args, "hook_frames", False):
        cfg.override("detect.hook_frames", True)
    if getattr(args, "no_visual", False):
        cfg.override("detect.visual", False)
    if getattr(args, "debug_reframe", False):
        cfg.override("reframe.debug", True)
    cfg.override("localize.language", getattr(args, "language", None))
    cfg.override("localize.tts_backend", getattr(args, "tts", None))
    cfg.override("localize.voice_sample", getattr(args, "voice_sample", None))
    cfg.override("localize.translate_backend", getattr(args, "translate_backend", None))
    if getattr(args, "dub", False):
        cfg.override("localize.dub", True)
    if getattr(args, "no_dub", False):
        cfg.override("localize.dub", False)     # force original-audio-only (default)
    dub_mode = getattr(args, "dub_mode", None)
    if dub_mode:
        if dub_mode == "captions":
            cfg.override("localize.dub", False)
        else:
            cfg.override("localize.dub", True)
            if dub_mode == "clone":
                cfg.override("localize.tts_backend", "xtts")
    cfg.override("localize.stem_separation", getattr(args, "stem_separation", None))
    cfg.override("localize.stem_model", getattr(args, "stem_model", None))
    cfg.override("localize.vocal_removal_strength", getattr(args, "vocal_removal_strength", None))
    if getattr(args, "debug_audio", False):
        cfg.override("localize.debug_audio", True)
    if getattr(args, "allow_voice_bleed", False):
        cfg.override("localize.allow_voice_bleed", True)
    if getattr(args, "allow_untranslated", False):
        cfg.override("localize.allow_untranslated", True)
    if getattr(args, "no_cache", False):
        cfg.override("cache.disabled", True)
    if getattr(args, "dry_run_cost", False):
        cfg.override("cost.dry_run", True)
    cfg.override("cost.max_usd_per_job", getattr(args, "cost_ceiling", None))
    if getattr(args, "yes", False):
        cfg.override("cost.confirm", False)
    if getattr(args, "refresh_translation", False):
        cfg.override("cache.refresh_translation", True)
    if getattr(args, "lipsync", False):
        cfg.override("lipsync.enabled", True)
    cfg.override("lipsync.wav2lip_repo", getattr(args, "wav2lip_repo", None))
    cfg.override("lipsync.checkpoint", getattr(args, "wav2lip_checkpoint", None))
    if getattr(args, "no_review", False):
        cfg.override("review.enabled", False)
    if getattr(args, "no_captions", False):
        cfg.override("captions.enabled", False)
    if getattr(args, "no_loudnorm", False):
        cfg.override("render.loudnorm", False)
    if getattr(args, "jumpcuts", False):
        cfg.override("edit.jumpcuts", True)
    # STEP 9: metadata/thumbnail are opt-in (off by default). --metadata/--thumbnail
    # turn them on; the legacy --no-* flags still force them off.
    if getattr(args, "metadata", False):
        cfg.override("metadata.enabled", True)
    if getattr(args, "thumbnail", False):
        cfg.override("thumbnail.enabled", True)
    if getattr(args, "no_metadata", False):
        cfg.override("metadata.enabled", False)
    if getattr(args, "no_thumbnail", False):
        cfg.override("thumbnail.enabled", False)
    if getattr(args, "no_sidecar", False):
        cfg.override("publish.sidecar", False)
    if getattr(args, "resume", False):
        cfg.override("render.resume", True)
    backend = getattr(args, "backend", None)
    if backend:
        cfg.override("detect.backend", backend)


def _print_summary(manifest: dict) -> None:
    src = manifest["source"]
    print("\n" + "=" * 64)
    print(f"  Source : {src['title'] or src['source']}")
    print(f"  Length : {src['duration']:.0f}s   Language: {src['language']}")
    print(f"  Output : {manifest['settings']['resolution']}  "
          f"({manifest['settings']['aspect']}, fill={manifest['settings']['fill']})")
    print(f"  {manifest['recommendation']['rationale']}")
    print("-" * 64)
    for c in manifest["clips"]:
        print(f"  clip {c['clip_id']}  [{c['start']:.1f}-{c['end']:.1f}]  "
              f"{c['duration']:.1f}s  score={c['score']:.2f}")
        print(f"           {c['file_path']}")
        if c.get("reason"):
            print(f"           why: {c['reason']}")
    print("-" * 64)
    if manifest.get("summary"):
        print(f"  {manifest['summary']}")
    print(f"  {len(manifest['clips'])} clip(s) + manifest -> {manifest['manifest_path']}")
    print("=" * 64)


def _cost_confirm(cfg):
    """Return a callback the pipeline calls with a cost estimate before spending."""
    if not bool(cfg.get("cost.confirm", True)):
        return lambda est: True

    def confirm(est) -> bool:
        try:
            ans = input(f"\nEstimated dub cost: {est.human()}\nProceed? (yes/no): ")
        except EOFError:
            return True   # non-interactive (piped): don't block
        return ans.strip().lower().startswith("y")
    return confirm


def cmd_run(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")
    cfg = Config.load(args.config)
    _apply_common_overrides(cfg, args)
    try:
        manifest = run_pipeline(
            args.source,
            cfg,
            owner_confirmed=args.owner_confirmed,
            transcript_path=args.transcript,
            confirm_cost=_cost_confirm(cfg),
        )
    except ShortForgeError as e:
        log.error("%s", e)
        return 2
    _print_summary(manifest)
    return 0


def _collect_batch_sources(args: argparse.Namespace) -> list[str]:
    sources = list(args.sources or [])
    if getattr(args, "from_file", None):
        with open(args.from_file, "r", encoding="utf-8") as f:
            for line in f:
                s = line.split("#", 1)[0].strip()
                if s:
                    sources.append(s)
    return sources


def cmd_batch(args: argparse.Namespace) -> int:
    """Run the pipeline over several of the operator's own videos, in sequence.

    Deliberately lean: no job queue, no database — just a loop that keeps going
    when one source fails, then writes one combined index. That covers the real
    scale need (a backlog of your own videos) without the ops overhead.
    """
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")

    sources = _collect_batch_sources(args)
    if not sources:
        log.error("run-batch needs at least one source (positional args or --from-file).")
        return 2
    if not args.owner_confirmed:
        log.error("Batch requires --owner-confirmed (it applies to every source).")
        return 2
    if getattr(args, "transcript", None):
        log.warning("--transcript is ignored in batch mode (one file can't fit every source)")

    out_dir = None
    results: list[dict] = []
    failures: list[dict] = []
    for i, src in enumerate(sources, 1):
        log.info("=== batch %d/%d: %s ===", i, len(sources), src)
        cfg = Config.load(args.config)
        _apply_common_overrides(cfg, args)
        out_dir = cfg.get("paths.output_dir", "out")
        try:
            manifest = run_pipeline(src, cfg, owner_confirmed=True, transcript_path=None,
                                    confirm_cost=lambda est: True)  # batch: ceiling still applies
        except ShortForgeError as e:
            log.error("source failed (%s): %s", src, e)
            failures.append({"source": src, "error": str(e)})
            continue
        _print_summary(manifest)
        results.append(manifest)

    total_clips = sum(len(m["clips"]) for m in results)
    index = {
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "sources_requested": len(sources),
        "sources_succeeded": len(results),
        "sources_failed": len(failures),
        "total_clips": total_clips,
        "failures": failures,
        "runs": [
            {
                "source": m["source"]["source"],
                "title": m["source"]["title"],
                "clips": len(m["clips"]),
                "manifest": m.get("manifest_path"),
            }
            for m in results
        ],
    }
    index_path = None
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        name = f"batch_{_dt.date.today():%Y%m%d}_index.json"
        index_path = write_index(index, out_dir, name)

    print("\n" + "#" * 64)
    print(f"  BATCH: {len(results)}/{len(sources)} source(s) OK, "
          f"{len(failures)} failed  ->  {total_clips} clip(s) total")
    if failures:
        for f in failures:
            print(f"   ✗ {f['source']}: {f['error']}")
    if index_path:
        print(f"  index -> {os.path.abspath(index_path)}")
    print("#" * 64)
    return 0 if not failures else 1


def _print_niches(results: list[dict], sort_by: str) -> None:
    print("\n" + "=" * 68)
    print(f"  Growing short-form niches  (sorted by {sort_by})")
    print("  score = growth + monetization + room-to-grow (less competition)")
    print("=" * 68)
    for i, n in enumerate(results, 1):
        print(f"{i:2d}. {n['name']}  [{n['category']}]   score {n['opportunity']}")
        print(f"      growth: {n['growth']} · competition: {n['competition']} · "
              f"pays: {n['monetization']}")
        print(f"      who: {n['audience']}")
        print(f"      why: {n['why']}")
        print(f"      angles: {', '.join(n['subniches'])}")
    print("=" * 68)
    print("  Tip: the lower-competition sub-niche 'angles' are usually where a")
    print("  new channel grows fastest. Filter with --category / --low-competition.")
    print("=" * 68)


def cmd_niches(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")
    cfg = Config.load(args.config)
    results = suggest(
        cfg,
        category=args.category,
        top=args.top,
        sort_by=args.sort,
        low_competition=args.low_competition,
    )
    if not results:
        log.error("No niches matched. Categories: %s", ", ".join(niche_categories()))
        return 2
    _print_niches(results, args.sort)
    if args.interests:
        note = personalize(args.interests, cfg, results)
        if note:
            print("\nPersonalized for you (Claude):\n")
            print(note)
        else:
            print("\n(Personalization needs ANTHROPIC_API_KEY + `pip install anthropic`; "
                  "showing the curated ranking above.)")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")
    cfg = Config.load(args.config)
    from shortforge.doctor import run_checks, format_report, FAIL
    checks = run_checks(cfg)
    print(format_report(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


def cmd_check_llm(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")
    from shortforge.llm import check
    ok, detail = check()
    print(detail)
    return 0 if ok else 1


def cmd_ui(args: argparse.Namespace) -> int:
    """Launch the Streamlit dashboard (job + settings screens).

    Arms its OWN shutdown notice, independent of `cli.py listen`'s
    (`lifecycle.UI_STATE_FILE`, not the worker's `runstate.json` — see the
    module docstring on why they must never share a file). `start_all.bat`
    opens this in its own window alongside `listen`; an operator closing
    JUST that window — thinking of it as "the program" — got no notification
    at all before this, since only `listen`/`queue run` had exit handling.
    Deliberately says nothing about the queue's state (`include_queue_detail
    =False`): whether this window is open has no bearing on whether the
    worker is still running and rendering.
    """
    import subprocess
    import shutil
    from shortforge import lifecycle

    app = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.py")
    if shutil.which("streamlit") is None:
        log.error("Streamlit is not installed. Run: pip install streamlit")
        return 1

    work_dir = Config.load(getattr(args, "config", None)).get("paths.work_dir", ".shortforge")
    lifecycle.install_exit_notice(
        work_dir, state_file=lifecycle.UI_STATE_FILE,
        icon="🖥️", title="ShortForge dashboard closed",
        include_queue_detail=False)

    log.info("launching ShortForge UI (Ctrl+C to stop) …")
    return subprocess.call(["streamlit", "run", app])


def _fmt_hms(seconds: float) -> str:
    m, s = divmod(int(max(0, seconds)), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _queue_summary_message(s: dict, out_dir: str) -> str:
    """The end-of-run notification, chosen from why the drain actually stopped.

    The bug this closes: the summary used to be a single unconditional "🏁 All
    links processed", sent no matter what `drain_queue` had done. A drain that
    exited immediately — queue paused, or another worker holding the lock —
    returned a dict that looked exactly like a completed run, so the operator
    was told every link was processed while their link had never started. A
    message that looks successful but isn't is the failure mode CLAUDE.md
    rule 1 exists to prevent, so the reason is now load-bearing.
    """
    from shortforge.runner import fmt_hms

    why = s.get("stopped_because", "empty")
    pending = s.get("pending", 0)
    if why == "locked":
        return ("⏭️ <b>Already working</b> — another ShortForge worker is running this "
                "queue, so this extra run exited without touching it. "
                f"{pending} link(s) still pending.")
    if why == "paused":
        return ("⏸ <b>Queue is paused</b> — nothing was started. "
                f"{pending} link(s) still pending; reply \"start\" (or press "
                "▶ Start working) to begin.")
    if why == "asked_to_stop":
        return (f"🛑 <b>Stopped early</b> — {s.get('made', 0)} clip(s) made this run, "
                f"{pending} link(s) still pending.")
    return (f"🏁 <b>All links processed</b>\n"
            f"{s.get('done', 0)} done, {s.get('failed', 0)} failed\n"
            f"<b>{s.get('total_clips', 0)} clip(s)</b> total in "
            f"{fmt_hms(s.get('elapsed', 0))}\nOutput: {out_dir}")


def cmd_queue(args: argparse.Namespace) -> int:
    """Persistent link queue: add links with per-link settings, then work through
    them one at a time, surviving restarts and power cuts."""
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")
    from shortforge import queue as Q
    from shortforge import notify as N

    cfg0 = Config.load(args.config)
    work_dir = cfg0.get("paths.work_dir", ".shortforge")
    q = Q.load_queue(work_dir)
    action = args.queue_action

    if action == "add":
        urls = list(args.urls or [])
        if getattr(args, "from_file", None):
            with open(args.from_file, "r", encoding="utf-8") as f:
                urls += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        if not urls:
            log.error("queue add needs at least one URL (or --from-file).")
            return 2
        settings = {k: getattr(args, k, None) for k in Q.JOB_SETTINGS}
        for u in urls:
            job = Q.add_job(q, u, settings, label=getattr(args, "label", "") or "")
            print(f"  + {job['id']}  {u}" + (f"   {job['settings']}" if job["settings"] else ""))
        Q.save_queue(q, work_dir)
        print(Q.describe(q))
        return 0

    if action == "list":
        jobs = q.get("jobs", [])
        if not jobs:
            print("Queue is empty. Add links with:  python cli.py queue add <url> ...")
            return 0
        for i, j in enumerate(jobs, 1):
            mark = {"pending": "…", "running": "▶", "done": "✓", "failed": "✗"}.get(j["status"], "?")
            extra = f"  {len(j['clips'])} clip(s)" if j["clips"] else ""
            err = f"  ERROR: {str(j['error'])[:80]}" if j.get("error") else ""
            print(f" {mark} {i:2d}. [{j['id']}] {j['url'][:70]}{extra}{err}")
            if j["settings"]:
                print(f"        settings: {j['settings']}")
        print(Q.describe(q))
        return 0

    if action == "clear":
        keep = [j for j in q.get("jobs", []) if j["status"] in (Q.PENDING, Q.RUNNING)] \
            if args.done_only else []
        removed = len(q.get("jobs", [])) - len(keep)
        q["jobs"] = keep
        Q.save_queue(q, work_dir)
        print(f"Removed {removed} job(s). {Q.describe(q)}")
        return 0

    # --- run ---------------------------------------------------------------- #
    from shortforge import lifecycle
    prev = lifecycle.mark_online(work_dir, mode="queue")
    lifecycle.install_exit_notice(work_dir)      # say goodbye however we're closed

    resumed = Q.requeue_interrupted(q)
    if resumed:
        log.info("resuming: %d job(s) were interrupted and are back in the queue", resumed)
    Q.save_queue(q, work_dir)

    if not Q.next_pending(q):
        print("Nothing pending. " + Q.describe(q))
        lifecycle.clear_state(work_dir)
        return 0

    hard_stop = lifecycle.interrupted_note(prev)
    started_msg = ("▶️ <b>ShortForge started</b>\n"
                   + (hard_stop + "\n" if hard_stop else "")
                   + Q.describe(q)
                   + (f"\nResumed {resumed} interrupted job(s)." if resumed else ""))
    N.notify(started_msg)

    from shortforge.runner import drain_queue
    try:
        with N.KeepAwake():                  # don't let Windows sleep mid-queue
            s = drain_queue(lambda: _cfg_for_queue(args), work_dir)
    except KeyboardInterrupt:
        q = Q.load_queue(work_dir)
        n = Q.requeue_interrupted(q)         # the running job goes back in the queue
        Q.save_queue(q, work_dir)
        log.warning("interrupted — %d job(s) stay queued and resume on the next run", n)
        lifecycle.announce_offline(work_dir, "stopped with Ctrl+C")
        return 130

    why = s.get("stopped_because", "empty")
    summary = _queue_summary_message(s, os.path.abspath(cfg0.get("paths.output_dir", "out")))
    log.info("queue finished (%s): %s", why, Q.describe(Q.load_queue(work_dir)))
    N.notify(summary)
    print(Q.describe(Q.load_queue(work_dir)))
    if why in ("locked", "paused"):
        # Did nothing and is exiting within a second of starting. The summary
        # above already said why; adding "🔴 ShortForge stopped" on top reads as
        # the whole program going down while the dashboard/listener are in fact
        # still running — that three-message burst is what the operator saw.
        lifecycle.cancel_goodbye(work_dir)
    return 0 if s["failed"] == 0 else 1      # the atexit hook sends the goodbye


def _listener_already_running(existing: dict | None, own_pid: int,
                              *, freshness_s: float = 15.0) -> bool:
    """True only when ``existing`` (a `runstate.json` snapshot) describes a
    DIFFERENT process that is both alive and has checked in recently.

    Both conditions matter independently: a live PID with a stale
    ``last_seen`` (e.g. a debugger paused it, or the clock jumped) must not
    block a restart, and neither must a fresh timestamp belonging to a PID
    that's actually dead (Windows can reuse PIDs). Pulled out as its own
    function so the exact refuse/allow boundary is unit-testable without
    driving `cmd_listen`'s threads.
    """
    from shortforge.utils import pid_alive

    if not existing or existing.get("pid") == own_pid:
        return False
    age = time.time() - float(existing.get("last_seen") or 0)
    return age < freshness_s and pid_alive(int(existing.get("pid") or 0))


def cmd_listen(args: argparse.Namespace) -> int:
    """Open the dashboard, listen for ntfy commands, and work the queue — the
    phone-driven mode.

    Two threads: the ntfy command listener, and a worker that drains the queue.
    Both talk through the queue file, so either can restart without losing
    anything. Nothing is ever auto-started: every session comes up PAUSED
    whenever there's pending or interrupted work, and stays that way until the
    operator explicitly replies "start" (or /resume) on the command topic — a
    silent auto-resume after a crash/reboot is exactly the surprise this is
    designed not to spring.
    """
    import threading
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")
    from shortforge import notify as N, queue as Q
    from shortforge import ntfy_bot as NB
    from shortforge.runner import drain_queue

    if not NB.configured():
        log.error("No remote control configured. Open Settings → Notifications and set "
                  "an ntfy COMMAND topic.")
        return 2

    from shortforge import lifecycle

    cfg0 = Config.load(args.config)
    work_dir = cfg0.get("paths.work_dir", ".shortforge")

    # Refuse to start a second listener on top of one that's already live: two
    # ntfy pollers on the same command topic would each act on the same
    # incoming message (double-queuing every link sent from the phone), and
    # there's no dedup against that once handle_text() has run.
    existing = lifecycle.read_state(work_dir)
    if _listener_already_running(existing, os.getpid()):
        log.error("ShortForge is already listening (pid %s). Not starting a second "
                  "instance on this folder — close that one first if you meant to "
                  "restart it.", existing.get("pid"))
        N.notify("⚠️ <b>Already running</b> — a second ShortForge just tried to "
                 "start but one is already listening. This new one is exiting; "
                 "the original keeps working.")
        return 2

    prev = lifecycle.mark_online(work_dir, mode="listen")
    lifecycle.install_exit_notice(work_dir)
    q = Q.load_queue(work_dir)
    resumed = Q.requeue_interrupted(q)
    has_work = Q.next_pending(q) is not None
    if has_work:
        Q.set_paused(q, True)        # always ask first — see the permission gate below
    Q.save_queue(q, work_dir)

    stop = threading.Event()

    def _worker():
        """Drain the queue whenever unpaused and something is pending; idle quietly
        otherwise (this is also how the startup permission gate holds: paused
        stays paused until an explicit reply flips it).

        No per-drain KeepAwake here — the whole `listen` process holds it
        continuously (below) for as long as it's running, idle or not. A link
        sent from the phone only does any good if the machine is still awake
        to receive it; sleep must never kick in just because nothing HAPPENED
        to be rendering at that exact moment.

        The idle branch also refreshes the heartbeat: `drain_queue` only ticks
        `last_seen` while a job is actually running, so a long idle stretch
        (normal for phone-driven use — most of the time is spent waiting) would
        otherwise make the dashboard's "is a worker running?" check, and the
        duplicate-instance guard above, go stale and wrongly say no.
        """
        while not stop.is_set():
            _q = Q.load_queue(work_dir)
            if Q.is_paused(_q) or Q.next_pending(_q) is None:
                lifecycle.heartbeat(work_dir)
                stop.wait(5)
                continue
            s = drain_queue(lambda: _cfg_for_queue(args), work_dir,
                            should_stop=stop.is_set)
            if s["made"]:
                N.notify(f"🏁 <b>Queue empty</b>\n{s['done']} done, {s['failed']} failed\n"
                         f"<b>{s['total_clips']} clip(s)</b> total")

    threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=NB.listen, args=(work_dir,),
                     kwargs={"stop": stop.is_set}, daemon=True).start()

    # What the operator wants to read after a reboot: that the UI is open, what
    # state things were left in, and — the point of the permission gate — that
    # nothing runs until they say so.
    q = Q.load_queue(work_dir)
    pending = Q.counts(q)[Q.PENDING]
    total_clips = Q.total_clips(q)
    nxt = Q.next_pending(q)
    hard_stop = lifecycle.interrupted_note(prev, total_clips=total_clips)
    lines = ["🖥️ <b>ShortForge UI is open</b> — http://localhost:8501"]
    if hard_stop:
        lines.append(hard_stop)
    elif total_clips:
        lines.append(f"📊 {total_clips} clip(s) produced so far.")
    if resumed:
        lines.append(f"↩️ {resumed} interrupted link(s) back in the queue.")
    if has_work:
        lines.append(f"⏳ {pending} link(s) queued — next: {nxt['url'][:70]} "
                     f"({Q.describe_settings(nxt)}).")
        lines.append('▶️ Reply "start" (or /resume) to begin, or "pause" to leave it '
                     "stopped. Nothing runs until you say so.")
    else:
        lines.append("Nothing queued yet — send me a link, or /help.")
    N.notify("\n".join(lines))
    log.info("listening for commands (paused=%s). Ctrl+C to stop.", has_work)

    # Held for the ENTIRE listening lifetime, not just while a job is actively
    # rendering: Windows' idle-sleep timer would otherwise suspend this whole
    # process (ntfy polling included) during any quiet stretch, silently
    # cutting off phone control until someone physically touches the machine.
    # The screen can still blank normally (KeepAwake only blocks SYSTEM sleep).
    with N.KeepAwake("ShortForge is listening for phone commands"):
        try:
            while not stop.is_set():
                stop.wait(5)
        except KeyboardInterrupt:
            log.info("listen: stopping…")
            stop.set()
            lifecycle.announce_offline(work_dir, "stopped with Ctrl+C")
            return 0
    return 0


def _cfg_for_queue(args) -> Config:
    """A fresh Config per job with the run-wide CLI overrides applied."""
    cfg = Config.load(getattr(args, "config", None))
    _apply_common_overrides(cfg, args)
    return cfg


def cmd_netdiag(args: argparse.Namespace) -> int:
    """Pinpoint WHICH network layer is blocking ntfy (DNS/TCP/TLS/HTTP)."""
    setup_logging(args.verbose)
    from shortforge.netdiag import report
    print(report(host=getattr(args, "host", None),
                 timeout=int(getattr(args, "timeout", 10) or 10),
                 proxy=getattr(args, "proxy", None)))
    return 0


def cmd_encode_sample(args: argparse.Namespace) -> int:
    """STEP 1: render the SAME slice with x264 and each hardware encoder so the
    operator can judge quality vs speed side-by-side before switching the default."""
    import time as _t
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")
    from shortforge.ingest import ingest
    from shortforge.reframe import apply_resolution, parse_aspect, build_filtergraph
    from shortforge.render import available_hw_encoders, video_encode_args
    from shortforge.utils import ffprobe_info, require_binary, run, format_timestamp

    cfg = Config.load(args.config)
    if args.resolution:
        cfg.override("reframe.resolution", args.resolution)
    meta = ingest(args.source, cfg, owner_confirmed=args.owner_confirmed)
    apply_resolution(cfg)
    out_w, out_h = parse_aspect(cfg.get("reframe.aspect", "9:16"),
                                int(cfg.get("reframe.width", 1080)),
                                int(cfg.get("reframe.height", 1920)))
    probe = ffprobe_info(meta.file_path)
    start, dur = float(args.start), float(args.duration)
    fg = build_filtergraph(probe.width, probe.height, out_w, out_h, fill="crop")
    out_dir = cfg.get("paths.output_dir", "out")
    os.makedirs(out_dir, exist_ok=True)
    ffmpeg = require_binary("ffmpeg")

    encoders = ["libx264"] + available_hw_encoders()
    print(f"\nEncoding a {dur:.0f}s sample at {out_w}x{out_h} with: {', '.join(encoders)}\n"
          f"(no fallback — a hardware failure is shown as FAILED so you see the real result)\n")
    for enc in encoders:
        outp = os.path.join(out_dir, f"encode-sample_{enc}.mp4")
        cmd = ([ffmpeg, "-y", "-ss", format_timestamp(start), "-t", f"{dur:.3f}",
                "-i", meta.file_path, "-filter_complex", fg, "-map", "[v]", "-map", "0:a:0?"]
               + video_encode_args(cfg, enc)
               + ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", outp])
        t0 = _t.time()
        try:
            run(cmd)
            dt, sz = _t.time() - t0, os.path.getsize(outp) / 1e6
            print(f"  {enc:12s}  {dt:6.1f}s encode   {sz:6.1f} MB   -> {outp}")
        except Exception as e:  # noqa: BLE001
            print(f"  {enc:12s}  FAILED: {str(e).splitlines()[-1][:120]}")
    print("\nOpen these files side by side at 100% and compare sharpness/artifacts. If a "
          "hardware encoder looks equivalent, set it in Settings (or --encoder qsv). If it "
          "looks softer, lower render.qsv_quality (e.g. 20) to raise the bitrate — hardware "
          "at a higher bitrate is still much faster than x264.\n")
    return 0


def cmd_check_providers(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")
    from shortforge.providers import check_providers
    print(check_providers())
    return 0


def _print_benchmark(results: dict) -> None:
    print("\n" + "=" * 78)
    if results["task"] == "transcribe":
        print(f"  ASR BENCHMARK — transcribe   audio {results['duration_s']}s"
              f"   (reference: {results['reference'] or 'n/a'})")
        print("=" * 78)
        print(f"    {'backend/model':<32} {'segs':>4} {'words':>5} {'conf':>5} "
              f"{'low':>4} {'diverge':>7} {'lat':>7}  cost")
        print("    " + "-" * 74)
        for r in results["backends"]:
            bm = f"{r['provider']}/{r['model']}"[:32]
            if r["error"]:
                print(f"    {bm:<32} ERR  {str(r['error'])[:44]}")
                continue
            mc = f"{r['mean_conf']:.2f}" if r["mean_conf"] is not None else "  — "
            dv = f"{r['divergence_pct']}%" if r["divergence_pct"] is not None else "  — "
            cost = f"${r['cost']:g}" if r["cost"] is not None else " n/a"
            print(f"    {bm:<32} {r['segments']:>4} {r['words']:>5} {mc:>5} "
                  f"{r['low_conf']:>4} {dv:>7} {r['latency_ms']:>5}ms  {cost}")
            print(f"      → {r['text'][:130]}")
        print("=" * 78)
        print("  Lower divergence vs the reference and higher confidence = cleaner transcript.")
        print("  There is no ground truth — read the samples above to judge which is right.")
        print("=" * 78)
        return
    if results["task"] == "translate":
        print(f"  LLM BENCHMARK — translate  {results['src_lang']} → {results['target_lang']}"
              f"   (±15% duration target)")
        for i, seg in enumerate(results["segments"], 1):
            print("=" * 78)
            print(f"  Segment {i}  (source {seg['source_s']}s):")
            print(f"    « {seg['source_text'][:100]} »")
            print(f"    {'provider/model':<34} {'chars':>5} {'est/src':>9} {'drift':>7} {'lat':>7}  fit")
            print("    " + "-" * 70)
            for r in seg["results"]:
                pm = f"{r['provider']}/{r['model']}"[:34]
                fit = "OK" if r["within_tol"] else "!!"
                if r["status"] != 200:
                    print(f"    {pm:<34} {'ERR':>5}  {str(r.get('error',''))[:40]}")
                    continue
                print(f"    {pm:<34} {r['chars']:>5} {r['est_spoken_s']:>4}/{r['source_s']:<4} "
                      f"{r['drift_pct']:>+6}% {r['latency_ms']:>5}ms  {fit}")
                print(f"      → {r['output'][:120]}")
    else:
        print("  LLM BENCHMARK — hooks")
        for r in results["providers"]:
            print("=" * 78)
            print(f"  {r['provider']}/{r['model']}   ({r['latency_ms']}ms)"
                  + (f"   ERROR: {r['error']}" if r.get("error") else ""))
            for s in r.get("top", []):
                print(f"    [{s.get('score')}] #{s.get('index')}  {s.get('reason','')[:80]}")
    print("=" * 78)


def cmd_benchmark_llm(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")
    cfg = Config.load(args.config)
    _apply_common_overrides(cfg, args)

    src = args.source
    ext = os.path.splitext(src)[1].lower()

    # --task transcribe compares ASR backends and needs actual audio, not a transcript.
    if args.task == "transcribe":
        if ext in (".json", ".srt"):
            log.error("--task transcribe needs an audio/video source (your own footage), "
                      "not a transcript file.")
            return 2
        if not args.owner_confirmed:
            log.error("--owner-confirmed is required when --source is a media file/URL.")
            return 2
        try:
            from shortforge.analyze.transcribe import extract_audio
            from shortforge.benchmark import render_markdown, run_transcribe
            from shortforge.cache import Cache
            from shortforge.ingest import ingest
            meta = ingest(src, cfg, owner_confirmed=True)
            cache = Cache(cfg.get("paths.work_dir", ".shortforge"), meta.hash)
            wav = extract_audio(meta.file_path, cache.path("audio16k.wav"))
            results = run_transcribe(wav, cfg, language=cfg.get("transcribe.language"),
                                     duration=meta.duration)
        except ShortForgeError as e:
            log.error("%s", e)
            return 2
        if not results["backends"]:
            log.error("No ASR backend is available. Install faster-whisper "
                      "(`pip install faster-whisper`) or add an ASR provider in the settings UI.")
            return 2
        _print_benchmark(results)
        out_dir = cfg.get("paths.output_dir", "out")
        os.makedirs(out_dir, exist_ok=True)
        base = os.path.join(out_dir, f"benchmark_transcribe_{_dt.date.today():%Y%m%d}")
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        with open(base + ".md", "w", encoding="utf-8") as f:
            f.write(render_markdown(results))
        print(f"\nWritten: {base}.json  and  {base}.md")
        return 0

    try:
        if ext in (".json", ".srt"):
            from shortforge.analyze import load_external_transcript
            transcript = load_external_transcript(src, 0.0, "auto")
        else:
            from shortforge.analyze import transcribe
            from shortforge.cache import Cache
            from shortforge.ingest import ingest
            if not args.owner_confirmed:
                log.error("--owner-confirmed is required when --source is a media file/URL "
                          "(or pass a cached transcript .json/.srt to --source).")
                return 2
            meta = ingest(src, cfg, owner_confirmed=True)
            cache = Cache(cfg.get("paths.work_dir", ".shortforge"), meta.hash)
            transcript = transcribe(meta, cfg, cache)   # reuses the cached transcript
    except ShortForgeError as e:
        log.error("%s", e)
        return 2

    from shortforge.benchmark import (candidate_entries, enabled_llms, render_markdown,
                                       run_hooks, run_translate)
    providers = enabled_llms(cfg)
    if getattr(args, "models", None):
        if not providers:
            log.error("--models needs one configured LLM provider to borrow the endpoint + key "
                      "from. Add one in the settings UI (python cli.py ui) first.")
            return 2
        providers = candidate_entries(args.models, providers[0])
        if not providers:
            log.error("--models was empty. Pass e.g. --models minimaxai/minimax-m3,z-ai/glm-5.2")
            return 2
        log.info("candidate models on %s: %s", providers[0].base_url,
                 ", ".join(p.model for p in providers))
    if not providers:
        log.error("No enabled LLM providers found. Add one in the settings UI (python cli.py ui) "
                  "or configure LLM_* in .env.")
        return 2
    log.info("benchmarking %d provider(s): %s", len(providers),
             ", ".join(f"{p.name}/{p.model}" for p in providers))

    if args.task == "hooks":
        results = run_hooks(transcript, providers, args.segments, cfg)
    else:
        results = run_translate(transcript, args.target_lang, providers, args.segments, cfg)
    _print_benchmark(results)

    out_dir = cfg.get("paths.output_dir", "out")
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"benchmark_{args.task}_{_dt.date.today():%Y%m%d}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(render_markdown(results))
    print(f"\nWritten: {base}.json  and  {base}.md")
    return 0


def _print_hook_modes(modes: dict, transcript, cfg, top: int) -> None:
    """Show the CLIPS each mode would build (hook anchor → full clip), not raw
    ASR-segment fragments. Clips are grown to target duration, deduped, ranked."""
    from shortforge.select import build_clips
    cfg.override("select.num_clips", int(top))
    tgt = int(cfg.get("select.target_duration", 45))
    print("\n" + "=" * 78)
    print(f"  HOOK DETECTION — CLIPS built around the top hooks (~{tgt}s target)")
    for name, cands in modes.items():
        print("=" * 78)
        clips = build_clips(transcript, cands, cfg, "hooks")
        strong = sum(1 for c in cands if c.score >= 0.5)
        print(f"  MODE: {name}   ({strong} strong hook segments ≥0.50 → {len(clips)} clips)")
        print(f"    {'clip span':>15} {'dur':>5} {'score':>6}   justification")
        print("    " + "-" * 68)
        for c in clips:
            print(f"    {c.start:>6.1f}-{c.end:<6.1f}   {c.end - c.start:>4.0f}s {c.score:>6.2f}   "
                  f"{(c.reason or '')[:46]}")
            print(f"        “{(c.caption_text or '')[:88]}”")
    print("=" * 78)


def cmd_hooks(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")
    cfg = Config.load(args.config)
    _apply_common_overrides(cfg, args)
    src = args.source
    ext = os.path.splitext(src)[1].lower()
    source_path = None
    try:
        if ext in (".json", ".srt"):
            from shortforge.analyze import load_external_transcript
            transcript = load_external_transcript(src, 0.0, "auto")
        else:
            from shortforge.analyze import transcribe
            from shortforge.cache import Cache
            from shortforge.ingest import ingest
            if not args.owner_confirmed:
                log.error("--owner-confirmed is required when --source is a media file/URL.")
                return 2
            meta = ingest(src, cfg, owner_confirmed=True)
            cache = Cache(cfg.get("paths.work_dir", ".shortforge"), meta.hash)
            transcript = transcribe(meta, cfg, cache)
            source_path = meta.file_path
    except ShortForgeError as e:
        log.error("%s", e)
        return 2

    from shortforge.detect.provider_hooks import compare_modes
    from shortforge.providers.store import load_store
    try:
        modes = compare_modes(transcript, cfg, source_path, load_store())
    except ShortForgeError as e:
        log.error("%s", e)
        return 2
    _print_hook_modes(modes, transcript, cfg, int(getattr(args, "top", 8) or 8))
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    """Print stored credentials + task bindings (keys redacted) — the exact host,
    auth-header shape and chat URL each model will use, for diagnosis."""
    setup_logging(args.verbose)
    from shortforge.providers import store as S
    from shortforge.llm import normalize_chat_url
    store = S.load_store()
    print(f"\nStore: {S.store_path()}")
    print("=" * 78)
    creds = S.credentials(store)
    if not creds:
        print("  (no credentials — add one in the settings UI: python cli.py ui)")
    for c in creds:
        print(f"  [{c['id']}] {c['name']}   auth={c.get('auth_style', 'bearer')}"
              + (f" ({c.get('auth_header_name')})" if c.get('auth_style') == 'custom' else ""))
        print(f"     base_url : {c.get('base_url', '')!r}")
        print(f"     chat url : {normalize_chat_url(c.get('base_url', ''))}")
        print(f"     api_key  : {S.masked(c.get('api_key'))}")
        for m in c.get("models", []):
            print(f"       - {m.get('model')}  [{m.get('category')}, {m.get('tier', 'unknown')}, "
                  f"{'on' if m.get('enabled', True) else 'off'}]  id={m['id']}")
    if store.get("providers"):
        print(f"\n  Legacy single-model providers (unmigrated): {len(store['providers'])}")
    print("-" * 78)
    print("  Task bindings (non-default):")
    any_binding = False
    for meta in S.TASKS:
        b = S.get_task_binding(store, meta["key"])
        if b["primary"] or b["secondary"] or b["fallback"]:
            any_binding = True
            chain = " -> ".join(m.get("name", "?") for m in S.resolve_task(store, meta["key"])) or "(none)"
            print(f"    {meta['key']}: {chain}")
    if not any_binding:
        print("    (none set — tasks resolve by category priority)")
    print("=" * 78)
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    cfg = Config.load(args.config)
    from shortforge import maintenance as M
    from shortforge.cache import clear_cache
    work_dir = getattr(args, "work_dir", None) or cfg.get("paths.work_dir", ".shortforge")

    if args.cache_action == "report":
        # Cached sources are gigabytes and were never pruned by anything except
        # the post-failure emergency path, so on a modest disk the first sign of
        # trouble was a render dying on "no space left on device".
        r = M.cache_report(work_dir)
        print(f"Working directory: {os.path.abspath(work_dir)}")
        print(f"  cached downloads : {M.human_gb(r['downloads_bytes'])} "
              f"({r['downloads_files']} file(s))   <- reclaimable, re-fetchable")
        print(f"  hook scores      : {M.human_gb(r['hookscores_bytes'])} "
              f"({r['hookscores_files']} file(s))   <- paid LLM work, kept")
        print(f"  everything       : {M.human_gb(r['total_bytes'])}")
        print(f"  free on disk     : {M.human_gb(r['free_bytes'])}")
        print("\nprune old downloads:  python cli.py cache prune --dry-run")
        return 0

    if args.cache_action == "prune":
        n, freed = M.prune_downloads(work_dir, keep_hours=args.keep_hours,
                                     dry_run=args.dry_run)
        if not n:
            print(f"Nothing older than {args.keep_hours:g}h to remove.")
        elif args.dry_run:
            print(f"Would remove {n} download(s) older than {args.keep_hours:g}h, "
                  f"freeing {M.human_gb(freed)}. Re-run without --dry-run to do it.")
        else:
            print(f"Removed {n} download(s) older than {args.keep_hours:g}h — "
                  f"freed {M.human_gb(freed)}.")
        return 0

    if args.cache_action == "clear":
        if args.translation:
            what = "translation"
        elif args.transcript:
            what = "transcript"
        else:
            what = "all"
        n = clear_cache(work_dir, what)
        print(f"cleared {n} cached item(s) [{what}] from {work_dir}")
        print("(the job queue, worker state and chat history are never touched)")
        return 0
    log.error("unknown cache action")
    return 2


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or (default or "")


def cmd_wizard(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    load_env_file(getattr(args, "env_file", None) or ".env")
    cfg = Config.load(args.config)
    print("ShortForge wizard — answer the prompts (Enter accepts the default).\n")

    # Section 7 operator input wizard order.
    source = _ask("1. Source video URL or local path")
    if not source:
        log.error("A source is required.")
        return 2
    owner = _ask("   Confirm this is YOUR content (yes/no)", "yes").lower().startswith("y")
    if not owner:
        log.error("Ownership is required — ShortForge only processes your own content.")
        return 2

    aspect = _ask("2. Aspect / shape (9:16 / 1:1 / 16:9 / WxH)", "9:16")
    resolution = _ask("   Resolution / pixel size (1080p / 720p / 480p / WxH)", "1080p")
    duration = _ask("3. Target clip duration seconds (30/45/60)", "45")
    from shortforge.reframe import estimate_export
    try:
        est = estimate_export(resolution, aspect, float(_to_int(duration, 45)))
        print(f"   → {est['width']}x{est['height']}, ~{est['mb_per_clip']} MB/clip, "
              f"render {est['render_speed']}")
    except Exception:  # noqa: BLE001
        pass
    num = _ask("4. Number of clips (0 = let the tool recommend)", "0")
    language = _ask("5. Output language (en/de/it/es/ja/ar, blank = keep source)", "")
    print("   Dub modes:  captions = translate the TEXT only (keep original audio);")
    print("               voice    = replace speech with a synthetic voice;")
    print("               clone    = replace speech with a cloned voice (xtts).")
    dub_mode = _ask("6. Dub mode (captions / voice / clone)", "captions")
    tts = _ask("   TTS backend (auto / espeak / edge / xtts)", "auto")

    # C5: the exact confusion that shipped English captions over French audio.
    if language and dub_mode.lower().startswith("caption"):
        keep = _ask(
            f"   You chose {language} output with dub mode 'captions' — the "
            f"ORIGINAL audio is kept and only the captions are translated (no "
            f"voiceover). Continue? (yes/no)", "yes")
        if not keep.lower().startswith("y"):
            dub_mode = _ask("   Dub mode (voice / clone)", "voice")
    transcript = _ask("7. Transcript (auto / path to .srt|.json)", "auto")
    caps = _ask("8. Burn captions? (yes/no)", "yes").lower().startswith("y")
    template = _ask("   Caption template (" + " / ".join(list_templates()) + ")", "clean")
    animation = _ask("   Caption animation (" + " / ".join(ANIMATIONS) + ", blank = template)", "")
    caption_font = _ask("   Caption font / text form (blank = template)", "")
    fill = _ask("   Reframe fill (crop / blur)", "crop")
    jumpcuts = _ask("9. Trim dead air with jump cuts? (yes/no)", "no").lower().startswith("y")
    output = _ask("12. Output directory", cfg.get("paths.output_dir", "out"))

    cfg.override("reframe.aspect", aspect)
    cfg.override("reframe.resolution", resolution or "1080p")
    cfg.override("reframe.fill", fill)
    cfg.override("select.target_duration", _to_int(duration, 45))
    cfg.override("select.num_clips", _to_int(num, 0))
    cfg.override("captions.enabled", caps)
    cfg.override("captions.template", template or None)
    cfg.override("captions.animation", animation or None)
    cfg.override("captions.font", caption_font or None)
    cfg.override("edit.jumpcuts", jumpcuts)
    cfg.override("paths.output_dir", output)
    cfg.override("localize.language", language or None)
    if language and not dub_mode.lower().startswith("caption"):
        cfg.override("localize.dub", True)
        cfg.override("localize.tts_backend", "xtts" if dub_mode.lower().startswith("clone") else tts)
        lip = _ask("   Lip-sync the dub to the speaker's mouth? (yes/no) — needs Wav2Lip + GPU",
                   "no").lower().startswith("y")
        if lip:
            cfg.override("lipsync.enabled", True)
            cfg.override("lipsync.wav2lip_repo", _ask("     Path to Wav2Lip checkout", "") or None)
            cfg.override("lipsync.checkpoint", _ask("     Path to wav2lip_gan.pth", "") or None)

    transcript_path = None if transcript.lower() in ("", "auto") else transcript

    # C5: echo the full plan and require confirmation before processing.
    dubbing = bool(language and not dub_mode.lower().startswith("caption"))
    print("\n" + "-" * 56)
    print("  Plan:")
    print(f"    source     : {source}")
    print(f"    output     : {aspect}, ~{duration}s, {num} clip(s), fill={fill}")
    print(f"    language   : {language or '(keep source)'}")
    print(f"    audio      : {'DUB — ' + dub_mode if dubbing else 'original audio (captions only)'}")
    print(f"    captions   : {'on (' + (template or 'clean') + ')' if caps else 'off'}"
          + (f", translated to {language}" if language else ""))
    print(f"    jump cuts  : {'yes' if jumpcuts else 'no'}")
    print(f"    out dir    : {output}")
    print("-" * 56)
    if not _ask("  Proceed? (yes/no)", "yes").lower().startswith("y"):
        log.error("Cancelled.")
        return 1

    try:
        manifest = run_pipeline(
            source, cfg, owner_confirmed=True, transcript_path=transcript_path,
            confirm_cost=_cost_confirm(cfg),
        )
    except ShortForgeError as e:
        log.error("%s", e)
        return 2
    _print_summary(manifest)
    return 0


def _to_int(text: str, default: int) -> int:
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return default


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="shortforge",
        description="Turn your own long-form videos into short vertical clips (Phase 1).",
    )
    p.add_argument("--config", help="Path to settings.yaml (defaults to config/settings.yaml)")
    p.add_argument("--env-file", help="Path to a .env with API keys (defaults to ./.env)")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="Run the pipeline non-interactively")
    r.add_argument("source", help="Your video: a URL or a local file path")
    _add_run_options(r)
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("run-batch",
                       help="Run the pipeline over several of your own videos (sequential)")
    b.add_argument("sources", nargs="*",
                   help="Your videos: URLs or local paths (or use --from-file)")
    b.add_argument("--from-file",
                   help="Text file with one source per line (# comments allowed)")
    _add_run_options(b)
    b.set_defaults(func=cmd_batch)

    w = sub.add_parser("wizard", help="Interactive operator wizard")
    w.set_defaults(func=cmd_wizard)

    nsub = sub.add_parser("niches",
                          help="Suggest growing short-form niches (where to grow)")
    nsub.add_argument("--category",
                      help="Filter by category (e.g. " + ", ".join(niche_categories()) + ")")
    nsub.add_argument("--top", type=int, default=8, help="How many niches to show")
    nsub.add_argument("--sort", choices=["opportunity", "growth", "monetization"],
                      default="opportunity", help="Ranking lever (default: opportunity)")
    nsub.add_argument("--low-competition", action="store_true",
                      help="Only show low-competition niches")
    nsub.add_argument("--interests",
                      help="Your interests/channel for a personalized pick (needs Claude)")
    nsub.set_defaults(func=cmd_niches)

    csub = sub.add_parser("cache", help="Inspect / clear / prune ShortForge caches")
    csub.add_argument("cache_action", choices=["report", "clear", "prune"],
                      help="report = show what the work dir is holding; "
                           "clear = drop caches (queue and state are never touched); "
                           "prune = remove only downloads older than --keep-hours")
    csub.add_argument("--translation", action="store_true",
                      help="Clear only cached translations")
    csub.add_argument("--transcript", action="store_true",
                      help="Clear only cached transcripts")
    csub.add_argument("--all", action="store_true", help="Clear the whole cache (default)")
    csub.add_argument("--keep-hours", type=float, default=48.0, dest="keep_hours",
                      help="prune: keep downloads newer than this (default 48)")
    csub.add_argument("--dry-run", action="store_true",
                      help="prune: show what would go without removing it")
    csub.add_argument("--work-dir", help="Cache directory (default from config)")
    csub.set_defaults(func=cmd_cache)

    dsub = sub.add_parser("doctor", help="Check the environment / dependencies (C1)")
    dsub.set_defaults(func=cmd_doctor)

    lsub = sub.add_parser("check-llm", help="Test the configured LLM provider (E)")
    lsub.set_defaults(func=cmd_check_llm)

    psub = sub.add_parser("check-providers",
                          help="Test all configured LLM/TTS/audio providers + capabilities (L)")
    psub.set_defaults(func=cmd_check_providers)

    prov = sub.add_parser("providers",
                          help="Print stored credentials + task bindings (keys redacted) — "
                               "host, auth shape, chat URL per model, for diagnosis")
    prov.set_defaults(func=cmd_providers)

    usub = sub.add_parser("ui", help="Launch the web dashboard (job + settings screens)")
    usub.set_defaults(func=cmd_ui)

    qp = sub.add_parser("queue", help="Persistent link queue: add links, then run them one by one")
    qsub = qp.add_subparsers(dest="queue_action", required=True)
    qadd = qsub.add_parser("add", help="Add link(s) with their own settings")
    qadd.add_argument("urls", nargs="*", help="One or more video URLs")
    qadd.add_argument("--from-file", help="Read URLs from a text file (one per line)")
    qadd.add_argument("--label", default="", help="Short name used to tag this link's outputs")
    qadd.add_argument("--num-clips", type=int, dest="num_clips", help="Clips from THIS link")
    qadd.add_argument("--duration", type=int, dest="duration", help="Clip seconds for THIS link")
    qadd.add_argument("--tolerance", type=int, dest="tolerance")
    qadd.add_argument("--aspect", dest="aspect", help="9:16 | 1:1 | 16:9 | WxH")
    qadd.add_argument("--resolution", dest="resolution", help="1080p | 720p | 480p | WxH")
    qadd.add_argument("--language", dest="language")
    qadd.add_argument("--caption-template", dest="caption_template")
    qsub.add_parser("list", help="Show the queue and each job's status")
    qrun = qsub.add_parser("run", help="Work through pending links (resumes after a crash)")
    qrun.add_argument("--owner-confirmed", action="store_true",
                      help="Confirm every queued link is your own / licensed content")
    qclear = qsub.add_parser("clear", help="Remove jobs from the queue")
    qclear.add_argument("--done-only", action="store_true",
                        help="Keep pending/running jobs, drop finished ones")
    qp.set_defaults(func=cmd_queue)

    ls = sub.add_parser("listen",
                        help="Open the UI and listen for ntfy commands (owner-only), "
                             "working the queue with permission asked before each start")
    ls.add_argument("--owner-confirmed", action="store_true",
                    help="Confirm every link you send is your own / licensed content")
    ls.set_defaults(func=cmd_listen)

    nd = sub.add_parser("netdiag",
                        help="Diagnose why ntfy won't connect (DNS/TCP/TLS/proxy)")
    nd.add_argument("--host", help="Host to test (default: your configured ntfy server)")
    nd.add_argument("--timeout", type=int, default=10)
    nd.add_argument("--proxy", help="Test through this proxy, e.g. http://127.0.0.1:8080")
    nd.set_defaults(func=cmd_netdiag)

    es = sub.add_parser("encode-sample",
                        help="Render one slice with x264 vs hardware encoders to compare quality")
    es.add_argument("source", help="Your video: a URL or a local file path")
    es.add_argument("--owner-confirmed", action="store_true",
                    help="Confirm the source is your own / licensed content (required for URLs)")
    es.add_argument("--start", type=float, default=30.0, help="Slice start seconds (default 30)")
    es.add_argument("--duration", type=float, default=8.0, help="Slice length seconds (default 8)")
    es.add_argument("--resolution", help="Export size (default from config: 1080p)")
    es.set_defaults(func=cmd_encode_sample)

    hk = sub.add_parser("hooks",
                        help="Compare hook-detection modes on a source (C1): heuristic vs "
                             "transcript-LLM vs fused — prints candidates with scores/reasons")
    hk.add_argument("--source", required=True,
                    help="A media file/URL or a cached transcript (.json/.srt)")
    hk.add_argument("--owner-confirmed", action="store_true",
                    help="Required when --source is a media file/URL")
    hk.add_argument("--top", type=int, default=8, help="How many candidates to show per mode")
    hk.add_argument("--work-dir", help="Cache/intermediate directory")
    hk.set_defaults(func=cmd_hooks)

    bl = sub.add_parser("benchmark-llm",
                        help="Compare providers: LLMs on translate/hooks (STEP 2) "
                             "or ASR backends on transcribe (STEP 3.5b)")
    bl.add_argument("--source", required=True,
                    help="A cached transcript (.json/.srt) or your own media file/URL")
    bl.add_argument("--owner-confirmed", action="store_true",
                    help="Required when --source is media (reuses the cached transcript)")
    bl.add_argument("--target-lang", default="en", help="Target language for translate (default en)")
    bl.add_argument("--segments", type=int, default=5, help="How many segments to compare")
    bl.add_argument("--task", choices=["translate", "hooks", "transcribe"], default="translate",
                    help="translate/hooks compare LLMs; transcribe compares ASR backends (STEP 3.5b)")
    bl.add_argument("--models",
                    help="Comma-separated model ids to benchmark on the first configured "
                         "provider's endpoint + key (test candidates without adding each as a "
                         "provider). LLM tasks only.")
    bl.add_argument("--output", help="Output directory for the results files")
    bl.add_argument("--whisper-model", help="tiny|base|small|... (only if it must transcribe)")
    bl.add_argument("--work-dir", help="Cache/intermediate directory")
    bl.set_defaults(func=cmd_benchmark_llm)
    return p


def _add_run_options(r: argparse.ArgumentParser) -> None:
    """Flags shared by `run` and `run-batch` (everything except the source(s))."""
    r.add_argument("--owner-confirmed", action="store_true",
                   help="Confirm the source is your own / licensed content (required)")
    r.add_argument("--duration", type=int, help="Target clip duration in seconds")
    r.add_argument("--tolerance", type=int, help="Allowed +/- seconds from target")
    r.add_argument("--num-clips", type=int, help="Number of clips (0 = auto-recommend)")
    r.add_argument("--aspect", help="9:16 / 1:1 / 16:9 / WxH (e.g. 1080x1920)")
    r.add_argument("--fill", choices=["crop", "blur"], help="Reframe fill mode")
    r.add_argument("--reframe-mode", choices=["track", "center"],
                   help="track = virtual camera follows the action (A4); center = static crop")
    r.add_argument("--debug-reframe", action="store_true",
                   help="Also write a source-aspect diagnostic video of the crop path (A4.7)")
    r.add_argument("--caption-template", choices=list_templates(),
                   help="Named caption look: " + " | ".join(list_templates()))
    r.add_argument("--caption-animation", choices=ANIMATIONS,
                   help="Caption animation (overrides template default)")
    r.add_argument("--caption-font", help="Override the caption font (text form)")
    r.add_argument("--caption-size", type=int, help="Override caption font size (px)")
    r.add_argument("--highlight-color", help="Override sung-word colour (ASS &HAABBGGRR)")
    r.add_argument("--uppercase", action="store_true", help="Force UPPERCASE captions")
    r.add_argument("--caption-style", choices=["karaoke", "simple"],
                   help="legacy shorthand (maps to --caption-animation)")
    r.add_argument("--burned-in", choices=["none", "cover", "blur", "crop"],
                   help="Treat captions baked into the source pixels (A3)")
    r.add_argument("--resolution", help="Export size (short side): 1080p | 720p | 480p | WxH. "
                   "Separate from --aspect. Default 1080p; lower renders faster on CPU.")
    r.add_argument("--encoder", choices=["auto", "qsv", "nvenc", "amf", "x264"],
                   help="Video encoder. x264 (default, software) | qsv (Intel Quick Sync, much "
                        "faster) | nvenc | amf | auto (best hardware available). Compare quality "
                        "first with `encode-sample`.")
    r.add_argument("--logo", help="Path to a logo image to overlay (M8)")
    r.add_argument("--logo-corner", choices=["TL", "TR", "BL", "BR"], help="Logo corner")
    r.add_argument("--logo-size", help="Logo width: fraction of output width (0..1) or px (>1)")
    r.add_argument("--logo-opacity", type=float, help="Logo opacity 0..1")
    r.add_argument("--no-dub", action="store_true",
                   help="Force original-audio-only (no TTS/translation voice) — the default")
    r.add_argument("--niche", help="Niche keyword(s) for metadata/hashtags (M10)")
    # Localization (Phase 3, M6)
    r.add_argument("--language", help="Target language code for captions/dub "
                   "(en/de/it/es/ja/ar/...); omit to keep the source language")
    r.add_argument("--dub", action="store_true",
                   help="Synthesize a voiceover in --language (else captions-only)")
    r.add_argument("--dub-mode", choices=["captions", "voice", "clone"],
                   help="captions = translated text only; voice = synthetic voice; "
                        "clone = cloned voice (needs xtts)")
    r.add_argument("--tts", choices=["auto", "espeak", "edge", "xtts"],
                   help="TTS backend: espeak (offline) / edge (natural) / xtts (clone, GPU)")
    r.add_argument("--stem-separation", choices=["auto", "true", "false"],
                   help="Split vocals from music/SFX before dubbing (A1)")
    r.add_argument("--stem-model", help="Demucs model (htdemucs, htdemucs_ft, mdx_extra_q)")
    r.add_argument("--vocal-removal-strength",
                   help="partial (keep vocals-stem ambience low) | full | a dB value (G1)")
    r.add_argument("--debug-audio", action="store_true",
                   help="Export separated stems + the reconstructed bed as WAVs (G1)")
    r.add_argument("--allow-voice-bleed", action="store_true",
                   help="Dub without stems by ducking the original (accepts two voices)")
    r.add_argument("--allow-untranslated", action="store_true",
                   help="Continue if translation fails (keeps source text; loud warning)")
    r.add_argument("--no-cache", action="store_true",
                   help="Never read or write caches for this run")
    r.add_argument("--dry-run-cost", action="store_true",
                   help="Estimate spend + show the clip plan, render nothing (STEP 1)")
    r.add_argument("--cost-ceiling", type=float,
                   help="Abort if the estimated dub cost exceeds this many USD")
    r.add_argument("-y", "--yes", action="store_true",
                   help="Skip the cost-confirmation prompt")
    r.add_argument("--refresh-translation", action="store_true",
                   help="Recompute the translation only (keep transcript + visual analysis)")
    r.add_argument("--voice-sample", help="Your-voice reference clip for --tts xtts")
    r.add_argument("--translate-backend", choices=["auto", "llm", "argos"],
                   help="Translation backend (Claude / offline Argos)")
    r.add_argument("--lipsync", action="store_true",
                   help="Lip-sync dubbed clips to the new voice (Wav2Lip; GPU, cross-language)")
    r.add_argument("--wav2lip-repo", help="Path to a cloned Rudrabha/Wav2Lip checkout")
    r.add_argument("--wav2lip-checkpoint", help="Path to wav2lip_gan.pth")
    r.add_argument("--no-review", action="store_true", help="Skip the QC review gate")
    r.add_argument("--no-captions", action="store_true", help="Do not burn captions")
    r.add_argument("--no-loudnorm", action="store_true", help="Skip -14 LUFS normalisation")
    r.add_argument("--jumpcuts", action="store_true",
                   help="Trim long silences / dead air (jump cuts); off when dubbing")
    r.add_argument("--metadata", action="store_true",
                   help="Generate title/description/tags per clip (opt-in; costs an LLM call each)")
    r.add_argument("--thumbnail", action="store_true",
                   help="Generate a cover-frame thumbnail per clip (opt-in)")
    r.add_argument("--no-metadata", action="store_true", help="Force metadata off (default)")
    r.add_argument("--no-thumbnail", action="store_true", help="Force thumbnail off (default)")
    r.add_argument("--no-sidecar", action="store_true",
                   help="Skip the per-video title/description/tags .txt file")
    r.add_argument("--resume", action="store_true",
                   help="Skip clips whose output file already exists")
    r.add_argument("--whisper-model", help="tiny|base|small|medium|large-v3 (default small)")
    r.add_argument("--source-lang",
                   help="Force the source language (ISO code, e.g. fr) instead of autodetect")
    r.add_argument("--min-confidence", type=float,
                   help="Drop transcript segments below this ASR confidence (0..1)")
    r.add_argument("--cookies", help="Path to cookies.txt for private/unlisted "
                   "videos on your own channel (M1 auth)")
    r.add_argument("--cookies-from-browser", choices=["firefox", "chrome", "edge", "brave"],
                   help="Read cookies from this browser for yt-dlp auth (fixes YouTube's "
                        "bot wall). Firefox is most reliable on Windows.")
    r.add_argument("--transcript", help="Use a supplied .srt/.json instead of ASR")
    r.add_argument("--vision", action="store_true",
                   help="Use Claude-vision multimodal hook scoring (claude-watch; needs key)")
    r.add_argument("--hook-frames", action="store_true",
                   help="Fuse free llama-3.2 vision frame scores onto the top hook candidates "
                        "(off by default — 1 image/call, so it's a slow bonus, not needed)")
    r.add_argument("--no-visual", action="store_true",
                   help="Disable local visual hook signals (motion/cuts/faces)")
    r.add_argument("--use-llm", dest="backend", action="store_const", const="llm",
                   help="Force Claude hook detection (needs ANTHROPIC_API_KEY)")
    r.add_argument("--no-llm", dest="backend", action="store_const", const="heuristic",
                   help="Force the keyword heuristic")
    r.add_argument("--work-dir", help="Cache/intermediate directory")
    r.add_argument("--output", help="Output directory for rendered clips")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
