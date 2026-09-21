from email import policy
from email.parser import BytesParser

import pytest

from noreply_gateway.message import MessageRejected, mailbox


def parse(message):
    return BytesParser(policy=policy.SMTP).parsebytes(message.mime)


def test_normal_sender_case_insensitive(message):
    result = message(sender="Gateway <GATEWAY@example.test>")
    assert result.nat is False
    assert result.subject == "Example"


def test_nat_address_and_hidden_recipient(message):
    result = message(sender="_NAT_original@example.net", recipients=["hidden@example.net"])
    decoded = parse(result)
    assert decoded["From"] == "gateway@example.test"
    assert decoded["Subject"] == "Example (Sender: original@example.net)"
    assert decoded["Bcc"] == "hidden@example.net"
    assert set(result.recipients) == {"recipient@example.test", "hidden@example.net"}
    assert result.nat


def test_nat_display_name_multiple_markers(message):
    result = message(sender='"_NAT_Alice _NAT_" <alice@example.net>')
    assert result.nat
    assert "_NAT_" not in result.subject
    assert "alice@example.net" in result.subject
    assert parse(result)["From"] == "gateway@example.test"


def test_missing_subject_matches_java_null(message):
    assert message(sender="_NAT_x@example.net", subject=b"").subject == "null (Sender: x@example.net)"


@pytest.mark.parametrize("sender", ["outsider@example.net", "_nat_x@example.net", "a@example.test, b@example.test"])
def test_sender_rejected(message, sender):
    with pytest.raises((MessageRejected, ValueError)):
        message(sender=sender)


def test_subject_marker_does_not_activate_nat(message):
    with pytest.raises(MessageRejected):
        message(sender="x@example.net", subject=b"Subject: _NAT_Test\r\n")


@pytest.mark.parametrize("header", [b"From: gateway@example.test\r\n", b"Subject: two\r\n", b"Sender: attacker@example.net\r\n", b"To: broken@@example.net\r\n"])
def test_malformed_or_conflicting_headers(message, header):
    with pytest.raises((MessageRejected, ValueError)):
        message(extra=header)


def test_resent_cannot_undo_nat(message):
    with pytest.raises(MessageRejected):
        message(sender="_NAT_alice@example.net", extra=b"Resent-From: attacker@example.net\r\n")
    accepted = message(sender="old@example.net", extra=b"Resent-From: _NAT_new@example.net\r\n")
    assert "new@example.net" in accepted.subject
    assert parse(accepted)["From"] == "gateway@example.test"


def test_body_bytes_preserved(message):
    body = b"--abc\r\nContent-Type: application/octet-stream\r\nContent-Transfer-Encoding: base64\r\n\r\nAAECAwQ=\r\n--abc--\r\n"
    result = message(extra=b'Content-Type: multipart/mixed; boundary="abc"\r\n', body=body)
    assert result.mime.partition(b"\r\n\r\n")[2] == body


def test_union_recipients_and_dedup(message):
    result = message(extra=b"Cc: cc@example.net\r\nBcc: blind@example.net\r\n", recipients=["CC@example.net", "envelope@example.net"])
    assert {x.casefold() for x in result.recipients} == {"recipient@example.test", "cc@example.net", "blind@example.net", "envelope@example.net"}
    assert "blind@example.net" in str(parse(result)["Bcc"])


def test_strict_recipients_rejects_header_only(config, message):
    config.account.recipient_policy = "envelope_strict"
    with pytest.raises(MessageRejected):
        message(recipients=["other@example.net"])


def test_header_recipient_cannot_bypass_domain_or_count(config, message):
    config.account.allowed_recipient_domains = ["example.test"]
    with pytest.raises(MessageRejected):
        message(extra=b"Cc: external@example.net\r\n")
    config.account.allowed_recipient_domains = []
    config.smtp.max_recipients = 1
    with pytest.raises(MessageRejected):
        message(extra=b"Cc: external@example.net\r\n")


def test_stale_auth_assertions_removed(message):
    result = parse(message(extra=b"DKIM-Signature: v=1; invalid\r\nReturn-Path: <old@example.test>\r\nAuthentication-Results: example; spf=pass\r\n"))
    assert result["DKIM-Signature"] is None
    assert result["Return-Path"] is None
    assert result["Authentication-Results"] is None
    assert result["Date"] and result["Message-ID"]


@pytest.mark.parametrize("address", ["", "broken", "x@", "@example.net", "x@@example.net", "x@éxample.net", "x@example.net\r\n", "x@example.net garbage"])
def test_mailbox_validation(address):
    with pytest.raises((MessageRejected, ValueError)):
        mailbox(address)


def test_empty_envelope_is_supported():
    assert mailbox("", allow_empty=True) == ""
