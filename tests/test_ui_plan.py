"""STEP 3.6: dashboard 'what happens next' plan helpers (pure, no Streamlit render)."""

import app


def test_plan_steps_local_no_language():
    steps = app._plan_steps(45, 12, 0, "9:16", "Track the action (virtual camera)",
                            "small", "", "captions")
    joined = " ".join(steps)
    assert "Whisper **small**" in joined
    assert "auto-recommended" in joined                       # 0 clips -> recommend
    assert "9:16" in joined and "tracking virtual camera" in joined
    assert "Keep" in joined and "original language" in joined
    assert "title, description and tags" in joined


def test_plan_steps_dub_language_and_custom_size():
    steps = app._plan_steps(120, 8, 3, "1080x1920", "Center crop", "medium", "en", "voice")
    joined = " ".join(steps)
    assert "**3**" in joined and "120s" in joined              # explicit count + no 90s cap
    assert "1080x1920" in joined and "center crop" in joined
    assert "Translate and dub" in joined and "**en**" in joined and "synthetic voice" in joined


def test_plan_notes_flags():
    assert any("no API cost" in n for n in app._plan_notes("small", "", "captions"))
    assert any("LLM" in n for n in app._plan_notes("small", "en", "captions"))
    notes = app._plan_notes("large-v3", "en", "voice")
    assert any("ElevenLabs" in n for n in notes)               # paid dub
    assert any("Demucs" in n for n in notes)                   # stem-separation ETA
    assert any("slow" in n for n in notes)                     # large model on CPU
