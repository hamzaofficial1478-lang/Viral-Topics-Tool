"""Network diagnosis for outbound HTTPS (why ntfy won't connect).

"urlopen error timed out" is the least useful message in Python: it covers DNS
failure, a blocked TCP port, a stalled TLS handshake and a dropped HTTP request.
This walks those four layers separately so the answer is specific — and it
detects the case that VPNs never fix: **Windows has a system proxy configured,
the browser uses it, and Python does not.**
"""

from __future__ import annotations

import socket
import ssl
import urllib.parse
import urllib.request

PORT = 443
DEFAULT_HOST = "ntfy.sh"


def _configured_host() -> str:
    """The operator's own ntfy server if they set one (self-hosted), else ntfy.sh."""
    try:
        from .providers.store import load_store
        server = (load_store().get("ntfy", {}) or {}).get("server") or ""
        host = urllib.parse.urlparse(server).hostname
        return host or DEFAULT_HOST
    except Exception:  # noqa: BLE001
        return DEFAULT_HOST


def windows_proxy() -> str | None:
    """The proxy Windows itself is configured to use, if any.

    Browsers read this automatically; Python only honours HTTP(S)_PROXY env
    vars, so a machine behind a corporate/AV proxy works everywhere *except*
    Python — which looks exactly like "the internet is broken"."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if not enabled:
            return None
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
        return str(server) or None
    except Exception:  # noqa: BLE001 - not Windows, or no proxy configured
        return None


def env_proxy() -> str | None:
    import os
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")


def build_opener(proxy: str | None = None):
    """A urllib opener that goes through ``proxy`` (or Windows' proxy)."""
    proxy = proxy or env_proxy() or windows_proxy()
    if not proxy:
        return urllib.request.build_opener()
    if "://" not in proxy:
        proxy = "http://" + proxy
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


def check(host: str | None = None, timeout: int = 10,
          proxy: str | None = None) -> list[tuple[str, bool, str]]:
    """Walk DNS → TCP → TLS → HTTP against ``host`` (default: the configured ntfy
    server, else ntfy.sh). Returns [(layer, ok, detail)]."""
    host = host or _configured_host()
    out: list[tuple[str, bool, str]] = []

    # 1. DNS
    try:
        infos = socket.getaddrinfo(host, PORT, proto=socket.IPPROTO_TCP)
        ips = sorted({i[4][0] for i in infos})
        out.append(("DNS lookup", True, ", ".join(ips[:3])))
    except Exception as e:  # noqa: BLE001
        out.append(("DNS lookup", False, f"{type(e).__name__}: {e}"))
        return out                      # nothing else can work

    # 2. TCP
    try:
        with socket.create_connection((host, PORT), timeout=timeout):
            pass
        out.append(("TCP connect :443", True, "open"))
    except Exception as e:  # noqa: BLE001
        out.append(("TCP connect :443", False, f"{type(e).__name__}: {e}"))

    # 3. TLS
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, PORT), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                out.append(("TLS handshake", True, tls.version() or "ok"))
    except Exception as e:  # noqa: BLE001
        out.append(("TLS handshake", False, f"{type(e).__name__}: {e}"))

    # 4. HTTP (this is what ntfy actually does)
    try:
        opener = build_opener(proxy)
        with opener.open(f"https://{host}", timeout=timeout) as r:
            out.append(("HTTPS request", True, f"HTTP {r.status}"))
    except Exception as e:  # noqa: BLE001
        out.append(("HTTPS request", False, f"{type(e).__name__}: {e}"))
    return out


def advise(results: list[tuple[str, bool, str]], host: str | None = None,
           proxy: str | None = None) -> str:
    """Turn the layer results into the one thing to try next."""
    host = host or _configured_host()
    ok = {name: good for name, good, _ in results}
    win_proxy = windows_proxy()
    env_p = env_proxy()

    if not ok.get("DNS lookup", False):
        return (f"DNS can't resolve {host}. Your ISP's DNS is blocking it.\n"
                "FIX: switch this PC's DNS to Cloudflare (1.1.1.1) or Google (8.8.8.8) — "
                "Settings → Network → Change adapter options → IPv4 properties.")
    if not ok.get("TCP connect :443", False):
        if win_proxy and not env_p:
            return (f"Windows is set to use a proxy ({win_proxy}) but Python isn't using it — "
                    "that's why your browser works and ShortForge doesn't. VPNs don't fix this.\n"
                    f'FIX: set the proxy for Python, then restart ShortForge:\n'
                    f'    setx HTTPS_PROXY "http://{win_proxy}"\n'
                    f'    setx HTTP_PROXY  "http://{win_proxy}"')
        timed_out = any("timed out" in d.lower() or "timeout" in d.lower()
                        for n, ok_, d in results if n == "TCP connect :443")
        if timed_out:
            return (
                f"DNS resolves but the connection to {host}:443 TIMES OUT (it is not refused).\n"
                "A local firewall/antivirus REFUSES instantly; a silent timeout means your "
                f"packets are being dropped upstream — i.e. your ISP/country blocks {host} "
                "by IP. Retrying will never help.\n\n"
                "FIX, in order of what actually works:\n"
                "1. Use a SYSTEM-WIDE VPN, not a browser extension. Browser add-on VPNs only "
                "tunnel the browser, so Python stays blocked — that is why your browser works "
                "and this doesn't. Free system-wide options: Cloudflare WARP (the 1.1.1.1 app), "
                "Proton VPN, Windscribe. Install, connect, then re-run this check.\n"
                "2. If you have a working proxy, point Python at it:\n"
                '   setx HTTPS_PROXY "http://host:port"   (then restart ShortForge)\n'
                "3. Or self-host ntfy / use a different ntfy server (Settings → Notifications) "
                f"if only {host} specifically is blocked.")
        return (f"Port 443 to {host} is refused. That is a local block: antivirus or firewall "
                "is stopping python.exe specifically (your browser is allowed, Python isn't).\n"
                "FIX: add an outbound rule for python.exe, or turn off the AV web/HTTPS shield "
                "and test again.")
    if not ok.get("TLS handshake", False):
        return ("TCP connects but the secure handshake fails — something is intercepting "
                "HTTPS (antivirus 'SSL/HTTPS scanning' or a corporate proxy).\n"
                "FIX: turn off HTTPS/SSL scanning in your antivirus for python.exe, or "
                "install its root certificate into Python's trust store.")
    if not ok.get("HTTPS request", False):
        return ("The connection works but the request still fails — most likely a proxy that "
                "needs credentials.\n"
                'FIX: set HTTPS_PROXY with your user/password:\n'
                '    setx HTTPS_PROXY "http://user:pass@host:port"')
    return (f"All four layers pass — {host} is reachable from this PC. If the ntfy Test "
            "button still fails, double-check the topic name was saved correctly.")


def report(host: str | None = None, timeout: int = 10, proxy: str | None = None) -> str:
    host = host or _configured_host()
    results = check(host, timeout=timeout, proxy=proxy)
    lines = [f"Connectivity check — {host}", "=" * 44]
    for name, good, detail in results:
        lines.append(f" [{'✓' if good else '✗'}] {name:18s} {detail}")
    wp, ep = windows_proxy(), env_proxy()
    lines.append("")
    lines.append(f" Windows system proxy : {wp or '(none)'}")
    lines.append(f" Python HTTPS_PROXY   : {ep or '(none)'}")
    lines.append("")
    lines.append(advise(results, host, proxy))
    return "\n".join(lines)
