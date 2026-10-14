import logging
import os
import threading
import time

import pytest

from hidpp_fakes import ScriptedTransport
from logitech_flow_kvm import util
from logitech_flow_kvm.exceptions import DeviceNotFound
from logitech_flow_kvm.hidpp import DeviceUnreachable
from logitech_flow_kvm.hidpp import ProtocolError
from logitech_flow_kvm.hidpp import direct
from logitech_flow_kvm.hidpp import discovery
from logitech_flow_kvm.hidpp.direct import DirectDevice
from logitech_flow_kvm.hidpp.direct import PresenceWatcher
from logitech_flow_kvm.hidpp.models import BluetoothNodeInfo
from logitech_flow_kvm.hidpp.transport import HidRawIO
from logitech_flow_kvm.util import parse_connection_status

KEYBOARD_UNIT_ID = bytes.fromhex("F83FB9E1")
KEYBOARD_ID = "F83FB9E1"

FEATURE_INDEXES = {0x0003: 0x02, 0x1814: 0x0A}


def wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("condition never became true")


class FakeBluetoothDevice:
    """Answers HID++ 2.0 requests the way an MX Keys S did over Bluetooth."""

    def __init__(self, unit_id: bytes = KEYBOARD_UNIT_ID, features=None):
        self.unit_id = unit_id
        self.features = FEATURE_INDEXES if features is None else features
        self.current_host = 2
        self.host_writes: list[int] = []

    def respond(self, devnumber: int, payload: bytes, long_message: bool):
        assert devnumber == 0xFF
        index, function = payload[0], payload[1] >> 4
        header = payload[:2]
        if index == 0x00 and function == 0:
            feature = int.from_bytes(payload[2:4], "big")
            return header + bytes([self.features.get(feature, 0), 0, 0])
        if index == 0x00 and function == 1:  # ping
            return header + bytes([4, 5, payload[4]])
        if index == self.features.get(0x0003) and function == 0:
            return header + bytes([0x03]) + self.unit_id + b"\x00\x02\xb3\x78"
        if index == self.features.get(0x1814) and function == 0:
            return header + bytes([3, self.current_host])
        if index == self.features.get(0x1814) and function == 1:
            self.host_writes.append(payload[2])
            return None
        raise AssertionError(f"unexpected request {payload.hex()}")


class FakeIO(ScriptedTransport):
    def __init__(self, device: FakeBluetoothDevice):
        super().__init__(respond=device.respond)
        self.closed = False
        self.fail_writes = False

    def write(self, devnumber: int, payload: bytes, long_message: bool) -> None:
        if self.fail_writes:
            raise OSError("node went away")
        super().write(devnumber, payload, long_message)

    def close(self) -> None:
        self.closed = True


class FakeBluetooth:
    """Stands in for sysfs (`find_nodes`) and /dev (`open_transport`)."""

    def __init__(self):
        self.nodes: list[BluetoothNodeInfo] = []
        self.devices: dict[str, FakeBluetoothDevice] = {}
        self.opened: list[FakeIO] = []
        self.unopenable: set[str] = set()

    def connect(
        self,
        path: str = "/dev/hidraw1",
        device: FakeBluetoothDevice | None = None,
        address: str = "d4:82:c5:97:17:4d",
    ) -> FakeBluetoothDevice:
        device = device or FakeBluetoothDevice()
        self.nodes.append(
            BluetoothNodeInfo(
                path=path, product_id=0xB378, name="MX KEYS S", address=address
            )
        )
        self.devices[path] = device
        return device

    def disconnect(self, path: str = "/dev/hidraw1") -> None:
        self.nodes = [node for node in self.nodes if node.path != path]
        del self.devices[path]

    def find_nodes(self) -> list[BluetoothNodeInfo]:
        return list(self.nodes)

    def open_transport(self, path: str) -> FakeIO:
        if path in self.unopenable:
            raise PermissionError(13, "Permission denied", path)
        io = FakeIO(self.devices[path])
        self.opened.append(io)
        return io

    def direct_device(self, device_id: str = KEYBOARD_ID) -> DirectDevice:
        return DirectDevice(
            device_id, find_nodes=self.find_nodes, open_transport=self.open_transport
        )


@pytest.fixture(autouse=True)
def clear_id_cache():
    direct._ids.clear()
    direct._permission_warned.clear()
    yield
    direct._ids.clear()


@pytest.fixture
def bt():
    return FakeBluetooth()


def _write_node(root, name: str, uevent: list[str], descriptor: bytes) -> None:
    device_dir = os.path.join(root, name, "device")
    os.makedirs(device_dir, exist_ok=True)
    with open(os.path.join(device_dir, "uevent"), "w") as f:
        f.write("\n".join(uevent) + "\n")
    with open(os.path.join(device_dir, "report_descriptor"), "wb") as f:
        f.write(descriptor)


class TestFindBluetoothDevices:
    def test_finds_a_logitech_bluetooth_node_speaking_hidpp(
        self, tmp_path, monkeypatch
    ):
        _write_node(
            str(tmp_path),
            "hidraw1",
            [
                "HID_ID=0005:0000046D:0000B378",
                "HID_NAME=MX KEYS S",
                "HID_UNIQ=d4:82:c5:97:17:4d",
            ],
            b"\x05\x01\x85\x01\x06\x43\xff\x85\x11\x95\x13",
        )
        monkeypatch.setattr(discovery, "HIDRAW_SYSFS_GLOB", str(tmp_path / "hidraw*"))

        assert discovery.find_bluetooth_devices() == [
            BluetoothNodeInfo(
                path="/dev/hidraw1",
                product_id=0xB378,
                name="MX KEYS S",
                address="d4:82:c5:97:17:4d",
            )
        ]

    def test_ignores_a_node_without_the_hidpp_long_report(self, tmp_path, monkeypatch):
        _write_node(
            str(tmp_path),
            "hidraw1",
            ["HID_ID=0005:0000046D:0000B378"],
            b"\x05\x01\x85\x01",
        )
        monkeypatch.setattr(discovery, "HIDRAW_SYSFS_GLOB", str(tmp_path / "hidraw*"))

        assert discovery.find_bluetooth_devices() == []

    def test_ignores_usb_and_other_vendors(self, tmp_path, monkeypatch):
        _write_node(
            str(tmp_path),
            "hidraw0",
            ["HID_ID=0003:0000046D:0000C548"],
            b"\x85\x11",
        )
        _write_node(
            str(tmp_path),
            "hidraw2",
            ["HID_ID=0005:0000045E:00000001"],
            b"\x85\x11",
        )
        monkeypatch.setattr(discovery, "HIDRAW_SYSFS_GLOB", str(tmp_path / "hidraw*"))

        assert discovery.find_bluetooth_devices() == []
        # Receivers are still found as before.
        assert discovery.find_receivers() == []


class TestLongOnlyTransport:
    def test_sends_short_payloads_as_long_reports(self, tmp_path):
        fifo = str(tmp_path / "hidraw")
        os.mkfifo(fifo)
        with HidRawIO(fifo, long_only=True) as io:
            io.write(0xFF, b"\x00\x18\x00\x00\x5a", long_message=False)
            report = os.read(io._fd, 64)

        assert report[:2] == b"\x11\xff"
        assert len(report) == 20

    def test_sends_short_reports_by_default(self, tmp_path):
        fifo = str(tmp_path / "hidraw")
        os.mkfifo(fifo)
        with HidRawIO(fifo) as io:
            io.write(0xFF, b"\x00\x18\x00\x00\x5a", long_message=False)
            report = os.read(io._fd, 64)

        assert report[:2] == b"\x10\xff"
        assert len(report) == 7


class TestIdentify:
    def test_uses_the_unit_id(self, bt):
        bt.connect()

        assert direct.scan(
            find_nodes=bt.find_nodes, open_transport=bt.open_transport
        ) == [(bt.nodes[0], KEYBOARD_ID)]
        assert bt.opened[0].closed

    def test_falls_back_to_the_bluetooth_address(self, bt):
        bt.connect(device=FakeBluetoothDevice(features={0x1814: 0x0A}))

        [(_node, device_id)] = direct.scan(
            find_nodes=bt.find_nodes, open_transport=bt.open_transport
        )

        assert device_id == "D482C597174D"

    def test_identifies_each_node_only_once(self, bt):
        bt.connect()
        for _ in range(3):
            direct.scan(find_nodes=bt.find_nodes, open_transport=bt.open_transport)

        assert len(bt.opened) == 1

    def test_reidentifies_a_reused_hidraw_number(self, bt):
        bt.connect()
        direct.scan(find_nodes=bt.find_nodes, open_transport=bt.open_transport)
        bt.disconnect()
        direct.scan(find_nodes=bt.find_nodes, open_transport=bt.open_transport)
        bt.connect(
            device=FakeBluetoothDevice(unit_id=bytes.fromhex("DBEE1602")),
            address="d7:be:0d:b3:3a:9c",
        )

        [(_node, device_id)] = direct.scan(
            find_nodes=bt.find_nodes, open_transport=bt.open_transport
        )

        assert device_id == "DBEE1602"

    def test_skips_a_node_it_cannot_open_and_says_why_once(self, bt, caplog):
        bt.connect()
        bt.unopenable.add("/dev/hidraw1")

        with caplog.at_level(logging.WARNING):
            for _ in range(2):
                assert (
                    direct.scan(
                        find_nodes=bt.find_nodes, open_transport=bt.open_transport
                    )
                    == []
                )

        assert len([r for r in caplog.records if "permission" in r.message]) == 1


class TestDirectDevice:
    def test_is_unreachable_until_its_node_appears(self, bt):
        device = bt.direct_device()

        assert device.refresh() is False
        with pytest.raises(ProtocolError) as error:
            device.get_change_host_info(0xFF)
        assert error.value.means_unreachable
        assert device.ping_device(0xFF) is None

    def test_reads_change_host_info_once_connected(self, bt):
        bt.connect()
        device = bt.direct_device()

        assert device.refresh() is True
        info = device.get_change_host_info(0xFF)

        assert info is not None
        assert (info.num_hosts, info.current_host) == (3, 2)
        assert device.ping_device(0xFF) == 4.5

    def test_change_device_host_switches_with_a_zero_indexed_host(self, bt):
        fake = bt.connect()
        device = bt.direct_device()
        device.refresh()

        util.change_device_host(device.paired_device, 1)

        assert fake.host_writes == [0]

    def test_change_device_host_reports_an_away_device_as_unreachable(self, bt):
        device = bt.direct_device()
        device.refresh()

        with pytest.raises(DeviceUnreachable):
            util.change_device_host(device.paired_device, 1)

    def test_closes_its_node_when_the_device_leaves(self, bt):
        bt.connect()
        device = bt.direct_device()
        device.refresh()
        io = bt.opened[-1]

        bt.disconnect()

        assert device.refresh() is False
        assert io.closed
        assert not device.connected

    def test_an_io_error_mid_request_means_the_device_left(self, bt):
        bt.connect()
        device = bt.direct_device()
        device.refresh()
        bt.opened[-1].fail_writes = True

        with pytest.raises(ProtocolError) as error:
            device.get_change_host_info(0xFF)

        assert error.value.means_unreachable
        assert not device.connected

    def test_keeps_one_paired_device_across_reconnects(self, bt):
        device = bt.direct_device()
        paired = device.paired_device
        bt.connect()
        device.refresh()
        bt.disconnect()
        device.refresh()
        bt.connect(path="/dev/hidraw3")
        device.refresh()

        assert device.connected
        assert device.paired_device is paired
        assert device.enumerate_devices() == [paired]
        assert device.get_device(0xFF) is paired
        assert device.get_device(1) is None

    def test_paired_device_names_the_device(self, bt):
        bt.connect()
        device = DirectDevice(KEYBOARD_ID, node=bt.nodes[0])

        paired = device.paired_device
        assert paired.id == KEYBOARD_ID
        assert paired.number == 0xFF
        assert paired.path == "bt:F83FB9E1"
        assert paired.wpid == "B378"
        assert paired.codename == "MX KEYS S"

    def test_can_be_named_by_its_bluetooth_address(self, bt):
        bt.connect()
        device = bt.direct_device("D482C597174D")

        assert device.refresh() is True


class TestPresenceWatcher:
    def _start(self, device):
        seen: list[bool] = []
        lock = threading.Lock()

        def callback(notification):
            assert notification.sub_id == 0x41
            assert notification.devnumber == 0xFF
            status = parse_connection_status(notification.data)
            with lock:
                seen.append(status["link_status"] == 0)

        watcher = PresenceWatcher(device, callback, interval=0.01)
        watcher.start()
        return watcher, seen

    def test_announces_the_initial_state_then_each_change(self, bt):
        device = bt.direct_device()
        watcher, seen = self._start(device)
        try:
            wait_for(lambda: seen == [False])
            bt.connect()
            wait_for(lambda: seen == [False, True])
            bt.disconnect()
            wait_for(lambda: seen == [False, True, False])
        finally:
            watcher.stop()
            watcher.join(timeout=5)
        assert not watcher.is_alive()

    def test_reannounces_on_request(self, bt):
        bt.connect()
        device = bt.direct_device()
        watcher, seen = self._start(device)
        try:
            wait_for(lambda: seen == [True])
            device.notify_devices()
            wait_for(lambda: seen == [True, True])
        finally:
            watcher.stop()
            watcher.join(timeout=5)

    def test_survives_a_failing_callback(self, bt):
        calls = []

        def callback(notification):
            calls.append(notification)
            raise RuntimeError("boom")

        device = bt.direct_device()
        watcher = PresenceWatcher(device, callback, interval=0.01)
        watcher.start()
        try:
            wait_for(lambda: len(calls) == 1)
            bt.connect()
            wait_for(lambda: len(calls) == 2)
        finally:
            watcher.stop()
            watcher.join(timeout=5)


class TestUtil:
    @pytest.fixture
    def env(self, bt, monkeypatch):
        monkeypatch.setattr(util, "find_receivers", lambda: [])
        monkeypatch.setattr(util, "find_bluetooth_devices", bt.find_nodes)
        monkeypatch.setattr(direct, "find_bluetooth_devices", bt.find_nodes)
        monkeypatch.setattr(direct, "open_hidraw", bt.open_transport)

        def scan():
            return direct.scan(
                find_nodes=bt.find_nodes, open_transport=bt.open_transport
            )

        monkeypatch.setattr(util, "scan_bluetooth_devices", scan)

        class FakeDirectDevice(DirectDevice):
            def __init__(self, device_id, *, node=None):
                super().__init__(
                    device_id,
                    node=node,
                    find_nodes=bt.find_nodes,
                    open_transport=bt.open_transport,
                )

        monkeypatch.setattr(util, "DirectDevice", FakeDirectDevice)
        return bt

    def test_get_devices_lists_connected_bluetooth_devices(self, env):
        env.connect()

        [paired] = [d for d in util.get_devices() if d is not None]

        assert paired.id == KEYBOARD_ID
        assert paired.path == "bt:F83FB9E1"
        assert util.get_theoretical_max_device_count() == 1

    def test_resolve_device_ids_waits_for_an_absent_device(self, env, caplog):
        with caplog.at_level(logging.WARNING):
            resolved = util.resolve_device_ids([KEYBOARD_ID])

        assert resolved.receivers == []
        [device] = resolved.direct_devices
        assert not device.connected
        assert resolved.devices == {KEYBOARD_ID: device.paired_device}
        assert any("waiting for it" in r.message for r in caplog.records)

    def test_resolve_device_ids_attaches_a_present_device(self, env):
        env.connect()

        resolved = util.resolve_device_ids([KEYBOARD_ID])

        [device] = resolved.direct_devices
        assert device.connected
        assert device.paired_device.codename == "MX KEYS S"

    def test_resolve_devices_finds_direct_devices_even_while_away(self, env):
        device = env.direct_device()

        assert util.resolve_devices([device], [KEYBOARD_ID]) == {
            KEYBOARD_ID: device.paired_device
        }

    def test_get_device_by_path_accepts_a_bluetooth_path(self, env):
        env.connect()

        device = util.get_device_by_path("bt:F83FB9E1")

        assert device.id == KEYBOARD_ID
        assert device.receiver.get_change_host_info(0xFF) is not None

    def test_get_device_by_path_rejects_an_absent_bluetooth_device(self, env):
        with pytest.raises(DeviceNotFound):
            util.get_device_by_path("bt:F83FB9E1")
