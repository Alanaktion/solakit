"""Bounded ESC/POS text for named 58mm USB or serial thermal printers."""

import json
import textwrap
import time
from typing import Literal

from escpos.printer import Serial, Usb
from pydantic import Field, model_validator
from sola_packs.support import IDENTIFIER, Settings, grant, resource
from sola_packs.support.evidence import Lease, Operation

from solakit.core.io import durable
from solakit.tools import ToolContext, ToolError, ToolResult, tool


class Printer(Settings):
    transport: Literal["usb", "serial"]
    vendor_id: int | None = Field(default=None, ge=1, le=65535)
    product_id: int | None = Field(default=None, ge=1, le=65535)
    serial_device: str | None = None
    baudrate: int = Field(default=9600, ge=300, le=4000000)
    profile: str = "simple"
    columns: int = Field(default=32, ge=16, le=48)
    feed_lines: int = Field(default=2, ge=1, le=6)
    timeout_s: float = Field(default=10, gt=0, le=60)
    max_text_bytes: int = Field(default=16000, ge=1, le=100000)

    @model_validator(mode="after")
    def valid(self):
        if self.transport == "usb":
            if self.vendor_id is None or self.product_id is None or self.serial_device:
                raise ValueError("USB needs vendor_id/product_id and no serial_device")
        elif (
            not self.serial_device
            or not self.serial_device.startswith("/dev/")
            or self.vendor_id is not None
            or self.product_id is not None
        ):
            raise ValueError("Serial needs a /dev device and no USB IDs")
        return self


class BoundedUsb(Usb):
    def _raw(self, msg):
        if self.device.write(self.out_ep, msg, self.timeout) != len(msg):
            raise OSError("Incomplete USB write")


class BoundedSerial(Serial):
    """python-escpos 3.x does not propagate a serial write timeout."""

    def open(self, raise_not_found=True):
        super().open(raise_not_found=raise_not_found)
        if self._device:
            self._device.write_timeout = self.timeout

    def _raw(self, msg):
        if self.device.write(msg) != len(msg):
            raise OSError("Incomplete serial write")

    def drain(self):
        deadline = time.monotonic() + self.timeout
        while self.device.out_waiting:
            if time.monotonic() >= deadline:
                raise TimeoutError("Serial output did not drain")
            time.sleep(0.01)

    def close(self):
        # Serial.close() in python-escpos flushes without a timeout. Closing the
        # port directly avoids an unbounded drain on a disconnected device.
        if self._device:
            self._device.close()
        self._device = False


def connect(cfg):
    if cfg.transport == "usb":
        return BoundedUsb(
            cfg.vendor_id,
            cfg.product_id,
            timeout=max(1, int(cfg.timeout_s * 1000)),
            profile=cfg.profile,
        )
    return BoundedSerial(
        devfile=cfg.serial_device,
        baudrate=cfg.baudrate,
        timeout=cfg.timeout_s,
        dsrdtr=False,
        profile=cfg.profile,
    )


def send(ctx, cfg, printer, text, operation_id):
    request = {"printer": printer, "device": cfg.model_dump(), "text": text}
    identity = (
        ["usb", cfg.vendor_id, cfg.product_id]
        if cfg.transport == "usb"
        else ["serial", cfg.serial_device]
    )
    with (
        Lease(ctx, "printer-devices", identity),
        Operation(ctx, "printer", [printer, operation_id], request) as operation,
    ):
        previous = operation.current
        if previous:
            if previous["stage"] == "sent":
                return previous["result"]
            raise ToolError(
                "Print is pending or uncertain; "
                "inspect receipt and device before retrying"
            )
        operation.record("prepared")
        device = None
        try:
            device = connect(cfg)
            wrapper = textwrap.TextWrapper(
                width=cfg.columns, replace_whitespace=False, drop_whitespace=False
            )
            wrapped = "\n".join(wrapper.fill(line) for line in text.split("\n"))
            device.textln(wrapped.rstrip("\n"))
            # This device has no physical cutter: explicitly feed, never cut.
            device.ln(cfg.feed_lines)
            if isinstance(device, BoundedSerial):
                device.drain()
            device.close()
            device = None
        except Exception as exc:
            operation.record("uncertain")
            raise ToolError(
                "Print uncertain; inspect receipt and device before retrying"
            ) from exc
        finally:
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass
        result = {
            "status": "sent_to_transport",
            "printer": printer,
            "operation_id": operation_id,
            "bytes": len(text.encode("utf-8")),
        }
        operation.record("sent", result=result)
        return result


@tool(name="printer.print_text", profiles=["*"], writes=["printer.print"])
async def print_text(
    ctx: ToolContext, printer: str, text: str, operation_id: str
) -> ToolResult:
    """Print thermal receipt text with a stable operation ID to prevent duplicates."""
    grant(ctx, "printer.print")
    cfg = resource(ctx, "printers", printer, Printer, "print")
    if not IDENTIFIER.fullmatch(operation_id):
        raise ToolError("A stable operation ID is required")
    if not text.strip() or len(text.encode("utf-8")) > cfg.max_text_bytes:
        raise ToolError("Print text is empty or exceeds the device byte limit")
    if any((ord(c) < 32 and c not in "\n\t") or 127 <= ord(c) < 160 for c in text):
        raise ToolError("Printer control characters are forbidden")
    return ToolResult(
        content=json.dumps(await durable(send, ctx, cfg, printer, text, operation_id))
    )
