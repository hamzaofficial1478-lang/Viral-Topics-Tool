"""Tabs whose content is built only while they are open.

By default Streamlit runs the code of EVERY tab on every click, whichever one
is on screen, so a page with a heavy tab is slow everywhere on it. Since 1.55,
``st.tabs(..., on_change="rerun")`` reports which tab is open (``.open``) and
reruns when another is picked, so only that one needs building.
"""

from __future__ import annotations

import streamlit as st

from ..utils import log

_warned = False


class _AlwaysOpen:
    """Wraps a plain tab on an older Streamlit: everything renders, as before."""

    def __init__(self, tab):
        self._tab = tab
        self.open = True

    def __enter__(self):
        return self._tab.__enter__()

    def __exit__(self, *exc):
        return self._tab.__exit__(*exc)


def lazy_tabs(labels: list[str], *, key: str) -> list:
    """``st.tabs`` where only the selected tab's ``.open`` is True.

    The caller wraps each tab's content in ``if tab.open:``. On a Streamlit
    older than 1.55 every tab reports open (the old, slower behaviour) and
    that is logged once — update.bat installs the newer version.
    """
    global _warned
    try:
        return list(st.tabs(labels, key=key, on_change="rerun"))
    except TypeError:
        if not _warned:
            _warned = True
            log.warning("this Streamlit is older than 1.55: every tab is built on every "
                        "click, which is slower — run update.bat to update it")
        return [_AlwaysOpen(t) for t in st.tabs(labels)]
