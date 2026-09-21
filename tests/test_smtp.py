import asyncio
import smtplib
from types import SimpleNamespace

import pytest

from noreply_gateway.smtp import SMTPServer


@pytest.fixture
async def smtp(config, store):
    instance = SMTPServer(config, store)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.close()


async def response(reader):
    lines = []
    while True:
        line = await asyncio.wait_for(reader.readline(), 3)
        lines.append(line)
        if len(line) < 4 or line[3:4] != b"-":
            return b"".join(lines)


async def command(reader, writer, data):
    writer.write(data + b"\r\n")
    await writer.drain()
    return await response(reader)


async def test_smtplib_no_auth_persistent_transactions(smtp, store):
    def run():
        with smtplib.SMTP("127.0.0.1", smtp.port, timeout=5) as client:
            assert client.ehlo()[0] == 250
            assert "auth" not in client.esmtp_features
            assert client.docmd("AUTH", "PLAIN dummy")[0] == 502
            for sender in ("gateway@example.test", "_NAT_source@example.net"):
                raw = f"From: {sender}\r\nTo: recipient@example.test\r\nSubject: Test\r\n\r\n.dot\r\n..two\r\n"
                assert client.sendmail("anything@example.net", ["recipient@example.test"], raw) == {}
    await asyncio.to_thread(run)
    assert (await store.stats())["counters"]["accepted"] == 2
    for row in await store.messages():
        assert (await store.export(row["id"])).endswith(b".dot\r\n..two\r\n")


async def test_from_mismatch_is_rejected(smtp, store):
    def run():
        with smtplib.SMTP("127.0.0.1", smtp.port, timeout=5) as client:
            with pytest.raises(smtplib.SMTPDataError) as exc:
                client.sendmail("gateway@example.test", ["recipient@example.test"], "From: attacker@example.net\r\n\r\nbody\r\n")
            assert exc.value.smtp_code == 554
    await asyncio.to_thread(run)
    assert (await store.stats())["counters"].get("accepted", 0) == 0


async def test_selected_additional_address_is_accepted_by_live_smtp(config, store):
    server = SMTPServer(config, store, SimpleNamespace(selected_sender="shared@example.test"))
    await server.start()
    try:
        def run():
            with smtplib.SMTP("127.0.0.1", server.port, timeout=5) as client:
                raw = "From: shared@example.test\r\nTo: recipient@example.test\r\nSubject: delegated\r\n\r\nbody\r\n"
                assert client.sendmail("ignored@example.test", ["recipient@example.test"], raw) == {}
                with pytest.raises(smtplib.SMTPDataError) as error:
                    client.sendmail("ignored@example.test", ["recipient@example.test"], raw.replace("shared@example.test", "unlisted@example.test"))
                assert error.value.smtp_code == 554
        await asyncio.to_thread(run)
        rows = await store.messages()
        assert len(rows) == 1
        assert b"From: shared@example.test" in await store.export(rows[0]["id"])
    finally:
        await server.close()


async def test_sequence_empty_envelope_and_rset(smtp, store):
    reader, writer = await asyncio.open_connection("127.0.0.1", smtp.port)
    try:
        assert (await response(reader)).startswith(b"220")
        assert (await command(reader, writer, b"MAIL FROM:<>")).startswith(b"503")
        await command(reader, writer, b"EHLO client")
        assert (await command(reader, writer, b"MAIL FROM:<>")).startswith(b"250")
        assert (await command(reader, writer, b"DATA")).startswith(b"503")
        await command(reader, writer, b"RSET")
        assert (await command(reader, writer, b"RCPT TO:<x@example.net>")).startswith(b"503")
        await command(reader, writer, b"MAIL FROM:<>")
        await command(reader, writer, b"RCPT TO:<recipient@example.test>")
        assert (await command(reader, writer, b"DATA")).startswith(b"354")
        assert (await command(reader, writer, b"From: gateway@example.test\r\n\r\nbody\r\n.")).startswith(b"250")
        assert (await store.messages())[0]["status"] == "queued"
    finally:
        writer.close()
        await writer.wait_closed()


async def test_bare_lf_data_is_drained_without_smuggling(smtp, store):
    reader, writer = await asyncio.open_connection("127.0.0.1", smtp.port)
    try:
        await response(reader)
        await command(reader, writer, b"EHLO c")
        await command(reader, writer, b"MAIL FROM:<>")
        await command(reader, writer, b"RCPT TO:<recipient@example.test>")
        await command(reader, writer, b"DATA")
        writer.write(b"From: gateway@example.test\r\n\r\nbody\n.\nMAIL FROM:<attacker@example.net>\r\n.\r\nNOOP\r\n")
        await writer.drain()
        assert (await response(reader)).startswith(b"554")
        assert (await response(reader)).startswith(b"250")
        assert await store.messages() == []
    finally:
        writer.close()
        await writer.wait_closed()


async def test_buffer_backpressure_no_ack(smtp, config, store):
    config.smtp.max_buffer_bytes = 1
    def run():
        with smtplib.SMTP("127.0.0.1", smtp.port, timeout=5) as client:
            with pytest.raises(smtplib.SMTPDataError) as exc:
                client.sendmail("", ["x@example.test"], "From: gateway@example.test\r\n\r\nbody\r\n")
            assert exc.value.smtp_code == 452
            assert client.noop()[0] == 250
    await asyncio.to_thread(run)
    assert smtp.buffered == 0
    assert await store.messages() == []


async def test_queue_failure_returns_451(smtp, config, store):
    config.queue.max_messages = 1
    def run():
        with smtplib.SMTP("127.0.0.1", smtp.port, timeout=5) as client:
            message = "From: gateway@example.test\r\n\r\nbody\r\n"
            client.sendmail("", ["x@example.test"], message)
            with pytest.raises(smtplib.SMTPDataError) as exc:
                client.sendmail("", ["x@example.test"], message)
            assert exc.value.smtp_code == 451
    await asyncio.to_thread(run)


def test_ipv4_mapped_network_gate(smtp):
    assert smtp.allowed("127.0.0.1")
    assert smtp.allowed("::ffff:127.0.0.1")
    assert smtp.allowed("198.51.100.1") is False
    assert smtp.allowed("not-an-ip") is False


async def test_network_denial(config, store):
    config.smtp.allowed_networks = ["192.0.2.0/24"]
    instance = SMTPServer(config, store)
    await instance.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", instance.port)
        assert (await response(reader)).startswith(b"554")
        writer.close()
        await writer.wait_closed()
    finally:
        await instance.close()
