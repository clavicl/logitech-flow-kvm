# Porting flow-client to Windows

`flow-client` doesn't work on Windows yet. All device access is Linux-only:

- `src/logitech_flow_kvm/hidpp/discovery.py` scans `/sys/class/hidraw`.
- `src/logitech_flow_kvm/hidpp/transport.py` (`HidRawIO`) opens `/dev/hidraw*` and uses `select` on the file descriptor.
- `src/logitech_flow_kvm/hidpp/direct.py` (Bluetooth `DirectDevice` / `PresenceWatcher`) polls sysfs for the device's node.

On Windows the client starts and connects to the server, but it never sees the keyboard or mouse. So it can't report the leader arriving, and it can't push followers away. WSL doesn't help, because it can't see devices that belong to Windows.

This file describes how to do the port in a Claude Code session on the Windows machine, where it can be tested against the real devices.

## Setup

The current setup:

- **Server:** Raspberry Pi, Flow host 3, running `flow-server 3 F83FB9E1 DBEE1602`.
- **Devices:** both are connected over Bluetooth, with no receiver.
  - MX Keys S, ID `F83FB9E1`, product `B378`. This is the leader.
  - MX Master 3S, ID `DBEE1602`, product `B034`. This is the follower.
- **Repo:** `https://github.com/clavicl/logitech-flow-kvm`, branch `bluetooth-support`.

## 1. Prepare the Windows machine

Do everything natively in **PowerShell**, not WSL.

```powershell
winget install Python.Python.3.12 Git.Git
```

Close and reopen PowerShell so `python` and `git` are on PATH. Then:

```powershell
git clone https://github.com/clavicl/logitech-flow-kvm.git
cd logitech-flow-kvm
git switch bluetooth-support
```

Install Claude Code, either the Claude desktop app (Code tab) or the CLI. Then open a session in the `logitech-flow-kvm` folder.

Before starting, connect both devices to the Windows machine on its Easy-Switch channel.

## 2. Prompt for the Claude Code session

Paste this into the session:

> This is logitech-flow-kvm with Bluetooth support added on the `bluetooth-support` branch. Read `windows-change.md` for background. Its device access is Linux-only: `hidpp/discovery.py` and `hidpp/transport.py` use `/sys/class/hidraw` and `/dev/hidraw*`, and `hidpp/direct.py` polls sysfs for Bluetooth presence.
>
> Add a Windows backend, probably via hidapi (the `hidapi` / `hid` PyPI package), so `flow-client` works natively on Windows. It needs three pieces:
> - discovery of Logitech receivers and Bluetooth HID++ devices (vendor 0x046D; HID++ collections are usage page 0xFF00 on receivers, 0xFF43 on Bluetooth devices);
> - a transport implementing `protocol.Transport` (write / read / drain / close);
> - a presence signal for Bluetooth devices, so `PresenceWatcher` can tell when a device Easy-Switches away.
>
> Keep Linux behaviour unchanged, and pick the backend by platform.
>
> My devices are an MX Keys S (ID F83FB9E1) and an MX Master 3S (ID DBEE1602), both over Bluetooth. Over Bluetooth they speak HID++ as device number 0xFF and only accept long (0x11) reports. A device's ID is the unitId from HID++ feature 0x0003, function 0 (`reply[1:5]`, hex upper).
>
> Before building anything, verify on this machine:
> 1. the HID++ interface of each device can be found and opened;
> 2. reading feature 0x0003 returns the IDs above;
> 3. what happens to that interface when a device Easy-Switches away. Does it disappear from enumeration, or stay listed but stop answering? Stop and tell me if presence can't be detected.
>
> Then build it and test `logitech-flow-kvm list-devices` and `logitech-flow-kvm switch-to-host bt:<ID> <host>` here. Add tests in the style of `tests/` (fakes in `tests/hidpp_fakes.py`). Keep `ruff check`, `ruff format --check`, `mypy src tests`, and all existing tests passing. Don't commit until I've reviewed it.

## 3. Things to check on Windows

- **Presence detection.** On Linux, the device's node disappears when it switches away, and that is the presence signal. Windows may keep a Bluetooth device listed while it's disconnected, so this needs testing first.
- **Exclusive access.** Windows may refuse to open keyboard and mouse top-level collections, while the vendor-defined HID++ collection should be openable. Confirm this.
- **Logitech Options+.** If it's installed, it may already be holding the HID++ interface open or sending its own Flow commands. Test with it closed.
- **Clipboard sync.** `pyperclip` should work natively on Windows, but check it.
- **End to end.**
  1. Start `flow-server` on the Pi.
  2. Run `logitech-flow-kvm flow-client <windows-host-number> <pi-address>` on Windows and pair it. The pairing code is typed into the Pi's server console.
  3. Switch the keyboard between hosts and confirm the mouse follows in both directions.

## 4. Keeping the machines in sync

When the Windows work is done, commit and push from Windows:

```powershell
git push
```

Then on the Pi:

```bash
git pull
```

If the Pi's install was made with `pipx install --editable`, it picks up the changes right away. Otherwise reinstall with `pipx install --force <path-to-clone>`.
