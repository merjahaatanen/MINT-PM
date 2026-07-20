"""
Persistent logged-in Chrome session manager
============================================
The nightly scrape attaches to a Chrome browser that is already logged into the
Bobrick PM system. On the VM the remote-debugging PORT is not fixed, so instead
of hard-coding 9222 we:

  1. Launch Chrome once with a dedicated debug profile and
     ``--remote-debugging-port=0`` (let Chrome pick a free port).
  2. Read the actual port Chrome chose from the ``DevToolsActivePort`` file that
     Chrome writes into the profile directory. This is the reliable, official
     way to discover the port.
  3. Verify the debug endpoint answers, and expose the port to the scraper.

The window is left OPEN so a human can log in on the VM if the session ever
expires. The same session is reused for every nightly run.

Usage (standalone, for testing):
    python chrome_session.py            # launches + prints the captured port
    python chrome_session.py --status   # just report a running session's port
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))

# Dedicated, already-logged-in debug profile. Override with CHROME_PROFILE in
# the environment (.env) if Chrome lives elsewhere on the VM.
DEFAULT_PROFILE = os.path.join(
    LOCALAPPDATA, "Google", "Chrome", "PM_Debug_Profile"
)
PROFILE = os.environ.get("CHROME_PROFILE", "").strip() or DEFAULT_PROFILE

# 0 => let Chrome pick a free port and we capture it. Set CHROME_DEBUG_PORT to
# pin a specific port instead.
CONFIG_PORT = int(os.environ.get("CHROME_DEBUG_PORT", "0") or "0")

DASH_HOME = "https://circaweb.bobrick.com/PME/Forms/EquipmentAll"

CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH", "").strip(),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

# Where we remember the last captured port between processes (so the server and
# the nightly job can share one Chrome without re-launching).
PORT_FILE = os.path.join(HERE, ".chrome_debug_port")


def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if path and os.path.exists(path):
            return path
    sys.exit(
        "ERROR: Could not find chrome.exe. Set CHROME_PATH in your .env to the "
        "full path of chrome.exe on this machine."
    )


def _endpoint_ok(port: int, timeout: float = 1.0) -> bool:
    """True if a Chrome remote-debugging endpoint answers on this port."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as r:
            return r.status == 200
    except Exception:
        return False


def _page_count(port: int) -> int:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json", timeout=2
        ) as r:
            targets = json.loads(r.read().decode("utf-8", "replace"))
            return sum(1 for t in targets if t.get("type") == "page")
    except Exception:
        return -1


def _read_devtools_port() -> int | None:
    """Chrome writes the chosen port to <profile>/DevToolsActivePort."""
    path = os.path.join(PROFILE, "DevToolsActivePort")
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline().strip()
            return int(first) if first.isdigit() else None
    except (OSError, ValueError):
        return None


def _save_port(port: int):
    try:
        with open(PORT_FILE, "w", encoding="utf-8") as f:
            f.write(str(port))
    except OSError:
        pass


def _load_saved_port() -> int | None:
    try:
        with open(PORT_FILE, encoding="utf-8") as f:
            v = f.read().strip()
            return int(v) if v.isdigit() else None
    except (OSError, ValueError):
        return None


def find_running_port() -> int | None:
    """Return the port of an already-running debug Chrome, if any.

    Checks, in order: a pinned CONFIG_PORT, the last port we saved, and the
    port recorded in the profile's DevToolsActivePort file.
    """
    for candidate in (CONFIG_PORT, _load_saved_port(), _read_devtools_port()):
        if candidate and _endpoint_ok(candidate):
            return candidate
    return None


def launch(headless: bool = False, timeout: int = 45) -> int:
    """Launch Chrome on the debug profile and return the captured port.

    Reuses an existing session if one is already running.
    """
    existing = find_running_port()
    if existing:
        print(f"[chrome_session] reusing running Chrome on port {existing}")
        _save_port(existing)
        return existing

    chrome = find_chrome()
    os.makedirs(PROFILE, exist_ok=True)

    # Remove any stale DevToolsActivePort so we don't read an old value.
    stale = os.path.join(PROFILE, "DevToolsActivePort")
    try:
        os.remove(stale)
    except OSError:
        pass

    args = [
        chrome,
        f"--remote-debugging-port={CONFIG_PORT}",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--restore-last-session=false",
        DASH_HOME,
    ]
    if headless:
        args.insert(1, "--headless=new")
        args.insert(2, "--disable-gpu")

    print(f"[chrome_session] launching Chrome (profile: {PROFILE}) ...")
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Wait for Chrome to write DevToolsActivePort and for the endpoint to answer.
    end = time.time() + timeout
    port = None
    while time.time() < end:
        port = _read_devtools_port() if CONFIG_PORT == 0 else CONFIG_PORT
        if port and _endpoint_ok(port):
            break
        time.sleep(0.5)

    if not port or not _endpoint_ok(port):
        sys.exit(
            "ERROR: Chrome did not expose a debug endpoint in time. Make sure "
            "no conflicting Chrome is running on this profile and try again."
        )

    # Wait for at least one usable page target.
    while time.time() < end and _page_count(port) < 1:
        time.sleep(0.5)

    print(f"[chrome_session] Chrome ready on port {port}")
    _save_port(port)
    return port


def is_logged_in(port: int) -> bool:
    """Heuristic auth check: the current tab should be on the PM site and NOT a
    login/sign-in page. Used by the nightly job so it never overwrites good data
    with a scrape that failed because the session expired.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json", timeout=3
        ) as r:
            targets = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return False
    for t in targets:
        if t.get("type") != "page":
            continue
        url = (t.get("url") or "").lower()
        if "circaweb.bobrick.com/pme" in url and "login" not in url and "signin" not in url:
            return True
    return False


def ensure_session(require_login: bool = True) -> int:
    """Ensure a debug Chrome is running and return its port.

    If require_login is True and the session does not look authenticated, this
    still returns the port (the caller decides how to handle it) but prints a
    clear warning.
    """
    port = launch()
    if require_login and not is_logged_in(port):
        print(
            "[chrome_session] WARNING: Chrome does not appear to be logged into "
            f"the PM site. Open the Chrome window on this machine, sign in, and "
            f"navigate to {DASH_HOME}."
        )
    return port


def main():
    ap = argparse.ArgumentParser(description="Manage the logged-in debug Chrome")
    ap.add_argument("--status", action="store_true",
                    help="Only report the port of a running debug Chrome.")
    ap.add_argument("--headless", action="store_true",
                    help="Launch headless (not recommended - you can't log in).")
    args = ap.parse_args()

    if args.status:
        port = find_running_port()
        if port:
            print(f"Running on port {port} (logged_in={is_logged_in(port)})")
        else:
            print("No running debug Chrome found.")
        return

    port = launch(headless=args.headless)
    print(f"PORT={port}")
    print(f"logged_in={is_logged_in(port)}")


if __name__ == "__main__":
    main()
