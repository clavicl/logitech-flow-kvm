"""Logitech devices connected straight to this host over Bluetooth.

The rest of this package is built around receivers: one hidraw node, many
paired devices behind it, and the receiver reporting their comings and goings
as connection notifications. A Bluetooth device has none of that. It gets a
hidraw node of its own, speaks HID++ on it as device number 0xFF, and the node
itself is the only sign of whether the device is here: it exists while the
device is connected to this host and vanishes when it Easy-Switches away.

`DirectDevice` adapts that to the receiver-shaped interface everything else
expects -- a one-slot receiver whose slot is always "paired" -- and
`PresenceWatcher` stands in for the receiver's connection notifications by
watching for the node to come and go.
"""

from __future__ import annotations

import logging
import struct
import threading
from collections.abc import Callable
from typing import Protocol
from typing import TypeAlias

from .discovery import find_bluetooth_devices
from .exceptions import ERROR_CONNECTION_REQUEST_FAILED
from .exceptions import ProtocolError
from .models import BluetoothNodeInfo
from .models import ChangeHostInfo
from .models import Notification
from .protocol import ROOT_FEATURE_INDEX
from .protocol import HidppConnection
from .receiver import CHANGE_HOST_READ_FUNCTION
from .receiver import CHANGE_HOST_WRITE_FUNCTION
from .receiver import FEATURE_CHANGE_HOST
from .receiver import PairedDevice
from .receiver import Receiver
from .transport import LONG_MESSAGE_ID
from .transport import HidRawIO

logger = logging.getLogger(__name__)

# A device addressed directly, rather than through a receiver, is always 0xFF.
DIRECT_DEVNUMBER = 0xFF

FEATURE_DEVICE_INFORMATION = 0x0003
DEVICE_INFORMATION_READ_FUNCTION = 0x00

PATH_PREFIX = "bt:"

# How often `PresenceWatcher` checks sysfs for the device's node.
PRESENCE_POLL_INTERVAL = 1.0

CONNECTION_SUB_ID = 0x41
# The `link_status` bit of a 0x41 notification's first byte (see
# `util.parse_connection_status`): set means "link not established".
LINK_NOT_ESTABLISHED = 0x40


class ClosableTransport(Protocol):
    def write(self, devnumber: int, payload: bytes, long_message: bool) -> None: ...

    def read(self, timeout: float) -> tuple[int, int, bytes] | None: ...

    def drain(self) -> None: ...

    def close(self) -> None: ...


TransportOpener = Callable[[str], ClosableTransport]
NodeFinder = Callable[[], list[BluetoothNodeInfo]]


def open_hidraw(path: str) -> ClosableTransport:
    return HidRawIO(path, long_only=True)


def normalize_address(address: str) -> str:
    return address.replace(":", "").upper()


def _unreachable() -> ProtocolError:
    """The error a receiver answers with for a device it holds no link to;
    raised here for the same situation, so callers handle both alike."""
    return ProtocolError(ERROR_CONNECTION_REQUEST_FAILED)


def _feature_index(conn: HidppConnection, feature_id: int) -> int | None:
    """Like `HidppConnection.get_feature_index`, but tells "the device did not
    answer" (raised as unreachable) apart from "the device lacks the feature"
    (None)."""
    reply = conn.request(
        DIRECT_DEVNUMBER, ROOT_FEATURE_INDEX << 8, struct.pack("!H", feature_id)
    )
    if not reply:
        raise _unreachable()
    return reply[0] or None


# Node identity, cached so each node is only queried once per connection.
# Keyed by path *and* address: hidraw numbers are reused, so a path alone
# could later belong to a different device.
_ids: dict[tuple[str, str], str] = {}
_ids_lock = threading.Lock()
_permission_warned: set[str] = set()


def _read_device_id(transport: ClosableTransport, node: BluetoothNodeInfo) -> str:
    """The device's unitId (feature 0x0003), which is what a receiver reports
    as its serial -- so the same id names the device either way it connects.
    Falls back to the Bluetooth address for a device without that feature."""
    conn = HidppConnection(transport)
    try:
        index = _feature_index(conn, FEATURE_DEVICE_INFORMATION)
        if index is not None:
            reply = conn.request(
                DIRECT_DEVNUMBER, (index << 8) | DEVICE_INFORMATION_READ_FUNCTION
            )
            if reply is None:
                raise _unreachable()
            unit_id = reply[1:5]
            if len(unit_id) == 4 and any(unit_id):
                return unit_id.hex().upper()
    except ProtocolError as error:
        if error.means_unreachable:
            raise
    return normalize_address(node.address)


def identify(
    node: BluetoothNodeInfo, *, open_transport: TransportOpener = open_hidraw
) -> str | None:
    """The device id behind `node`, or None if it can't be worked out right
    now (the node can't be opened, or the device didn't answer)."""
    key = (node.path, node.address)
    with _ids_lock:
        if key in _ids:
            return _ids[key]
        try:
            transport = open_transport(node.path)
        except PermissionError:
            if node.path not in _permission_warned:
                _permission_warned.add(node.path)
                logger.warning(
                    "No permission to open %s (%s); run as root or add a udev "
                    "rule granting access to it",
                    node.path,
                    node.name,
                )
            return None
        except OSError as error:
            logger.debug("Could not open %s: %s", node.path, error)
            return None
        try:
            device_id = _read_device_id(transport, node)
        except (OSError, ProtocolError) as error:
            logger.debug("Could not identify %s: %s", node.path, error)
            return None
        finally:
            try:
                transport.close()
            except OSError:
                pass
        _ids[key] = device_id
        return device_id


def scan(
    *,
    find_nodes: NodeFinder = find_bluetooth_devices,
    open_transport: TransportOpener = open_hidraw,
) -> list[tuple[BluetoothNodeInfo, str]]:
    """Every Logitech HID++ device connected over Bluetooth right now, with
    its id. Forgets the ids of nodes that have gone."""
    nodes = find_nodes()
    present = {(node.path, node.address) for node in nodes}
    with _ids_lock:
        for key in list(_ids):
            if key not in present:
                del _ids[key]
    found = []
    for node in nodes:
        device_id = identify(node, open_transport=open_transport)
        if device_id is not None:
            found.append((node, device_id))
    return found


class DirectDevice:
    """A Bluetooth device, presented as a receiver with a single slot.

    Created from a device id alone, so it can stand for a device that is
    connected to some other host at the moment; `refresh()` (normally called
    by a `PresenceWatcher`) opens the device's node when it turns up and
    closes it when it goes. While it is away, every request fails the way a
    receiver's would for a device it holds no link to: with an "unreachable"
    `ProtocolError`.

    `paired_device` is created once and never replaced, unlike a receiver's
    `PairedDevice`s which are re-read from its registers on demand, so it
    stays the same dictionary key (see `reconciler.Reconciler`) however many
    times the device leaves and returns.
    """

    kind = "bluetooth"
    max_devices = 1

    def __init__(
        self,
        device_id: str,
        *,
        node: BluetoothNodeInfo | None = None,
        find_nodes: NodeFinder = find_bluetooth_devices,
        open_transport: TransportOpener = open_hidraw,
    ):
        """`node`, if the device is connected right now, only supplies its
        product id and name for display; it is not opened until `refresh()`.
        `find_nodes`/`open_transport` are test seams."""
        self.device_id = device_id
        self.path = PATH_PREFIX + device_id
        self._find_nodes = find_nodes
        self._open_transport = open_transport
        # Reentrant, and held across each request: `refresh()` from the
        # watcher thread must not close the transport under a request in
        # flight on another.
        self._lock = threading.RLock()
        self._node: BluetoothNodeInfo | None = None
        self._transport: ClosableTransport | None = None
        self._conn: HidppConnection | None = None
        # Set by `notify_devices()` to have the watcher re-announce.
        self.announce_requested = threading.Event()
        self.paired_device = PairedDevice(
            receiver=self,
            number=DIRECT_DEVNUMBER,
            wpid=f"{node.product_id:04X}" if node else "",
            kind="bluetooth",
            serial=device_id,
            codename=(node.name or None) if node else None,
        )

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def matches(self, node: BluetoothNodeInfo, device_id: str) -> bool:
        return (
            device_id == self.device_id
            or normalize_address(node.address) == self.device_id
        )

    def refresh(self) -> bool:
        """Bring the open node in line with sysfs; returns whether the device
        is connected here."""
        found = scan(find_nodes=self._find_nodes, open_transport=self._open_transport)
        with self._lock:
            candidates = [
                node for node, device_id in found if self.matches(node, device_id)
            ]
            if self._node is not None:
                if self._node in candidates:
                    return True
                self._detach()
            for node in candidates:
                try:
                    self._attach(node)
                except OSError as error:
                    logger.debug("Could not open %s: %s", node.path, error)
                    continue
                return True
            return False

    def _attach(self, node: BluetoothNodeInfo) -> None:
        transport = self._open_transport(node.path)
        self._transport = transport
        self._conn = HidppConnection(transport)
        self._node = node
        logger.debug("Bluetooth device %s is at %s", self.device_id, node.path)

    def _detach(self) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            except OSError:
                pass
        self._transport = None
        self._conn = None
        self._node = None

    def close(self) -> None:
        with self._lock:
            self._detach()

    def __enter__(self) -> DirectDevice:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _call(self, fn: Callable[[HidppConnection], object]) -> object:
        with self._lock:
            if self._conn is None:
                raise _unreachable()
            try:
                return fn(self._conn)
            except OSError as error:
                # The node went away mid-request: the device just left.
                logger.debug("Lost %s mid-request: %s", self.device_id, error)
                self._detach()
                raise _unreachable() from error

    def get_device(self, number: int) -> PairedDevice | None:
        return self.paired_device if number == DIRECT_DEVNUMBER else None

    def enumerate_devices(self) -> list[PairedDevice]:
        # Always "paired", like a receiver's sleeping devices: whether it's
        # here right now is what connection notifications are for.
        return [self.paired_device]

    def ping_device(self, number: int, *, timeout: float = 1.5) -> float | None:
        try:
            result = self._call(
                lambda conn: conn.ping(DIRECT_DEVNUMBER, timeout=timeout)
            )
        except ProtocolError:
            return None
        assert result is None or isinstance(result, float)
        return result

    def get_change_host_info(self, number: int) -> ChangeHostInfo | None:
        def read(conn: HidppConnection) -> ChangeHostInfo | None:
            feature_index = _feature_index(conn, FEATURE_CHANGE_HOST)
            if feature_index is None:
                return None
            reply = conn.request(
                DIRECT_DEVNUMBER, (feature_index << 8) | CHANGE_HOST_READ_FUNCTION
            )
            if reply is None:
                # No answer from a node that's still there: the device is
                # most likely mid-switch, its node not yet torn down.
                raise _unreachable()
            if len(reply) < 2:
                return None
            return ChangeHostInfo(
                feature_index=feature_index, num_hosts=reply[0], current_host=reply[1]
            )

        result = self._call(read)
        assert result is None or isinstance(result, ChangeHostInfo)
        return result

    def set_current_host(self, number: int, feature_index: int, host: int) -> None:
        """See `Receiver.set_current_host`; `host` is 0-indexed."""
        self._call(
            lambda conn: conn.request(
                DIRECT_DEVNUMBER,
                (feature_index << 8) | CHANGE_HOST_WRITE_FUNCTION,
                bytes([host]),
                no_reply=True,
            )
        )

    def enable_connection_notifications(self) -> None:
        """Nothing to enable: presence is watched, not reported."""

    def notify_devices(self) -> None:
        """Have the `PresenceWatcher` re-announce the current connection state."""
        self.announce_requested.set()

    def connection_notification(self, connected: bool) -> Notification:
        """A synthetic 0x41 notification, as a receiver would send."""
        return Notification(
            report_id=LONG_MESSAGE_ID,
            devnumber=DIRECT_DEVNUMBER,
            sub_id=CONNECTION_SUB_ID,
            address=0x00,
            data=bytes([0x00 if connected else LINK_NOT_ESTABLISHED, 0, 0]),
        )


# Anything that can stand behind a `PairedDevice`.
AnyReceiver: TypeAlias = Receiver | DirectDevice


class PresenceWatcher(threading.Thread):
    """Delivers a `DirectDevice`'s connects and disconnects as 0x41
    notifications, the way a `NotificationListener` does for a receiver.

    Announces the initial state on start, every change after that, and the
    current state again whenever `notify_devices()` asks. Unlike a
    `NotificationListener`, it has no `on_disconnect`: the device leaving is
    the normal case here, not a failure to recover from.
    """

    def __init__(
        self,
        device: DirectDevice,
        callback: Callable[[Notification], None],
        interval: float = PRESENCE_POLL_INTERVAL,
    ):
        super().__init__(daemon=True)
        self._device = device
        self._callback = callback
        self._interval = interval
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()
        self._device.announce_requested.set()

    def run(self) -> None:
        announced: bool | None = None
        while not self._stop_event.is_set():
            try:
                connected = self._device.refresh()
            except Exception:
                logger.exception(
                    "Could not check whether %s is here", self._device.path
                )
                connected = self._device.connected
            reannounce = self._device.announce_requested.is_set()
            self._device.announce_requested.clear()
            if self._stop_event.is_set():
                break
            if connected != announced or reannounce:
                announced = connected
                try:
                    self._callback(self._device.connection_notification(connected))
                except Exception:
                    # Same reasoning as `NotificationListener`: this thread is
                    # the only source of this device's connection events.
                    logger.exception(
                        "Notification callback failed; continuing to watch"
                    )
            self._device.announce_requested.wait(self._interval)
