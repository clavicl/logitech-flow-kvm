from logitech_flow_kvm import tray


def test_the_null_stream_is_not_a_tty():
    # The client picks its TUI on `sys.stdout.isatty()`; `os.devnull` would
    # answer True on Windows.
    stream = tray._NullStream()

    assert stream.isatty() is False
    assert stream.write("anything") == len("anything")
