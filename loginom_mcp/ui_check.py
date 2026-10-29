"""Loginom UI automation: open a package, read errors, close windows.

The MCP host cannot show modal Loginom dialogs to the agent, so a broken package
would leave a window (and a ``.lck`` file) hanging on the user's desktop. This
module opens the package, reads Loginom's own log (and, as a fallback, the dialog
text via Win32 + UI Automation), dismisses the dialog and closes the window.

Everything here is Windows-only and silently degrades to a structured
"skipped" answer on other platforms or when Loginom is not installed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .paths import VENDOR, loginom_exe, ui_verify_available

CAPTURE_PS1 = VENDOR / "capture_loginom_ui.ps1"

# Phrases that mean Loginom refused to open/load a package.
ERROR_MARKERS = (
    "возникли ошибки",
    "ошибка загрузки",
    "не удалось загрузить",
    "не существует",
    "не найден",
    "элемент схемы",
    "aggregateexception",
    "ebgexception",
    "одинаковые идентификаторы",
    "list index out of bounds",
    "access violation",
    "не может быть открыт",
    "файл поврежд",
    "invalid package",
)

_MARKER_BYTES = tuple(m.encode("utf-8") for m in ERROR_MARKERS)

# Loginom may replace the package name with the module name in the title:
# "Package/Модуль1 · Сценарий · Loginom Community".
_TITLE_SEPARATORS = ("/", " · ", " - ")


def _run_ps(script: str, timeout: float = 20.0) -> str:
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                # Windows PowerShell writes console output in the OEM code page;
                # without this every Russian title/dialog comes back as mojibake.
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                + script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return (r.stdout or "").strip()


def _title_matches(title: str, package: str) -> bool:
    """True when a Loginom window title belongs to ``package``."""
    if not package:
        return bool(title)
    head = title
    for sep in _TITLE_SEPARATORS:
        if sep in head:
            head = head.split(sep, 1)[0]
    return package == head or package in title


def windows() -> list[dict]:
    """Visible Loginom top-level windows: [{pid, title}]."""
    ps = (
        "Get-Process loginom -ErrorAction SilentlyContinue | "
        "Where-Object { $_.MainWindowHandle -ne 0 } | "
        "ForEach-Object { \"$($_.Id)`t$($_.MainWindowTitle)\" }"
    )
    out = []
    for line in _run_ps(ps).splitlines():
        pid, _, title = line.partition("\t")
        if pid.strip().isdigit():
            out.append({"pid": int(pid), "title": title.strip()})
    return out


def titles() -> list[str]:
    return [w["title"] for w in windows() if w["title"]]


def capture_ui_text(timeout_s: float = 6.0) -> str:
    """Raw text of every visible Loginom window (blocks separated by ---)."""
    if not CAPTURE_PS1.exists():
        return ""
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                f"& '{CAPTURE_PS1}'",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return (r.stdout or "").strip()


def _has_marker(text: str) -> bool:
    lower = text.lower()
    return any(m in lower for m in ERROR_MARKERS)


def extract_errors(ui_text: str) -> list[str]:
    """Return dialog-like blocks that contain an error marker."""
    if not ui_text:
        return []
    hits: list[str] = []
    for block in re.split(r"\n---\s*\n", ui_text):
        if not _has_marker(block):
            continue
        lines = []
        for raw in block.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith(("WINTITLE:", "UIA_WINDOW:")) or len(line) >= 4:
                lines.append(line)
        text = "\n".join(lines).strip()
        if text and text not in hits:
            hits.append(text)
    return hits


def dismiss_dialogs() -> None:
    """Close modal Loginom dialogs with Escape/Enter (the OK button is default)."""
    if sys.platform != "win32":
        return
    ps = r"""
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class Kbd {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
}
"@
foreach ($p in Get-Process loginom -ErrorAction SilentlyContinue) {
  if ($p.MainWindowHandle -ne 0) {
    [void][Kbd]::SetForegroundWindow($p.MainWindowHandle)
    Start-Sleep -Milliseconds 80
    [Kbd]::keybd_event(0x1B, 0, 0, [UIntPtr]::Zero)
    [Kbd]::keybd_event(0x1B, 0, 2, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 40
    [Kbd]::keybd_event(0x0D, 0, 0, [UIntPtr]::Zero)
    [Kbd]::keybd_event(0x0D, 0, 2, [UIntPtr]::Zero)
  }
}
"""
    _run_ps(ps, timeout=20.0)


def _select(
    pattern: str | None, pids: set[int] | None
) -> list[dict]:
    """Windows matching ``pattern`` (title) OR belonging to ``pids``.

    Neither argument means *every* Loginom window.
    """
    out = []
    for w in windows():
        if pattern is None and not pids:
            out.append(w)
        elif pattern is not None and pattern in w["title"]:
            out.append(w)
        elif pids and w["pid"] in pids:
            out.append(w)
    return out


def _close_ps(pattern: str | None, pids: set[int] | None, force: bool) -> str:
    action = (
        "$_ | Stop-Process -Force"
        if force
        else "$null = $_.CloseMainWindow()"
    )
    proc = "Get-Process loginom -ErrorAction SilentlyContinue"
    if pids:
        id_list = ",".join(str(p) for p in sorted(pids))
        return f"{proc} | Where-Object {{ @({id_list}) -contains $_.Id }} | ForEach-Object {{ {action} }}"
    if pattern is not None:
        safe = pattern.replace("'", "''")
        return (
            f"{proc} | Where-Object {{ $_.MainWindowTitle -like '*{safe}*' }} | "
            f"ForEach-Object {{ {action} }}"
        )
    return f"{proc} | ForEach-Object {{ {action} }}"


def _alt_f4(pids: set[int]) -> None:
    """Ask Loginom windows to close with Alt+F4 (CEF ignores WM_CLOSE)."""
    if sys.platform != "win32" or not pids:
        return
    id_list = ",".join(str(p) for p in sorted(pids))
    ps = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"foreach ($p in Get-Process loginom -ErrorAction SilentlyContinue | "
        f"Where-Object {{ @({id_list}) -contains $_.Id }}) {{ "
        "if ($ws.AppActivate($p.Id)) { Start-Sleep -Milliseconds 250; "
        "$ws.SendKeys('%{F4}') } }"
    )
    _run_ps(ps, timeout=20.0)


def close_windows(
    pattern: str | None = None,
    force: bool = False,
    wait_s: float = 8.0,
    pids: set[int] | None = None,
) -> dict:
    """Close Loginom windows.

    Targets are windows whose title contains ``pattern`` or whose process id is
    in ``pids``. Pass neither to close *every* Loginom window (destructive: it
    also closes packages the user has open by hand).

    Loginom is a CEF application and ignores ``WM_CLOSE``, so a polite close is
    CloseMainWindow followed by Alt+F4. Only ``force=True`` uses Stop-Process.
    """
    if sys.platform != "win32":
        return {"ok": True, "skipped": "windows only", "closed": []}

    targets = _select(pattern, pids)
    if not targets:
        return {"ok": True, "closed": [], "remaining": titles()}

    _run_ps(_close_ps(pattern, pids, force=False), timeout=20.0)
    deadline = time.time() + 2.0
    while time.time() < deadline and _select(pattern, pids):
        time.sleep(0.4)
    if _select(pattern, pids):
        _alt_f4({w["pid"] for w in _select(pattern, pids)})

    deadline = time.time() + wait_s
    while time.time() < deadline:
        if not _select(pattern, pids):
            break
        time.sleep(0.4)

    remaining = _select(pattern, pids)
    if remaining and force:
        _run_ps(_close_ps(pattern, pids, force=True), timeout=20.0)
        deadline = time.time() + 6.0
        while time.time() < deadline and _select(pattern, pids):
            time.sleep(0.4)
        remaining = _select(pattern, pids)

    return {
        "ok": not remaining,
        "closed": [w["title"] for w in targets],
        "remaining": [w["title"] for w in remaining],
    }


LOG_LINE = re.compile(
    r"^(?P<time>\S+)\s+(?P<level>info|warn|warning|error|debug|trace)\s+"
    r"(?P<hash>[0-9a-fA-F]+)\s+\((?P<source>[^)]*)\)\s+-\s+(?P<body>.*)$"
)


def log_path() -> Path | None:
    """Newest Loginom ``app.log`` (``%APPDATA%/Loginom/*/Logs/app.log``)."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    candidates = list(Path(appdata).glob("Loginom/*/Logs/app.log"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def log_size() -> int:
    p = log_path()
    return p.stat().st_size if p and p.exists() else 0


def _split_body(body: str) -> tuple[str, dict]:
    """Split ``text{"Message": ...}`` into message text and parsed JSON."""
    idx = body.find('{"Message"')
    if idx < 0:
        return body.strip(), {}
    text = body[:idx].strip()
    try:
        payload = json.loads(body[idx:])
    except json.JSONDecodeError:
        return text or body.strip(), {}
    return text or str(payload.get("Message", "")).strip(), payload


def _parse_log_line(line: str) -> dict | None:
    m = LOG_LINE.match(line.rstrip("\n"))
    if not m:
        return None
    text, payload = _split_body(m.group("body"))
    inner = []
    for item in payload.get("InnerExceptions") or []:
        if isinstance(item, dict):
            msg = str(item.get("Message", "")).strip()
            if msg:
                inner.append(msg)
    return {
        "time": m.group("time"),
        "level": "warn" if m.group("level") == "warning" else m.group("level"),
        "source": m.group("source"),
        "message": text,
        "exception": payload.get("ExceptionClass"),
        "sender": payload.get("Sender"),
        "inner": inner,
    }


def read_log(
    since: int = 0,
    limit: int = 200,
    level: str | None = None,
) -> list[dict]:
    """Parse Loginom log entries written after byte offset ``since``.

    ``level="error"`` keeps only errors; the log is UTF-8.
    """
    p = log_path()
    if p is None or not p.exists():
        return []
    size = p.stat().st_size
    if since < 0 or since > size:  # rotated / truncated
        since = 0
    with p.open("rb") as fh:
        fh.seek(since)
        data = fh.read()
    entries = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        entry = _parse_log_line(line)
        if entry is None:
            continue
        if level and entry["level"] != level:
            continue
        entries.append(entry)
    return entries[-limit:]


def format_entries(entries: list[dict]) -> list[str]:
    """Human-readable one-liner per log entry (message + exception details)."""
    out = []
    for e in entries:
        head = f"[{e['time']}] {e['level'].upper()}: {e['message']}"
        if e.get("exception"):
            head += f" ({e['exception']}"
            if e.get("sender"):
                head += f" in {e['sender']}"
            head += ")"
        if e.get("inner"):
            head += " — " + "; ".join(e["inner"])
        out.append(head)
    return out


def _clear_lock(path: Path) -> str:
    lck = Path(str(path) + ".lck")
    deadline = time.time() + 8.0
    while lck.exists() and time.time() < deadline:
        time.sleep(0.3)
    if not lck.exists():
        return "none"
    try:
        lck.unlink()
    except OSError:
        # Loginom may have released it while we were trying.
        return "stuck" if lck.exists() else "released"
    return "removed"


def check_package(
    path: str | Path,
    open_ui: bool = True,
    close_after: bool = True,
    timeout_s: float = 16.0,
) -> dict:
    """Open ``path`` in Loginom, report errors, close the window again.

    The structural lint (``Unit.xml`` ↔ ``Unit.bin``) runs first; its result is
    included as ``lint_ok`` / ``lint_errors``.
    """
    from . import lgp_lint

    lgp = Path(path)
    result: dict = {
        "ok": True,
        "path": str(lgp),
        "exists": lgp.exists(),
        "opened": None,
        "dialogs": [],
        "errors": [],
        "titles": [],
    }
    if lgp.exists():
        lint = lgp_lint.lint(lgp)
        result["lint_ok"] = lint["ok"]
        if not lint["ok"]:
            result["lint_errors"] = lint["errors"]
    if not lgp.exists():
        result.update(ok=False, errors=["file not found"])
        return result
    if not open_ui:
        result["ui_skipped"] = "open_ui=False"
        return result
    if not ui_verify_available():
        result["ui_skipped"] = "Loginom UI is not available on this machine"
        return result

    exe = loginom_exe()
    assert exe is not None
    before = {w["pid"] for w in windows()}
    before_titles = set(titles())
    log_offset = log_size()
    proc = subprocess.Popen([str(exe), str(lgp.resolve())])
    own_pids: set[int] = set()
    if proc.pid not in before:
        own_pids.add(proc.pid)

    stem = lgp.stem
    deadline = time.time() + timeout_s
    opened = False
    while time.time() < deadline:
        if any(_title_matches(t, stem) for t in titles()):
            opened = True
            break
        time.sleep(0.4)

    time.sleep(1.0)
    # Loginom shows the «ошибки открытия» dialog a few seconds AFTER the
    # package window appears, and a single early UI snapshot misses it —
    # poll the dialog text several times and dismiss what we find.
    ui_text = ""
    dialogs: list[str] = []
    capture_deadline = time.time() + max(4.0, timeout_s * 0.3)
    while time.time() < capture_deadline:
        time.sleep(0.8)
        txt = capture_ui_text()
        if txt and txt != ui_text:
            ui_text = txt
            for d in extract_errors(txt):
                if d not in dialogs:
                    dialogs.append(d)
        if dialogs:
            dismiss_dialogs()
            time.sleep(0.6)
    # Loginom logs the real reason ("Ошибка загрузки пакета: ...") even when it
    # shows no dialog at all — the log is the primary source, the UI a fallback.
    log_entries = read_log(since=log_offset)
    log_errors = [e for e in log_entries if e["level"] == "error"]
    result["titles"] = titles()
    result["opened"] = opened
    result["log"] = format_entries(log_errors)
    # Not every logged error means the package is broken (Loginom logs the odd
    # "Out of memory" before a successful open). Only load/parse failures count.
    fatal = [
        e
        for e in log_errors
        if _has_marker(e["message"])
        or e.get("exception") in ("EBGAggregateException", "EBGException")
    ]

    if fatal or dialogs:
        result.update(ok=False, opened=False)
        result["dialogs"] = dialogs
        result["errors"] = format_entries(fatal) or [
            "Loginom refused to open the package"
        ]
        if dialogs:
            dismiss_dialogs()
    elif not opened:
        result.update(
            ok=False,
            errors=[f"window for {stem!r} never appeared"],
        )
        # an error dialog may have replaced the package window
        tail = extract_errors(ui_text)
        if tail:
            result["dialogs"] = tail
    elif log_errors:
        result["log_warnings"] = format_entries(
            [e for e in log_errors if e not in fatal]
        )

    if close_after:
        # Only our own window/process: never kill a Loginom the user is using.
        # `own_pids` is empty when Loginom reused an already running instance.
        close = close_windows(
            pattern=stem,
            pids=own_pids or None,
            force=bool(own_pids),
        )
        result["closed"] = close.get("closed", [])
        # An error dialog may carry no package name in its title; after Escape/
        # Enter it is gone, but if it survived, close only windows that appeared
        # during this check (never the ones that were already open).
        if not result["closed"]:
            for title in titles():
                if title and title not in before_titles and title not in result["closed"]:
                    extra = close_windows(pattern=title, force=True)
                    result["closed"] += extra.get("closed", [])
        remaining = [t for t in titles() if t not in before_titles]
        if remaining:
            result["close_failed"] = remaining
        result["lock"] = _clear_lock(lgp)

    return result
