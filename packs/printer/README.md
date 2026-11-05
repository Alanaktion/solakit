# Thermal printer pack

Independent `solakit-packs-printer` distribution using `python-escpos` for a
58mm receipt printer connected by native USB or USB-to-serial. No physical cutter
is assumed: each print ends with two configurable feed lines and sends no cut
command. Device selection is operator-owned configuration, never model input.

```sh
uv pip install -e . -e packs/common -e packs/printer
```

Native USB needs a working libusb backend on the host. USB-to-serial uses pyserial
and the device's actual `/dev/cu.*` or `/dev/tty*` path and baud rate. The drivers
are distinct; choose the one matching how the printer enumerates. Consult the
[python-escpos driver documentation](https://python-escpos.readthedocs.io/en/latest/user/printers.html).

In core configuration:

```toml
enabled_tools = ["sola_packs.printer"]

[profiles.interactive]
tools = ["printer.print_text"]
capabilities = ["printer.print", "printer:desk:print"]
```

In the pack-owned `SOLA_PACKS_CONFIG` TOML sidecar:

```toml
[printers.desk]
transport = "usb"
vendor_id = 0x1234 # Replace with actual device IDs.
product_id = 0x5678
profile = "simple" # Prefer the exact supported python-escpos profile if known.
columns = 32 # Typical 58mm font A width; confirm with physical output.
feed_lines = 2
max_text_bytes = 16000
timeout_s = 10
```

For serial, replace transport and USB IDs with:

```toml
transport = "serial"
serial_device = "/dev/cu.usbserial-EXAMPLE"
baudrate = 9600
```

Call `printer.print_text(printer="desk", text="A note", operation_id="note-001")`.
Text is bounded in UTF-8 bytes before ESC/POS encoding. Control characters are
rejected except newline/tab. The configured profile controls code-page encoding;
unsupported glyphs may become replacement characters. This is text printing,
not image rasterization or a promise of arbitrary Unicode rendering.

Private append-only receipts under `pack-operations/printer/` bind an operation ID
to exact text/device settings. A completed repeat returns the stored receipt;
changed requests, pending intents and uncertain sends fail closed. Device locks
prevent concurrent print calls interleaving. Transport success means bytes were
submitted, not proof that paper emerged. Timeouts bound individual device writes;
a running worker finishes receipt bookkeeping on cancellation. Never remove a
receipt to force a retry; inspect the device first.

Capture tests use python-escpos's Dummy transport and fake failing devices; they
do not access hardware. To validate deliberately, identify/configure the actual
device, print one short ASCII receipt, inspect wrapping/feed and repeat the same
operation ID to confirm no duplicate. Physical validation remains a separate gate.
