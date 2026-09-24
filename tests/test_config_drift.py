"""Config drift is this project's most-repeated bug — automate it away.

PROGRESS.md records it biting at least three separate times: a default changed
in `config.py` DEFAULTS but not in `config/settings.yaml` (the yaml silently
wins at runtime), or an inline `cfg.get("key", FALLBACK)` left holding the
value a tuning pass had just moved away from.

Those inline fallbacks are unreachable today — `Config.load()` always seeds
DEFAULTS, so the key is always present — but they are landmines that fire the
moment anything builds a Config another way, and every one found in the audit
held a *pre-tuning* value: `detect.visual=True` would re-enable the whole-source
CV pass that costs minutes per video, `metadata/thumbnail.enabled=True` would
turn paid per-clip LLM calls back on, `vision.max_images=6` is the exact value
the provider rejects with "At most 1 image(s) may be provided in one prompt",
and the `reframe.*` set is the shaky-camera tuning that was deliberately calmed.

So rather than re-check by hand every time, these tests fail the build on any
of the three drift shapes.
"""

import ast
import os
import re

import pytest
import yaml

from shortforge.config import DEFAULTS

_YAML = os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml")
_PKG = os.path.join(os.path.dirname(__file__), "..", "shortforge")


def _flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def _defaults():
    return _flatten(DEFAULTS)


def _yaml_values():
    with open(_YAML, encoding="utf-8") as f:
        return _flatten(yaml.safe_load(f) or {})


def _same(a, b):
    """1 and 1.0 are the same default; True and 1 are not."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b


def test_settings_yaml_never_contradicts_defaults():
    """The yaml is deep-merged OVER DEFAULTS, so any disagreement means the
    DEFAULTS entry is decorative and the real value is somewhere else — the
    exact confusion that made `burned_in_threshold` and the metadata flags
    hard to reason about."""
    d, y = _defaults(), _yaml_values()
    drift = {k: (d[k], y[k]) for k in y if k in d and not _same(d[k], y[k])}
    assert not drift, (
        "config/settings.yaml disagrees with config.py DEFAULTS "
        "(yaml wins at runtime, so DEFAULTS is lying):\n"
        + "\n".join(f"  {k}: DEFAULTS={dv!r} yaml={yv!r}" for k, (dv, yv) in drift.items()))


def test_settings_yaml_has_no_keys_defaults_does_not_know_about():
    """A yaml-only key works, but nothing documents or type-anchors it, and
    `doctor`/the Settings UI won't know it exists."""
    unknown = sorted(set(_yaml_values()) - set(_defaults()))
    assert not unknown, ("config/settings.yaml sets keys absent from DEFAULTS: "
                         + ", ".join(unknown))


def _iter_get_fallbacks():
    """Every `<something>.get("dotted.key", <literal>)` in the package."""
    for root, _dirs, files in os.walk(_PKG):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(root, fname)
            with open(path, encoding="utf-8") as f:
                source = f.read()
            try:
                tree = ast.parse(source)
            except SyntaxError:                                  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "get"
                        and len(node.args) == 2):
                    continue
                key_node, default_node = node.args
                if not (isinstance(key_node, ast.Constant)
                        and isinstance(key_node.value, str)):
                    continue
                try:
                    default = ast.literal_eval(default_node)
                except (ValueError, SyntaxError):
                    continue                                     # not a literal
                yield os.path.relpath(path, _PKG), node.lineno, key_node.value, default


def test_inline_config_fallbacks_match_defaults():
    """`cfg.get("detect.visual", True)` next to `"visual": False` in DEFAULTS is
    a landmine, not a safety net: it encodes a stale answer to the same
    question and only reveals itself if a Config ever reaches it without
    DEFAULTS seeded."""
    d = _defaults()
    bad = []
    for relpath, lineno, key, fallback in _iter_get_fallbacks():
        if key in d and not _same(d[key], fallback):
            bad.append(f"  shortforge/{relpath}:{lineno}  {key}: "
                       f"fallback={fallback!r} but DEFAULTS={d[key]!r}")
    assert not bad, ("inline cfg.get() fallbacks disagree with config.py DEFAULTS "
                     "(update the fallback, or drop it and let DEFAULTS answer):\n"
                     + "\n".join(sorted(bad)))


@pytest.mark.parametrize("key", [
    # The settings whose defaults were deliberately chosen for this operator's
    # CPU-only box; a silent revert costs minutes per video or real money.
    "detect.visual", "detect.backend", "detect.hook_frames",
    "metadata.enabled", "thumbnail.enabled", "reframe.mode",
    "transcribe.model", "vision.max_images",
])
def test_performance_and_cost_defaults_are_what_the_fast_path_expects(key):
    """A guard on the specific values behind "just make shorts, fast" and
    "default-mode paid calls = hook detection + metadata only"."""
    expected = {
        "detect.visual": False,        # whole-source CV pass: minutes per video
        "detect.backend": "heuristic",  # no LLM call on the default path
        "detect.hook_frames": False,   # per-frame vision = slow + paid
        "metadata.enabled": False,     # paid LLM call per clip
        "thumbnail.enabled": False,    # paid LLM call per clip
        "reframe.mode": "center",      # no face detection / saliency pass
        "transcribe.model": "small",   # 'base' garbles; 'small' is the floor
        "vision.max_images": 1,        # provider hard-rejects more than 1
    }[key]
    assert _same(_defaults()[key], expected)
