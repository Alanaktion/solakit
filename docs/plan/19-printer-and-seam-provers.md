# Printer pack and later seam proofs

Status: planned, printer after email.

Printer is a thin separate pack over a named device/transport with an explicit
print capability, bounded text, and visible send/error status. Test with a capture
transport before a deliberate physical-device check. Do not hardcode Sola's
printer or its connection details in the core.

Later, independently prioritized: ComfyUI/ZImage image pack and calendar read.
They prove the extension seam, not a new provider framework. A tool-specific
change to `core/loop.py` is a review finding. Pack-owned configuration and generic
files/repos/memory/web/messages/device services should carry these integrations.
