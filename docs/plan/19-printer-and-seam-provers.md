# Printer pack and later seam proofs

Status: USB/serial 58mm ESC/POS thermal printer pack implemented; physical check pending; calendar read implemented; image workflow remains.

- [x] Build a separately packaged printer module with pack-owned named-device config.
- [x] Enforce explicit print capability and bounded output; report send/error state.
- [x] Verify wrapping/feed with python-escpos Dummy and replay/grant/error capture tests.
- [ ] Deliberate physical-device check on the connected 58mm printer.
- [x] Read-only calendar pack over named ICS exports with scoped, bounded reads.
- [ ] Image-workflow pack.

The [printer pack](../../packs/printer/README.md) uses python-escpos over named USB/serial devices,
32-column wrapping and a two-line feed without a cut command. Private durable receipts
block uncertain replay; transport success does not prove paper output. Printer is a
thin separate pack over a named device/transport with an explicit
print capability, bounded text, and visible send/error status. Test with a capture
transport before a deliberate physical-device check. Do not hardcode Sola's
printer or its connection details in the core.

Later, independently prioritized: ComfyUI/ZImage image pack and calendar read.
They prove the extension seam, not a new provider framework. A tool-specific
change to `core/loop.py` is a review finding. Pack-owned configuration and generic
files/repos/memory/web/messages/device services should carry these integrations.

## Verification (2026-11-04)

Core and all optional-pack suites: 221 passed. Ruff lint/format and core, common
and printer wheel/sdist builds passed. Capture checks include actual python-escpos
Dummy bytes (wrap/feed, no cutter command), grants, byte/control limits, durable
replay refusal, device locking and bounded serial/USB writes.

Read-only local `ioreg` discovery identified a TECH CLA58 native USB printer,
vendor `0x6868`, product `0x0200`; these IDs were added only to the ignored local
pack sidecar. Discovery is not a print test. No physical output or rollout gate
is claimed. Offline `sola check` could not complete without the operator secret
in this shell.
