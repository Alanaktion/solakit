# CLI diagnostics verification

On 2026-11-23, the full doctor command passed against the local oMLX text pin
`Qwen3.6-35B-A3B-AntiLoop-NVFP4`. It streamed a terminal answer, checked an
integer lookup argument and the use of a fresh tool result, observed a separate
reasoning field, and recovered a code from two identical longer requests. In
one run those requests took 1.92 s and 0.36 s. The timings describe that pair
of requests only.

The vision endpoint listed `Qwen3-VL-8B-Instruct-4bit` and OpenViking reported
version 0.5.5. These were read-only availability checks. Pixel recognition and
memory extraction have separate live probes.

The CLI regression tests use a fake HTTP transport and disposable data. They
exercise typed argument rejection, continuation with the provider call ID,
pin mismatch, independent service results, disabled services, exit status,
and inherited profile changes. They also check that the operator data directory
stays absent, identity text stays out of requests, and private reasoning stays
out of the report.

All 710 deterministic tests, the locked Ruff checks, strict mypy and the clean
wheel smoke passed.
