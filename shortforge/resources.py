"""What this program is costing the machine, right now.

The operator runs ShortForge on modest hardware and has repeatedly had to ask
"is it stuck, or just slow?" — a question no message can settle, because it is
really "is anything still working". A live reading of what ShortForge itself is
using answers it directly: CPU moving means work is happening.

Deliberately cheap and deliberately optional. Every reading is best-effort and
falls back to "?" rather than raising: a monitoring panel that can break the
dashboard would be worse than no panel. `psutil` gives the good numbers; without
it the stdlib still covers core count and (on Windows) total/used memory, so the
panel degrades instead of disappearing.
"""

from __future__ import annotations

import os
import shutil
import time

_proc = None            # cached psutil.Process, so CPU% has a baseline to diff


def _psutil():
    try:
        import psutil
        return psutil
    except Exception:      # noqa: BLE001 - optional dependency
        return None


def _windows_memory() -> tuple[int, int] | None:
    """(total, available) bytes via GlobalMemoryStatusEx — no psutil needed."""
    try:
        import ctypes
        from ctypes import wintypes
        if not hasattr(ctypes, "windll"):
            return None

        class _MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _MemStatus()
        st.dwLength = ctypes.sizeof(_MemStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        return int(st.ullTotalPhys), int(st.ullAvailPhys)
    except Exception:      # noqa: BLE001
        return None


def gpu_name() -> str:
    """Installed GPU, if it can be identified without extra dependencies."""
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0].strip()
    except Exception:      # noqa: BLE001 - no NVIDIA GPU, or no driver tools
        pass
    if os.name == "nt":
        try:
            import subprocess
            out = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                capture_output=True, text=True, timeout=5)
            lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
            if len(lines) > 1:
                return lines[1]
        except Exception:  # noqa: BLE001
            pass
    return "not detected"


def snapshot(work_dir: str = ".shortforge") -> dict:
    """One cheap reading of machine + ShortForge usage.

    ``proc_*`` covers this process AND its children, because the work that
    matters — ffmpeg, and a spawned `queue run` — happens in child processes.
    Attributing only the parent would show a near-idle dashboard during a
    render, which is precisely the misleading answer to avoid.
    """
    global _proc
    ps = _psutil()
    out: dict = {"has_psutil": bool(ps), "cores": os.cpu_count() or 0,
                 "gpu": None, "speed": None}

    if ps is not None:
        try:
            if _proc is None:
                _proc = ps.Process(os.getpid())
                _proc.cpu_percent(None)          # prime the baseline
            vm = ps.virtual_memory()
            out["mem_total"] = vm.total
            out["mem_used"] = vm.total - vm.available
            out["mem_pct"] = vm.percent
            out["cpu_pct"] = ps.cpu_percent(interval=None)

            procs = [_proc] + _proc.children(recursive=True)
            rss = cpu = 0.0
            for p in procs:
                try:
                    rss += p.memory_info().rss
                    cpu += p.cpu_percent(None)
                except Exception:   # noqa: BLE001 - a child can exit mid-walk
                    continue
            out["proc_mem"] = int(rss)
            # Sum across cores; normalise so 100% means "one core saturated"
            # would read as 100/cores of the machine.
            out["proc_cpu_pct"] = round(cpu, 1)
            out["proc_count"] = len(procs)
        except Exception:  # noqa: BLE001
            pass
    else:
        mem = _windows_memory()
        if mem:
            total, avail = mem
            out["mem_total"], out["mem_used"] = total, total - avail
            out["mem_pct"] = round((total - avail) / total * 100, 1)

    try:
        out["disk_free"] = shutil.disk_usage(
            work_dir if os.path.isdir(work_dir) else ".").free
    except OSError:
        pass
    return out


def render_speed(work_dir: str = ".shortforge") -> str | None:
    """How fast the current job is going, in plain words.

    Derived from the per-job log the worker already writes, so it costs a file
    read and needs no new plumbing: how long the job has been running, and
    which stage it reached.
    """
    try:
        from . import lifecycle
        cur = (lifecycle.read_state(work_dir) or {}).get("current") or {}
        started = cur.get("started")
        if not started:
            return None
        mins = (time.time() - float(started)) / 60.0
        return f"{mins:.0f} min on this link" if mins >= 1 else "just started"
    except Exception:  # noqa: BLE001
        return None


def human_bytes(n) -> str:
    if not n:
        return "?"
    if n < 1024 ** 2:
        return f"{n / 1024:.0f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.0f} MB"
    return f"{n / 1024 ** 3:.1f} GB"
