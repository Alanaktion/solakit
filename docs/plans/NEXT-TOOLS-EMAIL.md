# Email tools

Support one operator-configured account and inbox. Sola can find messages, read
them without changing unread state, and send plain-text email. Use the
[shared tool plan](README.md#follow-up-tool-plans).

## Tool calls

| Tool | Arguments | Result |
| --- | --- | --- |
| `email.search` | `query=""`, `unread_only=false` | Up to 10 recent matching headers, newest first, with opaque `id`, sender, subject, date, unread state and `has_more`. |
| `email.read` | `id` | Headers and a bounded plain-text body, with a truncation flag. |
| `email.reply` | `id`, `body` | Sends a threaded reply to the original sender or Reply-To. Returns SMTP acceptance and the outgoing Message-ID. |
| `email.send` | `to`, `subject`, `body` | Sends a new plain-text message to one address. Returns SMTP acceptance and the outgoing Message-ID. |

Tell the model to copy `id` from search and search again if it is stale. An empty
search finds recent mail. For this slice, keep the inbox unchanged and omit
attachments, CC/BCC, custom headers, folder selection and mailbox discovery.

## Implementation

- `[email]` selects one IMAP inbox and sender, optional SMTP, and credential
  environment references. Configure an exact recipient allowlist for SMTP.
  Apply it to replies as well as new messages. Expose send/reply only when SMTP
  and the recipient list are configured, and only on interactive runs.
- Use verified TLS and fixed configured endpoints. Select the inbox read-only,
  quote search text as literal data, use `BODY.PEEK`, and scope the opaque ID to
  UIDVALIDITY plus UID. Reject stale IDs before using them. Bound header and
  body reads before MIME decoding. Prefer the `text/plain` part and report when
  there is no readable plain-text part. Ignore attachment bodies.
- Build reply recipient, thread headers, and subject from the fetched source.
  Reject ambiguous recipients, missing thread IDs, and header control
  characters. Construct the message with the standard email library. Keep
  the adapter focused on this provider path. If using blocking protocol clients,
  run them off the event loop with socket and total deadlines. On cancellation,
  close the active transport and settle the owned operation before returning.
- Persist an outgoing Message-ID and canonical request fingerprint before
  handing message data to SMTP. Reuse the same accepted receipt for an identical
  request in the same run. An unresolved identical request blocks sending even
  across runs until the operator checks its Message-ID. A fresh run may send the
  same content again after a confirmed prior send. Use a small durable indexed
  record with the existing state/evidence path. Keep authentication and
  recipient rejection distinguishable from ambiguous loss after submission.
  An SMTP acceptance confirms handoff to the server. Report final delivery as
  unknown. Provide an operator-only reconciliation script that records the
  verified outcome for the outgoing Message-ID. Leave unverified sends blocked.

## Acceptance

Use disposable IMAP/SMTP fixtures. Validate unread preservation, stale IDs,
literal search, MIME and size bounds, recipient denial, read-only eligibility,
and distinct outcomes for SMTP rejection, acceptance and connection loss.
Restart after a simulated lost acknowledgement and verify no second message is
submitted. Exercise “find the last message from this person and reply” with a
model and a test recipient. Use a scratch mailbox for the live opt-in smoke test.
