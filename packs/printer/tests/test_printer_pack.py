import asyncio
import json

import pytest
from sola_packs import printer


@pytest.fixture
def fixture(pack_context, tmp_path, monkeypatch):
    settings = tmp_path / "packs.toml"
    settings.write_text(
        """[printers.desk]
transport = "usb"
vendor_id = 4660
product_id = 22136
max_text_bytes = 16
"""
    )
    monkeypatch.setenv("SOLA_PACKS_CONFIG", str(settings))
    registry, ctx = pack_context(
        ["sola_packs.printer"],
        ["printer.print_text"],
        capabilities=["printer.print", "printer:desk:print"],
    )
    captured = []

    class Capture:
        def textln(self, text):
            captured.append(text)

        def ln(self, lines):
            assert lines == 2

        def close(self):
            pass

    monkeypatch.setattr(printer, "connect", lambda cfg: Capture())
    return registry, ctx, captured


def call(fixture, **changes):
    registry, ctx, _ = fixture
    arguments = dict(printer="desk", text="Hello 🌙\n", operation_id="print-1")
    arguments.update(changes)
    return asyncio.run(
        registry.dispatch("printer.print_text", json.dumps(arguments), ctx)
    )


def test_capture_and_durable_receipt(fixture):
    result = call(fixture)
    assert not result.error, result.content
    assert json.loads(result.content)["status"] == "sent_to_transport"
    assert call(fixture).content == result.content
    assert fixture[2] == ["Hello 🌙"]
    assert call(fixture, text="changed").error
    receipts = list(fixture[1].store.root.glob("pack-operations/printer/*.jsonl"))
    assert [
        json.loads(line)["stage"] for line in receipts[0].read_text().splitlines()
    ] == ["prepared", "sent"]


@pytest.mark.parametrize("text", ["", "🌙" * 5, "bad\x1bE", "bad\x00", "bad\r"])
def test_reject_before_transport(fixture, text):
    assert call(fixture, text=text).error
    assert fixture[2] == []


@pytest.mark.parametrize("capability", ["printer.print", "printer:desk:print"])
def test_explicit_grants(fixture, capability):
    fixture[1].config.profiles["interactive"].capabilities.remove(capability)
    assert call(fixture).error
    assert fixture[2] == []


def test_uncertain_partial_send_never_replayed(fixture, monkeypatch):
    def fail(*args, **kwargs):
        fixture[2].append(b"partial")
        raise TimeoutError("capture timeout")

    monkeypatch.setattr(printer, "connect", fail)
    assert "uncertain" in call(fixture).content
    assert "uncertain" in call(fixture).content
    assert fixture[2] == [b"partial"]


def test_real_escpos_dummy_wraps_and_feeds_without_cut(fixture, monkeypatch):
    from escpos.printer import Dummy

    device = Dummy(profile="simple")
    monkeypatch.setattr(printer, "connect", lambda cfg: device)
    text = "1234567890" * 4
    settings = printer.Printer(transport="usb", vendor_id=4660, product_id=22136)
    result = printer.send(fixture[1], settings, "desk", text, "dummy")
    assert result["status"] == "sent_to_transport"
    assert (
        device.output
        == b"\x1bt\x00" + (text[:32] + "\n" + text[32:] + "\n\n\n").encode()
    )


def test_serial_write_timeout_short_write_and_close(monkeypatch):
    class Port:
        write_timeout = None
        closed = False

        def write(self, data):
            return len(data) - 1

        def close(self):
            self.closed = True

    port = Port()

    def open_port(self, **kwargs):
        self._device = port

    monkeypatch.setattr(printer.Serial, "open", open_port)
    device = printer.BoundedSerial(devfile="/dev/cu.fixture", timeout=2)
    device.open()
    assert port.write_timeout == 2
    with pytest.raises(OSError, match="Incomplete"):
        device._raw(b"abc")
    device.close()
    assert port.closed


def test_serial_drain_timeout_is_bounded(monkeypatch):
    class Port:
        out_waiting = 1

    device = printer.BoundedSerial(devfile="/dev/cu.fixture", timeout=1)
    device._device = Port()
    ticks = iter([0, 2])
    monkeypatch.setattr(printer.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="did not drain"):
        device.drain()
    device._device = False


def test_device_lock_rejects_other_operation(fixture):
    from sola_packs.support.evidence import Lease

    with Lease(fixture[1], "printer-devices", ["usb", 4660, 22136]):
        result = call(fixture, operation_id="other")
    assert result.error and "already in progress" in result.content
    assert fixture[2] == []


def test_usb_timeout_and_short_write(monkeypatch):
    device = printer.connect(
        printer.Printer(
            transport="usb", vendor_id=4660, product_id=22136, timeout_s=0.0001
        )
    )
    assert device.timeout == 1

    class Port:
        def write(self, endpoint, data, timeout):
            return len(data) - 1

    device._device = Port()
    with pytest.raises(OSError, match="Incomplete USB"):
        device._raw(b"abc")
    device._device = False
