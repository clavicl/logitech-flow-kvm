from __future__ import annotations

import os
import select
import sys
import time
from typing import Any
from typing import TypeAlias

SHORT_MESSAGE_ID = 0x10
LONG_MESSAGE_ID = 0x11
SHORT_REPORT_SIZE = 7
LONG_REPORT_SIZE = 20
MAX_READ_SIZE = 32

_WRITE_RETRIES = 3
_WRITE_RETRY_DELAY = 0.1

# Linux has hidraw; everywhere else (in practice, Windows) goes through hidapi.
USE_HIDAPI = sys.platform == "win32"


def _frame(
    devnumber: int, payload: bytes, long_message: bool, long_only: bool
) -> bytes:
    if long_only or long_message or len(payload) > SHORT_REPORT_SIZE - 2:
        body = payload.ljust(LONG_REPORT_SIZE - 2, b"\x00")
        return bytes([LONG_MESSAGE_ID, devnumber]) + body
    body = payload.ljust(SHORT_REPORT_SIZE - 2, b"\x00")
    return bytes([SHORT_MESSAGE_ID, devnumber]) + body


class HidRawIO:
    """Raw read/write access to a single ``/dev/hidraw*`` node.

    A receiver's hidraw node broadcasts every incoming report to all open file
    descriptors, so each thread that needs to block on reads (e.g. a
    notification listener) should open its own ``HidRawIO`` rather than share
    one across threads -- otherwise a blocking read on one thread can steal
    the reply another thread's request is waiting for.

    `long_only` sends every report as a long (0x11) one, however short its
    payload. A Bluetooth device's own hidraw node declares only the long
    HID++ report in its descriptor, so a short report written to it is
    silently dropped.
    """

    def __init__(self, path: str, *, long_only: bool = False):
        self.path = path
        self.long_only = long_only
        self._fd = os.open(path, os.O_RDWR)

    def close(self) -> None:
        os.close(self._fd)

    def __enter__(self) -> HidRawIO:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def write(self, devnumber: int, payload: bytes, long_message: bool) -> None:
        report = _frame(devnumber, payload, long_message, self.long_only)

        written = 0
        for attempt in range(_WRITE_RETRIES):
            try:
                written = os.write(self._fd, report)
            except BrokenPipeError:
                if attempt == _WRITE_RETRIES - 1:
                    raise
                time.sleep(_WRITE_RETRY_DELAY)
                continue
            break

        if written != len(report):
            raise OSError(f"short write: {written}/{len(report)} bytes")

    def read(self, timeout: float) -> tuple[int, int, bytes] | None:
        """Read one report, or None if nothing arrived within `timeout` seconds."""
        rlist, _, _ = select.select([self._fd], [], [], timeout)
        if not rlist:
            return None
        data = os.read(self._fd, MAX_READ_SIZE)
        if not data:
            return None
        return data[0], data[1], data[2:]

    def drain(self) -> None:
        """Discard any reports already waiting in the kernel buffer."""
        while self.read(0) is not None:
            pass


# A receiver's HID++ interface shows up on Windows as two top-level
# collections on this usage page: one per report size. Each takes writes of
# its own report only, so a receiver needs both open.
HIDPP_RECEIVER_USAGE_PAGE = 0xFF00
HIDPP_SHORT_USAGE = 0x0001

# How long `HidApiIO.read` waits on one collection before trying the other.
_INTERLEAVE_INTERVAL = 0.01


def _collection_parent(path: str) -> str:
    r"""The part of a Windows HID path shared by all top-level collections of
    one interface: drops the `&ColNN` from the hardware id and the trailing
    collection index from the instance id, e.g.
    ``\\?\HID#VID_046D&PID_C548&MI_02&Col02#7&2a&0&0001#{...}`` ->
    ``\\?\hid#vid_046d&pid_c548&mi_02#7&2a&0``.
    """
    parts = path.lower().split("#")
    if len(parts) < 3:
        return path.lower()
    hardware_id = parts[1].split("&col", 1)[0]
    instance_id = parts[2].rsplit("&", 1)[0]
    return "#".join([parts[0], hardware_id, instance_id])


def _find_short_collection(hid: Any, path: str) -> str | None:
    parent = _collection_parent(path)
    for info in hid.enumerate():
        other = info["path"].decode()
        if (
            other != path
            and info.get("usage_page") == HIDPP_RECEIVER_USAGE_PAGE
            and info.get("usage") == HIDPP_SHORT_USAGE
            and _collection_parent(other) == parent
        ):
            return other
    return None


class HidApiIO:
    """The same as `HidRawIO`, through hidapi, for platforms without hidraw.

    `path` is a hidapi path naming the collection that carries long (0x11)
    reports. Unless `long_only`, the matching short (0x10) collection is
    opened alongside it, if there is one, and reads come from both.

    Like a hidraw node, every open handle to a collection gets its own copy
    of each incoming report, so the same one-handle-per-thread rule applies.
    """

    def __init__(self, path: str, *, long_only: bool = False):
        import hid

        self.path = path
        self.long_only = long_only
        self._long = hid.device()
        self._long.open_path(path.encode())
        self._short: Any = None
        if not long_only:
            short_path = _find_short_collection(hid, path)
            if short_path is not None:
                self._short = hid.device()
                try:
                    self._short.open_path(short_path.encode())
                except OSError:
                    self._long.close()
                    raise

    def close(self) -> None:
        self._long.close()
        if self._short is not None:
            self._short.close()

    def __enter__(self) -> HidApiIO:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def write(self, devnumber: int, payload: bytes, long_message: bool) -> None:
        long_only = self.long_only or self._short is None
        report = _frame(devnumber, payload, long_message, long_only)
        handle = self._long if report[0] == LONG_MESSAGE_ID else self._short
        if handle.write(report) < 0:
            raise OSError(f"write to {self.path} failed: {handle.error()}")

    def _read_one(self, handle: Any, timeout: float) -> tuple[int, int, bytes] | None:
        # hidapi treats a timeout of 0 as "block forever".
        data = handle.read(MAX_READ_SIZE, max(1, int(timeout * 1000)))
        if not data:
            return None
        return data[0], data[1], bytes(data[2:])

    def read(self, timeout: float) -> tuple[int, int, bytes] | None:
        """Read one report, or None if nothing arrived within `timeout` seconds."""
        if self._short is None:
            return self._read_one(self._long, timeout)
        deadline = time.monotonic() + timeout
        while True:
            for handle in (self._long, self._short):
                remaining = max(0.0, deadline - time.monotonic())
                report = self._read_one(handle, min(remaining, _INTERLEAVE_INTERVAL))
                if report is not None:
                    return report
            if time.monotonic() >= deadline:
                return None

    def drain(self) -> None:
        while self.read(0) is not None:
            pass


HidIO: TypeAlias = HidRawIO | HidApiIO


def open_device(path: str, *, long_only: bool = False) -> HidIO:
    """Open a HID++ node with whichever backend this platform uses."""
    if USE_HIDAPI:
        return HidApiIO(path, long_only=long_only)
    return HidRawIO(path, long_only=long_only)
