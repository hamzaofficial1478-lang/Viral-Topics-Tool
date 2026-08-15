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

## Install on a new PC

Three steps — no command line needed after cloning:

1. **Clone, then double-click `setup.bat`.** It runs unattended: checks Python
   (tells you where to get it if missing), creates the `venv` and installs
   everything, installs FFmpeg via `winget` if absent, installs the Visual C++
   runtime if absent, pre-downloads the Whisper `small` model, and finishes with
   `python cli.py doctor` printing a clear **PASS/FAIL**. The window stays open so
   you can read the result. Paths with spaces (e.g. `ammar laptops`) are fine.
   *If it says FFmpeg is missing right after installing it, close the window and
   run `setup.bat` again so the new PATH is picked up.*
2. **Double-click `start_ui.bat`** (or **`start_ui.vbs`** for no console window).
   It activates the venv, launches the dashboard, and opens your browser to it.
3. **Restore your settings.** In the dashboard open **Settings → 💾 Backup &
   restore** and **Import** the `shortforge-settings.json` you exported from your
   old PC. (Export it there with the **⬇️ Export settings** button first.) If you
   drop that file in the project folder before first launch, the Settings screen
   offers to import it automatically. That file contains your API keys in
   plaintext — keep it private; it's gitignored so it can't be committed.

**Keeping more than one PC in step:** double-click **`update.bat`** on each. It
pulls whichever branch that checkout is on, reinstalls anything new, updates
yt-dlp, and prints the **build id** the PC is now running. Compare that id
across your machines — if they differ, one is on older code, and a failure it
reports may already be fixed on the other. Local edits to tracked files are
stashed automatically (git otherwise refuses the pull outright); nothing is
discarded, and the script tells you how to restore them. The same id is shown
in the dashboard footer and at the top of `python cli.py doctor`.

**Start automatically at logon:** double-click **`install_autostart.bat`** and pick
Everything mode (UI + ntfy remote control) or Queue mode (option 3 turns it off
again). It needs no admin rights and works out its own paths — it drops a small
launcher into your Startup folder. Don't use `schtasks` for this: many machines
refuse it with "Access is denied".

**Desktop shortcut / taskbar:** right-click `start_ui.vbs` → **Send to → Desktop
(create shortcut)** and rename it "ShortForge". To pin it to the taskbar, make a
shortcut whose **Target** is `wscript.exe "C:\path\to\start_ui.vbs"` (Windows
only pins shortcuts to real programs like `wscript.exe`), then right-click it →
**Pin to taskbar**. Set a custom icon via the shortcut's **Properties → Change
Icon**.
