"""Test doubles for the hidpp.protocol.Transport protocol."""

import dataclasses
from collections.abc import Callable


@dataclasses.dataclass
class ScriptedReply:
    """Reply with `reply` (payload after the echoed header) when `matcher` matches."""

    matcher: Callable[[int, bytes], bool]
    reply: bytes


def register_matcher(devnumber: int, params: bytes) -> Callable[[int, bytes], bool]:
    """Match a request to `devnumber` whose params equal `params`."""

    def matcher(actual_devnumber: int, payload: bytes) -> bool:
        return actual_devnumber == devnumber and payload[2:] == params

    return matcher


Responder = Callable[[int, bytes, bool], "bytes | None"]


class ScriptedTransport:
    """A fake Transport that replies based on scripted (devnumber, params) matchers.

    Request IDs carry a randomized SoftwareId, so replies are matched by
    devnumber + params rather than by predicting the exact on-wire header.

    For the common case (a successful register/feature read), pass `replies`:
    the fake echoes back whatever header it actually received, followed by
    the scripted reply bytes. For full control over the reply -- e.g. to
    simulate an error reply, which doesn't follow that echoing convention --
    pass `respond` instead, returning the exact reply payload to deliver.
    """

    def __init__(
        self,
        replies: list[ScriptedReply] | None = None,
        respond: Responder | None = None,
    ):
        self._replies = replies or []
        self._respond = respond
        self._pending: list[tuple[int, int, bytes]] = []
        self.writes: list[tuple[int, bytes, bool]] = []

    def write(self, devnumber: int, payload: bytes, long_message: bool) -> None:
        self.writes.append((devnumber, payload, long_message))
        report_id = 0x11 if long_message else 0x10

        if self._respond is not None:
            reply_data = self._respond(devnumber, payload, long_message)
            if reply_data is not None:
                self._pending.append((report_id, devnumber, reply_data))
            return

        for scripted in self._replies:
            if scripted.matcher(devnumber, payload):
                self._pending.append(
                    (report_id, devnumber, payload[:2] + scripted.reply)
                )
                return

    def read(self, timeout: float) -> tuple[int, int, bytes] | None:
        if self._pending:
            return self._pending.pop(0)
        return None

    def drain(self) -> None:
        self._pending.clear()


class FakeHidDevice:
    """Stands in for a `hid.device` handle open on one collection: records
    writes, replays queued input reports, and fails like hidapi once its
    collection has gone."""

    def __init__(self, hid: "FakeHid"):
        self._hid = hid
        self.path: bytes | None = None
        self.written: list[bytes] = []
        self.closed = False

    def open_path(self, path: bytes) -> None:
        if path not in self._hid.collections or path in self._hid.unopenable:
            raise OSError("open failed")
        self.path = path
        self._hid.handles.append(self)

    def write(self, buff: bytes) -> int:
        assert self.path is not None
        if self.path in self._hid.gone:
            return -1
        self.written.append(bytes(buff))
        if self._hid.respond is not None:
            reply = self._hid.respond(bytes(buff))
            if reply is not None:
                self._hid.input.setdefault(self.path, []).append(reply)
        return len(buff)

    def read(self, max_length: int, timeout_ms: int = 0) -> list[int]:
        assert timeout_ms > 0, "a timeout of 0 would block forever"
        assert self.path is not None
        if self.path in self._hid.gone:
            raise OSError("read error")
        pending = self._hid.input.get(self.path, [])
        return list(pending.pop(0)[:max_length]) if pending else []

    def error(self) -> str:
        return "device gone"

    def close(self) -> None:
        self.closed = True


class FakeHid:
    """Stands in for the `hid` module (hidapi): `collections` is what
    `enumerate()` lists, `input` the reports queued on each collection.
    `respond`, if set, turns each report written into the one read back."""

    def __init__(self) -> None:
        self.collections: dict[bytes, dict] = {}
        self.input: dict[bytes, list[bytes]] = {}
        self.handles: list[FakeHidDevice] = []
        self.gone: set[bytes] = set()
        self.unopenable: set[bytes] = set()
        self.respond: Callable[[bytes], bytes | None] | None = None

    def add(self, path: bytes, **info) -> bytes:
        self.collections[path] = {"path": path, "vendor_id": 0x046D, **info}
        return path

    def enumerate(self, vendor_id: int = 0, product_id: int = 0) -> list[dict]:
        return [
            dict(info)
            for info in self.collections.values()
            if vendor_id in (0, info["vendor_id"])
            and product_id in (0, info.get("product_id"))
        ]

    def device(self) -> FakeHidDevice:
        return FakeHidDevice(self)
