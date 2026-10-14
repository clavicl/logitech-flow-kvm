"""The hidapi backend used on Windows, against a fake `hid` module."""

import sys

import pytest

from hidpp_fakes import FakeHid
from logitech_flow_kvm.hidpp import direct
from logitech_flow_kvm.hidpp import discovery
from logitech_flow_kvm.hidpp import transport
from logitech_flow_kvm.hidpp.models import BluetoothNodeInfo
from logitech_flow_kvm.hidpp.models import ReceiverInfo
from logitech_flow_kvm.hidpp.transport import HidApiIO
from logitech_flow_kvm.hidpp.transport import HidRawIO

BT_HOGP = "{00001812-0000-1000-8000-00805f9b34fb}"
HID_GUID = "{4d1e55b2-f16f-11cf-88cb-001111000030}"


def bt_path(product: str, address: str, col: int) -> bytes:
    return (
        f"\\\\?\\HID#{BT_HOGP}_Dev_VID&02046d_PID&{product}_REV&0015_{address}"
        f"&Col{col:02d}#9&22119063&0&{col - 1:04d}#{HID_GUID}"
    ).encode()


def usb_path(product: str, interface: int, col: int, instance: str) -> bytes:
    return (
        f"\\\\?\\HID#VID_046D&PID_{product}&MI_{interface:02d}&Col{col:02d}"
        f"#{instance}&0&{col - 1:04d}#{HID_GUID}"
    ).encode()


KEYBOARD_HIDPP = bt_path("b378", "d482c597174c", 5)
KEYBOARD_KBD = bt_path("b378", "d482c597174c", 1)
MOUSE_HIDPP = bt_path("b034", "d7be0db33a9c", 3)


@pytest.fixture
def hid(monkeypatch):
    fake = FakeHid()
    monkeypatch.setitem(sys.modules, "hid", fake)
    monkeypatch.setattr(transport, "USE_HIDAPI", True)
    return fake


def add_keyboard(hid: FakeHid) -> None:
    common = dict(
        product_id=0xB378,
        product_string="MX_KEYS_S",
        serial_number="d482c597174c",
        interface_number=-1,
        bus_type=2,
    )
    hid.add(KEYBOARD_KBD, usage_page=0x0001, usage=0x0006, **common)
    hid.add(KEYBOARD_HIDPP, usage_page=0xFF43, usage=0x0202, **common)


def add_receiver(hid: FakeHid, instance: str = "7&2a3b4c5d") -> tuple[bytes, bytes]:
    common = dict(product_id=0xC548, bus_type=1, product_string="USB Receiver")
    hid.add(
        usb_path("C548", 0, 1, instance),
        usage_page=0x0001,
        usage=0x0006,
        interface_number=0,
        **common,
    )
    short = hid.add(
        usb_path("C548", 2, 1, instance),
        usage_page=0xFF00,
        usage=0x0001,
        interface_number=2,
        **common,
    )
    long = hid.add(
        usb_path("C548", 2, 2, instance),
        usage_page=0xFF00,
        usage=0x0002,
        interface_number=2,
        **common,
    )
    return short, long


class TestFindBluetoothDevices:
    def test_finds_the_hidpp_collection_of_a_bluetooth_device(self, hid):
        add_keyboard(hid)

        assert discovery.find_bluetooth_devices() == [
            BluetoothNodeInfo(
                path=KEYBOARD_HIDPP.decode(),
                product_id=0xB378,
                name="MX_KEYS_S",
                address="d482c597174c",
            )
        ]

    def test_its_address_matches_the_linux_form(self, hid):
        add_keyboard(hid)

        [node] = discovery.find_bluetooth_devices()

        assert direct.normalize_address(node.address) == direct.normalize_address(
            "d4:82:c5:97:17:4c"
        )

    def test_ignores_a_collection_on_another_bus(self, hid):
        hid.add(
            KEYBOARD_HIDPP, product_id=0xB378, usage_page=0xFF43, usage=1, bus_type=1
        )

        assert discovery.find_bluetooth_devices() == []

    def test_trusts_the_usage_page_when_the_bus_is_unknown(self, hid):
        hid.add(KEYBOARD_HIDPP, product_id=0xB378, usage_page=0xFF43, usage=1)

        assert [n.path for n in discovery.find_bluetooth_devices()] == [
            KEYBOARD_HIDPP.decode()
        ]

    def test_a_device_that_switched_away_is_not_listed(self, hid):
        add_keyboard(hid)
        hid.add(MOUSE_HIDPP, product_id=0xB034, usage_page=0xFF43, bus_type=2)
        del hid.collections[KEYBOARD_HIDPP]

        assert [n.product_id for n in discovery.find_bluetooth_devices()] == [0xB034]

    def test_receivers_are_not_bluetooth_devices(self, hid):
        add_receiver(hid)

        assert discovery.find_bluetooth_devices() == []


class TestFindReceivers:
    def test_finds_the_long_hidpp_collection(self, hid):
        _short, long = add_receiver(hid)

        assert discovery.find_receivers() == [
            ReceiverInfo(
                path=long.decode(), product_id=0xC548, kind="bolt", interface=2
            )
        ]

    def test_ignores_unknown_products(self, hid):
        hid.add(
            usb_path("4099", 2, 2, "7&1"),
            product_id=0x4099,
            usage_page=0xFF00,
            usage=0x0002,
            interface_number=2,
        )

        assert discovery.find_receivers() == []

    def test_bluetooth_devices_are_not_receivers(self, hid):
        add_keyboard(hid)

        assert discovery.find_receivers() == []


class TestHidApiIO:
    def test_long_only_sends_every_report_long(self, hid):
        add_keyboard(hid)

        with HidApiIO(KEYBOARD_HIDPP.decode(), long_only=True) as io:
            io.write(0xFF, b"\x00\x18\x00\x00\x5a", long_message=False)

        [handle] = hid.handles
        [report] = handle.written
        assert report[:2] == b"\x11\xff"
        assert len(report) == 20
        assert handle.closed

    def test_reads_a_report(self, hid):
        add_keyboard(hid)
        reply = bytes.fromhex("11ff020a03f83fb9e10002b37800000000000100")
        hid.input[KEYBOARD_HIDPP] = [reply]

        with HidApiIO(KEYBOARD_HIDPP.decode(), long_only=True) as io:
            assert io.read(0.1) == (0x11, 0xFF, reply[2:])
            assert io.read(0) is None

    def test_drain_discards_waiting_reports(self, hid):
        add_keyboard(hid)
        hid.input[KEYBOARD_HIDPP] = [b"\x11\xff\x00", b"\x11\xff\x01"]

        with HidApiIO(KEYBOARD_HIDPP.decode(), long_only=True) as io:
            io.drain()
            assert io.read(0) is None

    def test_a_read_fails_once_the_device_switches_away(self, hid):
        add_keyboard(hid)

        with HidApiIO(KEYBOARD_HIDPP.decode(), long_only=True) as io:
            hid.gone.add(KEYBOARD_HIDPP)
            with pytest.raises(OSError):
                io.read(0.1)
            with pytest.raises(OSError):
                io.write(0xFF, b"\x00\x18", long_message=False)

    def test_cannot_open_an_absent_collection(self, hid):
        with pytest.raises(OSError):
            HidApiIO(KEYBOARD_HIDPP.decode(), long_only=True)

    def test_a_receiver_sends_short_reports_to_its_short_collection(self, hid):
        short, long = add_receiver(hid)
        # A second receiver of the same model, whose collections must not be
        # mixed up with the first one's.
        add_receiver(hid, instance="7&99999999")

        with HidApiIO(long.decode()) as io:
            io.write(0xFF, b"\x81\x02\x00", long_message=False)
            io.write(0x01, b"\x0a\x1b\x00", long_message=True)

        by_path = {h.path: h for h in hid.handles}
        assert set(by_path) == {short, long}
        assert [r[:2] for r in by_path[short].written] == [b"\x10\xff"]
        assert [len(r) for r in by_path[short].written] == [7]
        assert [r[:2] for r in by_path[long].written] == [b"\x11\x01"]

    def test_a_receiver_reads_from_both_collections(self, hid):
        short, long = add_receiver(hid)
        hid.input[short] = [b"\x10\x01\x41\x04\x61\x10\x00"]
        hid.input[long] = [b"\x11\xff\x81\x02" + bytes(16)]

        with HidApiIO(long.decode()) as io:
            reports = [io.read(0.1), io.read(0.1), io.read(0)]

        assert sorted(r[0] for r in reports if r is not None) == [0x10, 0x11]
        assert reports[2] is None

    def test_a_receiver_without_a_short_collection_sends_long(self, hid):
        _short, long = add_receiver(hid)
        del hid.collections[_short]

        with HidApiIO(long.decode()) as io:
            io.write(0xFF, b"\x81\x02\x00", long_message=False)

        [handle] = hid.handles
        assert handle.written[0][:1] == b"\x11"

    def test_closes_the_long_collection_if_the_short_one_wont_open(self, hid):
        short, long = add_receiver(hid)
        hid.unopenable.add(short)

        with pytest.raises(OSError):
            HidApiIO(long.decode())

        [handle] = hid.handles
        assert handle.closed


class TestCollectionParent:
    def test_collections_of_one_interface_share_a_parent(self):
        assert transport._collection_parent(
            usb_path("C548", 2, 1, "7&2a").decode()
        ) == transport._collection_parent(usb_path("C548", 2, 2, "7&2a").decode())

    def test_other_interfaces_and_instances_differ(self):
        parent = transport._collection_parent(usb_path("C548", 2, 2, "7&2a").decode())

        assert parent != transport._collection_parent(
            usb_path("C548", 0, 2, "7&2a").decode()
        )
        assert parent != transport._collection_parent(
            usb_path("C548", 2, 2, "7&3b").decode()
        )


class TestOpenDevice:
    def test_uses_hidapi_when_asked(self, hid):
        add_keyboard(hid)

        io = transport.open_device(KEYBOARD_HIDPP.decode(), long_only=True)

        assert isinstance(io, HidApiIO)
        assert io.long_only
        io.close()

    def test_uses_hidraw_otherwise(self, monkeypatch, tmp_path):
        monkeypatch.setattr(transport, "USE_HIDAPI", False)
        node = tmp_path / "hidraw0"
        node.write_bytes(b"")

        io = transport.open_device(str(node))

        assert isinstance(io, HidRawIO)
        io.close()


class TestDirectDeviceOverHidapi:
    """`DirectDevice` and `PresenceWatcher` unchanged, on top of hidapi."""

    @pytest.fixture(autouse=True)
    def clear_id_cache(self):
        direct._ids.clear()
        yield
        direct._ids.clear()

    def _answer_device_information(self, hid: FakeHid) -> None:
        def respond(report: bytes) -> bytes:
            header = report[2:4]
            if report[2] == 0x00:  # root: feature 0x0003 is at index 2
                reply = header + b"\x02"
            else:  # device information
                reply = header + bytes.fromhex("03f83fb9e10002b378")
            return report[:2] + reply.ljust(18, b"\x00")

        hid.respond = respond

    def test_identifies_a_device_by_its_unit_id(self, hid):
        add_keyboard(hid)
        self._answer_device_information(hid)

        [(node, device_id)] = direct.scan()

        assert node.path == KEYBOARD_HIDPP.decode()
        assert device_id == "F83FB9E1"

    def test_notices_the_device_leaving_and_returning(self, hid):
        add_keyboard(hid)
        self._answer_device_information(hid)
        device = direct.DirectDevice("F83FB9E1")

        assert device.refresh() is True
        info = hid.collections.pop(KEYBOARD_HIDPP)
        hid.gone.add(KEYBOARD_HIDPP)
        assert device.refresh() is False
        hid.collections[KEYBOARD_HIDPP] = info
        hid.gone.clear()
        assert device.refresh() is True
        device.close()
