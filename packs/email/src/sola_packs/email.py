"""Named TLS mailboxes, stable IMAP identities and receipted sender-only replies."""

import email.policy
import imaplib
import json
import re
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, getaddresses, make_msgid
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator
from sola_packs.support import (
    IDENTIFIER,
    Settings,
    external,
    fingerprint,
    grant,
    resource,
    secret,
)
from sola_packs.support.evidence import Operation

from solakit.core.config import SecretRef
from solakit.core.io import durable
from solakit.tools import ToolContext, ToolError, ToolResult, tool

_ID = re.compile(r"<[^<>\s@]+@[^<>\s@]+>\Z")


class Mailbox(Settings):
    imap_host: str
    username: str
    address: str
    password: SecretRef
    smtp_host: str | None = None
    imap_port: int = Field(default=993, ge=1, le=65535)
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_security: str = "tls"
    smtp_username: str | None = None
    smtp_password: SecretRef | None = None
    folder: str = "INBOX"
    timeout_s: float = Field(default=15, gt=0, le=60)
    max_message_bytes: int = Field(default=1000000, ge=1024, le=10000000)

    @model_validator(mode="after")
    def valid(self):
        if self.smtp_security not in {"tls", "starttls"}:
            raise ValueError("SMTP must use TLS or STARTTLS")
        for value in (
            self.imap_host,
            self.folder,
            self.username,
            self.smtp_host or "unused",
            self.smtp_username or "unused",
        ):
            if not value or any(character in value for character in "\r\n\x00"):
                raise ValueError("Invalid mailbox connection setting")
        address(self.address)
        return self


def address(value):
    if not value or any(character in value for character in "\r\n\x00"):
        raise ToolError("Invalid mailbox address")
    parsed = getaddresses([value])
    if len(parsed) != 1:
        raise ToolError("A single mailbox address is required")
    target = parsed[0][1]
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", target):
        raise ToolError("A valid ASCII mailbox address is required")
    return target


def credentials(ctx, cfg):
    password = secret(ctx, cfg.password)
    smtp_password = secret(ctx, cfg.smtp_password) if cfg.smtp_password else password
    return password, smtp_password


def connect(cfg, password):
    client = imaplib.IMAP4_SSL(
        cfg.imap_host,
        cfg.imap_port,
        ssl_context=ssl.create_default_context(),
        timeout=cfg.timeout_s,
    )
    try:
        client.login(cfg.username, password)
        folder = '"' + cfg.folder.replace("\\", "\\\\").replace('"', '\\"') + '"'
        status, _ = client.select(folder, readonly=True)
        if status != "OK":
            raise ToolError("Cannot select configured mailbox")
        _, values = client.response("UIDVALIDITY")
        validity = values[0].decode("ascii") if values else ""
        if not validity.isdigit():
            raise ToolError("Mailbox did not supply UIDVALIDITY")
        return client, validity
    except BaseException:
        close(client)
        raise


def close(client):
    try:
        client.logout()
    except (OSError, imaplib.IMAP4.error):
        pass


def identity(token, validity):
    match = re.fullmatch(r"([0-9]+):([1-9][0-9]*)", token)
    if match is None or match[1] != validity:
        raise ToolError(
            "Message identity is invalid or mailbox UIDVALIDITY changed; search again"
        )
    return match[2]


def fetch(client, uid, cap, *, headers=False):
    part = (
        "HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID REPLY-TO REFERENCES)"
        if headers
        else ""
    )
    status, values = client.uid("FETCH", uid, f"(BODY.PEEK[{part}]<0.{cap + 1}>)")
    if status != "OK":
        raise ToolError("Message could not be fetched")
    chunks = [
        item[1]
        for item in values
        if isinstance(item, tuple) and isinstance(item[1], bytes)
    ]
    if not chunks or sum(map(len, chunks)) > cap:
        raise ToolError("Message is missing or exceeds the configured byte limit")
    return BytesParser(policy=email.policy.default).parsebytes(b"".join(chunks))


def metadata(message):
    return {
        key: str(message.get(key, ""))
        for key in ("From", "To", "Subject", "Date", "Message-ID")
    }


def read_message(ctx, cfg, token):
    password = secret(ctx, cfg.password)
    client, validity = connect(cfg, password)
    try:
        message = fetch(client, identity(token, validity), cfg.max_message_bytes)
        body = message.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body else ""
        return {
            "id": token,
            "headers": metadata(message),
            "body": text,
            "content_type": body.get_content_type() if body else None,
            "attachments": [
                {
                    "filename": part.get_filename(),
                    "content_type": part.get_content_type(),
                }
                for part in message.iter_attachments()
            ],
        }
    finally:
        close(client)


@tool(name="email.search", profiles=["*"])
async def search(
    ctx: ToolContext, mailbox: str, query: str = "", limit: int = 20
) -> ToolResult:
    """Search literal text and return stable UIDVALIDITY:UID message identities."""
    cfg = resource(ctx, "mailboxes", mailbox, Mailbox, "read")
    if (
        not 1 <= limit <= 100
        or len(query) > 1000
        or any(c in query for c in "\r\n\x00")
    ):
        raise ToolError(
            "Search needs a single-line query up to 1000 characters and limit 1–100"
        )

    def perform():
        password = secret(ctx, cfg.password)
        client, validity = connect(cfg, password)
        try:
            quoted = '"' + query.replace("\\", "\\\\").replace('"', '\\"') + '"'
            arguments = (
                ("CHARSET", "UTF-8", "TEXT", quoted.encode("utf-8"))
                if query
                else (None, "ALL")
            )
            status, values = client.uid("SEARCH", *arguments)
            if status != "OK" or not values or not isinstance(values[0], bytes):
                raise ToolError("Mailbox search failed")
            ids = values[0].split()
            if len(ids) > 100000 or any(not uid.isdigit() for uid in ids):
                raise ToolError("Mailbox returned invalid or excessive search results")
            selected = sorted({int(uid) for uid in ids}, reverse=True)[:limit]
            rows = [
                {
                    "id": f"{validity}:{uid}",
                    "headers": metadata(fetch(client, str(uid), 65536, headers=True)),
                }
                for uid in selected
            ]
            return {"messages": rows, "has_more": len(ids) > limit}
        finally:
            close(client)

    try:
        result = await durable(perform)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise ToolError(
            "Mailbox search unavailable; check connection and credentials"
        ) from exc
    return ToolResult(content=external(f"mailbox:{mailbox}", result))


@tool(name="email.read", profiles=["*"])
async def read(ctx: ToolContext, mailbox: str, message_id: str) -> ToolResult:
    """Read external content by stable identity without marking the message seen."""
    cfg = resource(ctx, "mailboxes", mailbox, Mailbox, "read")
    try:
        result = await durable(read_message, ctx, cfg, message_id)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise ToolError(
            "Mailbox read unavailable; check connection and credentials"
        ) from exc
    return ToolResult(content=external(f"mailbox:{mailbox}", result))


def compose(ctx, cfg, original, body, outgoing_id):
    source_id = str(original.get("Message-ID", "")).strip()
    if not _ID.fullmatch(source_id) or len(source_id) > 998:
        raise ToolError("Original message lacks a usable Message-ID; reply refused")
    recipient = address(str(original.get("Reply-To") or original.get("From") or ""))
    subject = (
        str(original.get("Subject", "")).replace("\r", " ").replace("\n", " ")[:900]
    )
    references = re.findall(
        r"<[^<>\s@]+@[^<>\s@]+>", str(original.get("References", ""))
    )
    references = [
        value for value in references if len(value) <= 998 and value != source_id
    ][-19:] + [source_id]
    message = EmailMessage()
    message["From"] = address(cfg.address)
    message["To"] = recipient
    message["Subject"] = (
        subject if subject.lower().startswith("re:") else "Re: " + subject
    )
    message["Date"] = format_datetime(datetime.now(ZoneInfo(ctx.config.timezone)))
    message["Message-ID"] = outgoing_id
    message["In-Reply-To"] = source_id
    message["References"] = " ".join(references)
    message.set_content(body, cte="quoted-printable")
    return message, recipient


def send(ctx, cfg, mailbox, source, body, operation_id):
    request = {
        "mailbox": mailbox,
        "source": source,
        "body": body,
        "host": cfg.imap_host,
        "folder": cfg.folder,
        "username": cfg.username,
        "address": cfg.address,
    }
    with (
        Operation(ctx, "email-identities", [mailbox, operation_id], request) as binding,
        Operation(ctx, "email", [mailbox, fingerprint(request)], request) as operation,
    ):
        if binding.current is None:
            binding.record("bound", source=source)
        previous = operation.current
        if previous and previous["stage"] == "sent":
            return previous["result"]
        if previous and previous["stage"] not in {"failed_before_send", "rejected"}:
            raise ToolError(
                "Email operation is pending or uncertain; "
                f"Message-ID {previous.get('outgoing_id', 'unknown')}. "
                "Inspect the receipt before retrying"
            )
        password, smtp_password = credentials(ctx, cfg)
        client, validity = connect(cfg, password)
        try:
            original = fetch(client, identity(source, validity), cfg.max_message_bytes)
        finally:
            close(client)
        outgoing_id = (
            previous["outgoing_id"]
            if previous
            else make_msgid(domain=address(cfg.address).split("@", 1)[1])
        )
        message, recipient = compose(ctx, cfg, original, body, outgoing_id)
        operation.record("prepared", outgoing_id=outgoing_id, source=source)
        phase, smtp = "connecting", None
        try:
            tls = ssl.create_default_context()
            if cfg.smtp_security == "tls":
                smtp = smtplib.SMTP_SSL(
                    cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout_s, context=tls
                )
            else:
                smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout_s)
                smtp.ehlo()
                smtp.starttls(context=tls)
                smtp.ehlo()
            smtp.login(cfg.smtp_username or cfg.username, smtp_password)
            phase = "sending"
            refused = smtp.send_message(
                message, from_addr=address(cfg.address), to_addrs=[recipient]
            )
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        except Exception as exc:
            known_rejection = isinstance(
                exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError)
            )
            stage = (
                "failed_before_send"
                if phase == "connecting"
                else ("rejected" if known_rejection else "uncertain")
            )
            operation.record(stage, outgoing_id=outgoing_id, source=source)
            raise ToolError(
                f"Email {stage}; Message-ID {outgoing_id}. "
                "Inspect operation receipt before retrying."
            ) from exc
        finally:
            if smtp is not None:
                try:
                    smtp.close()
                except (OSError, smtplib.SMTPException):
                    pass
        result = {
            "status": "accepted_by_smtp",
            "operation_id": operation_id,
            "message_id": outgoing_id,
            "source": source,
        }
        operation.record("sent", outgoing_id=outgoing_id, result=result)
        return result


@tool(name="email.reply", profiles=["*"], writes=["email.reply"])
async def reply(
    ctx: ToolContext, mailbox: str, message_id: str, body: str, operation_id: str
) -> ToolResult:
    """Reply to source sender/Reply-To; reuse operation_id to prevent duplicates."""
    grant(ctx, "email.reply")
    cfg = resource(ctx, "mailboxes", mailbox, Mailbox, "reply")
    if not cfg.smtp_host:
        raise ToolError("SMTP is not configured for this mailbox")
    if not IDENTIFIER.fullmatch(operation_id) or not body.strip() or len(body) > 64000:
        raise ToolError(
            "Reply requires a stable operation ID and 1–64000 body characters"
        )
    try:
        result = await durable(send, ctx, cfg, mailbox, message_id, body, operation_id)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise ToolError(
            "Email reply unavailable; inspect receipt before retrying"
        ) from exc
    return ToolResult(content=json.dumps(result))
