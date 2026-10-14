"""`flow-client` in the background, behind a notification-area icon.

Installed as a GUI script, so Windows starts it without a console window.
Takes the same arguments as `flow-client`:

    logitech-flow-kvm-tray 2 192.168.1.168

The client runs on a worker thread exactly as it would non-interactively
(plain logging, no TUI); the icon's menu opens its log or quits it.
"""

from __future__ import annotations

import ctypes
import io
import logging
import os
import sys
import threading
from importlib import resources

from safdie import SafdieRunner

from .logging_setup import configure_logging
from .logging_setup import get_log_path

logger = logging.getLogger(__name__)

TITLE = "Logitech Flow KVM"
# One client per user session: a second would fight the first over the
# same devices.
MUTEX_NAME = "Local\\logitech-flow-kvm-tray"
ERROR_ALREADY_EXISTS = 183

# Held, never closed, for as long as this process runs.
_mutex = None


class _NullStream(io.TextIOBase):
    """Somewhere for output to go without a console. Not `os.devnull`: on
    Windows that is a character device, so it claims to be a TTY, and the
    client would try to start its TUI on it."""

    def write(self, s: str) -> int:
        return len(s)

    def isatty(self) -> bool:
        return False


def _already_running() -> bool:
    global _mutex
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    _mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() == ERROR_ALREADY_EXISTS  # type: ignore[attr-defined,unused-ignore]


def _run_client(argv: list[str], icon) -> None:
    try:
        SafdieRunner("logitech_flow_kvm.commands").run(["flow-client", *argv])
    except SystemExit as exit:
        if exit.code not in (None, 0):
            logger.error("flow-client exited with status %s", exit.code)
    except Exception:
        logger.exception("flow-client stopped with an error")
    icon.title = f"{TITLE} (stopped; see the log)"
    icon.notify("The client has stopped. Open the log for details.", TITLE)


def main() -> None:
    # A GUI script has no console, so these are None -- and the client's
    # `sys.stdout.isatty()` checks would fail on that.
    if sys.stdout is None:
        sys.stdout = _NullStream()
    if sys.stderr is None:
        sys.stderr = _NullStream()

    configure_logging()
    if _already_running():
        logger.info("The tray client is already running; not starting another")
        return

    import pystray
    from PIL import Image

    with resources.files(__package__).joinpath("icon.png").open("rb") as f:
        image = Image.open(f)
        image.load()

    def open_log(icon, item) -> None:
        os.startfile(get_log_path())  # type: ignore[attr-defined,unused-ignore]

    def quit_(icon, item) -> None:
        logger.info("Quitting from the tray icon")
        icon.stop()

    icon = pystray.Icon(
        "logitech-flow-kvm",
        image,
        TITLE,
        menu=pystray.Menu(
            pystray.MenuItem("Open log", open_log, default=True),
            pystray.MenuItem("Quit", quit_),
        ),
    )

    def setup(icon) -> None:
        icon.visible = True
        # A daemon thread: quitting just ends the process, which closes
        # every device handle and the server connection along with it.
        threading.Thread(
            target=_run_client, args=(sys.argv[1:], icon), daemon=True
        ).start()

    icon.run(setup=setup)
