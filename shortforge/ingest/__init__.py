"""M1 — Ingestion."""

from .ingest import (ingest, test_youtube_auth, update_ytdlp, ytdlp_version,
                     BOT_AUTH_MESSAGE)

__all__ = ["ingest", "test_youtube_auth", "update_ytdlp", "ytdlp_version",
           "BOT_AUTH_MESSAGE"]
