# Printer pack and later seam proofs

Status: not implemented — printer next; image/calendar remain later priorities.

- [ ] Build a separately packaged printer module with pack-owned named-device config.
- [ ] Enforce explicit print capability and bounded output; report send/error state.
- [ ] Verify with a capture transport, then a deliberate physical-device check.
- [ ] Later, only when prioritized: image-workflow pack and calendar-read pack.

Printer is a thin separate pack over a named device/transport with an explicit
print capability, bounded text, and visible send/error status. Test with a capture
transport before a deliberate physical-device check. Do not hardcode Sola's
printer or its connection details in the core.

Later, independently prioritized: ComfyUI/ZImage image pack and calendar read.
They prove the extension seam, not a new provider framework. A tool-specific
change to `core/loop.py` is a review finding. Pack-owned configuration and generic
files/repos/memory/web/messages/device services should carry these integrations.
