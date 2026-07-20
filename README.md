# ShortForge

Turns the operator's **own** long-form videos into multilingual short-form
clips (Reels/Shorts): ingest → transcribe → hook-detect → select → reframe →
translate/dub → captions → render → QC → publish sidecars. Python + FFmpeg,
runs locally on CPU.

> **Status & plan:** see **[`PROGRESS.md`](PROGRESS.md)** — the living status
> document (what's done, what's next, what's decided).
> **Operating rules & environment:** see **[`CLAUDE.md`](CLAUDE.md)**.
> Read `PROGRESS.md` first at the start of any session.

## Quickstart (Windows)
```
cd $HOME\Desktop\viral-topics-tool
venv\Scripts\activate
git pull origin claude/nifty-cray-n8l888
pip install -r requirements.txt
python cli.py doctor        # check environment / dependencies
python cli.py ui            # settings + new-job dashboard
python cli.py wizard        # guided CLI run
```

API keys are configured through the settings UI (`python cli.py ui`) and stored
in gitignored `config/providers.local.json` — never in chat, never committed.
