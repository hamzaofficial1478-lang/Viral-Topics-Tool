"""ShortForge — turn your own long-form videos into short vertical clips.

Phase 1 (this package): the core clipper.
    ingest -> transcribe -> detect hooks -> select clips -> reframe 9:16
    -> burn captions -> render -> manifest.

The one hard rule: source content must be the operator's own (or otherwise
licensed). Ingestion refuses to proceed without an explicit ownership
confirmation.
"""

__version__ = "0.1.0"
