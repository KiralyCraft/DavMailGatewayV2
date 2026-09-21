from __future__ import annotations

import re
from dataclasses import dataclass
from email import policy
from email.headerregistry import Address
from email.errors import MessageError
from email.parser import BytesHeaderParser
from email.utils import formatdate
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config


class MessageRejected(ValueError):
    pass


def mailbox(value: str, allow_empty: bool = False) -> str:
    if value == "" and allow_empty:
        return ""
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or len(value) > 254:
        raise MessageRejected("Invalid mailbox")
    try:
        value.encode("ascii")
        address = Address(addr_spec=value)
    except (ValueError, UnicodeError, IndexError, MessageError) as exc:
        raise MessageRejected("Invalid mailbox; SMTPUTF8 addresses are not supported") from exc
    if address.username == "" or address.domain == "" or value.strip() != value:
        raise MessageRejected("A complete addr-spec is required")
    return address.addr_spec


@dataclass
class PreparedMessage:
    id: str
    mime: bytes
    recipients: list[str]
    envelope_sender: str
    original_from: str
    subject: str
    message_id: str
    nat: bool


def prepare_message(raw: bytes, recipients: list[str], envelope_sender: str, queue_id: str, config: Config, additional_sender: str = "") -> PreparedMessage:
    if len(raw) > config.smtp.max_message_bytes:
        raise MessageRejected("Message too large")
    head, sep, body = raw.partition(b"\r\n\r\n")
    if sep == b"" or len(head) > config.smtp.max_header_bytes or b"\x00" in head:
        raise MessageRejected("Missing, oversized, or invalid message headers")
    if re.search(br"(?<!\r)\n|\r(?!\n)", head):
        raise MessageRejected("Headers require CRLF line endings")
    message = BytesHeaderParser(policy=policy.SMTP).parsebytes(head + b"\r\n\r\n")
    if message.defects:
        raise MessageRejected("Malformed headers")

    # Resolve the effective sender first, so Resent-From cannot undo NAT or
    # bypass sender enforcement. This intentionally fixes the Java ordering bug.
    for name in ("From", "To", "Cc", "Bcc", "Message-ID"):
        values = message.get_all("Resent-" + name)
        if values is not None:
            del message[name]
            del message["Resent-" + name]
            for value in values:
                message[name] = str(value)
    for name in ("From", "Subject", "Sender", "Message-ID"):
        if len(message.get_all(name, [])) > 1:
            raise MessageRejected("Duplicate " + name + " header")
    sender_header = message["From"]
    if sender_header is None or sender_header.defects or len(sender_header.addresses) != 1:
        raise MessageRejected("Exactly one valid From mailbox is required")
    original_from = str(sender_header)
    nat = config.account.nat_marker in original_from
    if nat:
        original = original_from.replace(config.account.nat_marker, "")
        subject = str(message["Subject"]) if message["Subject"] is not None else "null"
        del message["Subject"]
        message["Subject"] = subject + " (Sender: " + original + ")"
        message.replace_header("From", config.account.sender)
    elif mailbox(sender_header.addresses[0].addr_spec).casefold() not in {config.account.sender.casefold(), additional_sender.casefold()}:
        raise MessageRejected("From must match the configured sending mailbox, or contain the NAT marker")
    sender = message["Sender"]
    if sender is not None and (sender.defects or len(sender.addresses) != 1 or sender.addresses[0].addr_spec.casefold() != mailbox(message["From"].addresses[0].addr_spec).casefold()):
        raise MessageRejected("Sender header must match the configured mailbox")

    envelope = list(dict.fromkeys(mailbox(r) for r in recipients))
    if len(envelope) == 0:
        raise MessageRejected("At least one recipient is required")
    visible: dict[str, str] = {}
    for name in ("To", "Cc", "Bcc"):
        for header in message.get_all(name, []):
            if header.defects:
                raise MessageRejected("Malformed " + name + " header")
            for address in header.addresses:
                recipient = mailbox(address.addr_spec)
                visible[recipient.casefold()] = recipient
    if config.account.recipient_policy == "envelope_strict":
        envelope_keys = {r.casefold() for r in envelope}
        if set(visible) - envelope_keys:
            raise MessageRejected("Header recipients must also appear in SMTP RCPT TO")
    missing = [r for r in envelope if r.casefold() not in visible]
    if missing:
        previous = [str(x) for x in message.get_all("Bcc", [])]
        del message["Bcc"]
        message["Bcc"] = ", ".join(previous + missing)
    effective = dict(visible)
    effective.update((r.casefold(), r) for r in envelope)
    if len(effective) > config.smtp.max_recipients:
        raise MessageRejected("Too many effective recipients (headers plus envelope)")
    allowed = {d.casefold() for d in config.account.allowed_recipient_domains}
    if allowed and any(r.rsplit("@", 1)[1].casefold() not in allowed for r in effective):
        raise MessageRejected("Recipient domain is not allowed")

    # Do not forward stale assertions made about the pre-rewrite message.
    for name in ("Return-Path", "Authentication-Results", "DKIM-Signature", "ARC-Seal", "ARC-Message-Signature", "ARC-Authentication-Results"):
        del message[name]
    if message["Message-ID"] is None:
        message["Message-ID"] = "<" + queue_id + "@" + config.smtp.hostname + ">"
    if message["Date"] is None:
        message["Date"] = formatdate(localtime=False, usegmt=True)
    message_id = str(message["Message-ID"])
    # Only serialize headers. The body, multipart boundaries, and attachment
    # bytes are never reparsed or re-encoded.
    encoded_headers = message.as_bytes(policy=policy.SMTP).partition(b"\r\n\r\n")[0]
    mime = encoded_headers + b"\r\n\r\n" + body
    if len(mime) > config.smtp.max_message_bytes:
        raise MessageRejected("Rewritten message exceeds the configured size limit")
    if additional_sender and message["From"].addresses[0].addr_spec.casefold() == additional_sender.casefold() and len(mime) > 2_500_000:
        raise MessageRejected("Additional Graph sender is limited to 2,500,000-byte MIME messages")
    return PreparedMessage(queue_id, mime, list(effective.values()), envelope_sender, original_from, str(message.get("Subject", "")), message_id, nat)
