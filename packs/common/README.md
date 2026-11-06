# Solakit pack support

Separate `solakit-packs-common` distribution. Runtime modules do not import
pytest; `sola_packs.support.testing` is optional test support (`[test]` extra).

Provides pack-owned resource loading from `SOLA_PACKS_CONFIG`, strict per-resource
schemas, explicit resource and effect checks, and env/mode-0600 secret references. Relative
secret-file paths resolve beside the sidecar. Resolved credentials register with
the existing Store redactor before network access, so runtime logs and UI masking
continue to cover credentials the core did not know at startup.

External source strings are JSON-encoded inside explicit external-content
markers, escaping angle brackets so source text cannot close the marker.

Operation receipts use private append-only JSONL files, request hashes, bounded
reads, nonblocking cross-process locks and fsynced intent/results. Pending,
uncertain or torn evidence is not silently replayed or repaired. These records
complement the core's existing verbatim tool-call and result evidence.

Bounded POSIX process execution now lives in core `solakit.services.process` for
the built-in shell tools. Common pack support does not provide a shell dependency.
