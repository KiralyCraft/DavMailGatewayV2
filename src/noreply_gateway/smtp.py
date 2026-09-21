from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import ssl
import time
import uuid
from collections import Counter
from email.errors import MessageError

from .config import Config
from .message import MessageRejected, mailbox, prepare_message
from .store import QueueUnavailable, Store


class SMTPServer:
    """Small send-only ESMTP listener, deliberately without AUTH or STARTTLS.

    Optional implicit TLS uses asyncio's TLS server. Strict CRLF framing and
    line limits avoid ambiguous DATA boundaries. The listener is never an
    Internet-facing unauthenticated relay: client CIDRs are mandatory.
    """

    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self.server: asyncio.Server | None = None
        self.connections: set[asyncio.Task] = set()
        self.writers: set[asyncio.StreamWriter] = set()
        self.networks = [ipaddress.ip_network(n) for n in config.smtp.allowed_networks]
        self.buffered = 0
        self.counters: Counter = Counter()
        self.started_at = time.time()
        self.closing = False

    def allowed(self, host: str) -> bool:
        try:
            address = ipaddress.ip_address(host)
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
                address = address.ipv4_mapped
            return any(address in network for network in self.networks)
        except ValueError:
            return False

    async def start(self) -> None:
        context = None
        if self.config.smtp.tls_cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.config.smtp.tls_cert, self.config.smtp.tls_key)
        self.server = await asyncio.start_server(self._connection, self.config.smtp.host, self.config.smtp.port, limit=65_536, ssl=context)

    @property
    def port(self) -> int:
        return self.server.sockets[0].getsockname()[1]

    async def _reply(self, writer: asyncio.StreamWriter, text: str) -> None:
        writer.write(text.encode("ascii") + b"\r\n")
        await asyncio.wait_for(writer.drain(), 10)

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        peer = writer.get_extra_info("peername")
        registered = False
        try:
            if peer is None or self.allowed(peer[0]) is False:
                self.counters["denied_connections"] += 1
                await self._reply(writer, "554 5.7.1 Client network is not allowed")
                return
            if self.closing or len(self.connections) >= self.config.smtp.max_connections:
                await self._reply(writer, "421 4.3.2 Connection capacity exceeded")
                return
            self.connections.add(task)
            self.writers.add(writer)
            registered = True
            self.counters["connections"] += 1
            await self._reply(writer, "220 " + self.config.smtp.hostname + " ESMTP noreply-gateway")
            greeted = False
            extended = False
            sender: str | None = None
            recipients: list[str] = []
            command_count = 0
            while self.closing is False:
                line = await asyncio.wait_for(reader.readline(), self.config.smtp.command_timeout)
                if not line:
                    break
                if len(line) > 512 or line.endswith(b"\r\n") is False or b"\x00" in line:
                    await self._reply(writer, "500 5.5.2 Invalid command framing or length")
                    break
                try:
                    text = line[:-2].decode("ascii")
                except UnicodeDecodeError:
                    await self._reply(writer, "500 5.5.2 SMTP commands must be ASCII")
                    break
                command, _, argument = text.partition(" ")
                command, argument = command.upper(), argument.strip()
                command_count += 1
                if command_count > 100_000:
                    await self._reply(writer, "421 4.7.0 Please reconnect")
                    break
                if command == "QUIT":
                    await self._reply(writer, "221 2.0.0 Closing connection")
                    break
                if command in {"EHLO", "HELO"}:
                    if not argument:
                        await self._reply(writer, "501 5.5.2 Greeting domain required")
                        continue
                    greeted, extended, sender, recipients = True, command == "EHLO", None, []
                    if extended:
                        await self._reply(writer, "250-" + self.config.smtp.hostname + "\r\n250-SIZE " + str(self.config.smtp.max_message_bytes) + "\r\n250 8BITMIME")
                    else:
                        await self._reply(writer, "250 " + self.config.smtp.hostname)
                elif command == "AUTH":
                    await self._reply(writer, "502 5.5.1 AUTH is not supported; this listener uses a client-network allowlist")
                elif command == "NOOP":
                    await self._reply(writer, "250 2.0.0 OK")
                elif command == "RSET":
                    sender, recipients = None, []
                    await self._reply(writer, "250 2.0.0 Reset")
                elif command == "VRFY":
                    await self._reply(writer, "252 2.5.2 Cannot verify user")
                elif command == "MAIL":
                    if greeted is False or sender is not None:
                        await self._reply(writer, "503 5.5.1 Send EHLO/HELO, and RSET before a new transaction")
                        continue
                    match = re.fullmatch(r"FROM:\s*<([^<>]*)>(?:\s+(.+))?", argument, re.IGNORECASE)
                    if match is None:
                        await self._reply(writer, "501 5.5.2 Expected MAIL FROM:<address>")
                        continue
                    try:
                        parsed_sender = mailbox(match[1], allow_empty=True)
                        declared_size = 0
                        options = match[2].split() if match[2] else []
                        seen = set()
                        for option in options:
                            name, separator, value = option.upper().partition("=")
                            if extended is False or not separator or name in seen or name not in {"SIZE", "BODY"}:
                                raise MessageRejected("Unsupported or duplicate MAIL parameter")
                            seen.add(name)
                            if name == "SIZE":
                                if value.isdecimal() is False:
                                    raise MessageRejected("Invalid SIZE")
                                declared_size = int(value)
                            if name == "BODY" and value not in {"7BIT", "8BITMIME"}:
                                raise MessageRejected("Unsupported BODY encoding")
                    except MessageRejected as exc:
                        await self._reply(writer, "555 5.5.4 " + str(exc))
                        continue
                    if declared_size > self.config.smtp.max_message_bytes:
                        await self._reply(writer, "552 5.3.4 Declared message size exceeds limit")
                        continue
                    if self.store.healthy is False or self.store.accepting is False:
                        await self._reply(writer, "451 4.3.0 Queue is unavailable")
                        continue
                    sender, recipients = parsed_sender, []
                    await self._reply(writer, "250 2.1.0 Sender accepted; message From will be validated")
                elif command == "RCPT":
                    if sender is None:
                        await self._reply(writer, "503 5.5.1 MAIL FROM is required first")
                        continue
                    match = re.fullmatch(r"TO:\s*<([^<>]+)>", argument, re.IGNORECASE)
                    if match is None:
                        await self._reply(writer, "501 5.5.2 Expected RCPT TO:<address> without extension parameters")
                        continue
                    try:
                        recipient = mailbox(match[1])
                    except MessageRejected:
                        await self._reply(writer, "501 5.1.3 Invalid recipient mailbox")
                        continue
                    if len(recipients) >= self.config.smtp.max_recipients:
                        await self._reply(writer, "452 4.5.3 Too many recipients")
                        continue
                    allowed = self.config.account.allowed_recipient_domains
                    if allowed and recipient.rsplit("@", 1)[1].casefold() not in {d.casefold() for d in allowed}:
                        await self._reply(writer, "550 5.7.1 Recipient domain is not allowed")
                        continue
                    if recipient.casefold() not in {r.casefold() for r in recipients}:
                        recipients.append(recipient)
                    await self._reply(writer, "250 2.1.5 Recipient accepted")
                elif command == "DATA":
                    if argument or sender is None or not recipients:
                        await self._reply(writer, "503 5.5.1 MAIL and RCPT must precede DATA, without arguments")
                        continue
                    await self._reply(writer, "354 Send message; end with <CRLF>.<CRLF>")
                    try:
                        await self._data(reader, writer, sender, recipients)
                    finally:
                        sender, recipients = None, []
                else:
                    await self._reply(writer, "502 5.5.1 Command not implemented")
        except (asyncio.TimeoutError, ValueError):
            try:
                await self._reply(writer, "421 4.4.2 Timeout or line length exceeded")
            except (ConnectionError, asyncio.TimeoutError):
                pass
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).error("SMTP connection failed (%s)", type(exc).__name__)
        finally:
            if registered:
                self.connections.discard(task)
                self.writers.discard(writer)
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 3)
            except (OSError, asyncio.TimeoutError):
                pass

    async def _data(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, sender: str, recipients: list[str]) -> None:
        content = bytearray()
        retained = 0
        failure = ""
        try:
            async with asyncio.timeout(self.config.smtp.data_timeout):
                while True:
                    line = await reader.readline()
                    if not line:
                        raise ConnectionError("Disconnected in DATA")
                    if line == b".\r\n":
                        break
                    if line.endswith(b"\r\n") is False or len(line) > 1001 or b"\r" in line[:-2] or b"\n" in line[:-2]:
                        failure = "554 5.6.0 DATA requires CRLF and lines of at most 1000 octets"
                    if line.startswith(b"."):
                        line = line[1:]
                    if len(line) > 1000:
                        failure = "554 5.6.0 DATA line is too long"
                    if failure:
                        continue
                    if len(content) + len(line) > self.config.smtp.max_message_bytes:
                        failure = "552 5.3.4 Message too large"
                        continue
                    if self.buffered + len(line) > self.config.smtp.max_buffer_bytes:
                        failure = "452 4.3.1 Message-buffer capacity exceeded"
                        continue
                    content.extend(line)
                    retained += len(line)
                    self.buffered += len(line)
            if failure:
                self.counters["rejected_messages"] += 1
                await self._reply(writer, failure)
                return
            identifier = str(uuid.uuid4())
            try:
                message = prepare_message(bytes(content), recipients, sender, identifier, self.config)
            except (MessageRejected, ValueError, TypeError, AttributeError, MessageError):
                self.counters["rejected_messages"] += 1
                await self._reply(writer, "554 5.7.1 Invalid message headers, sender, or effective recipients; see gateway policy")
                return
            try:
                await self.store.submit(message)
            except QueueUnavailable:
                self.counters["deferred_messages"] += 1
                await self._reply(writer, "451 4.3.0 Unable to durably queue message; retry later")
                return
            self.counters["accepted_messages"] += 1
            await self._reply(writer, "250 2.0.0 Durably queued as " + identifier)
        finally:
            self.buffered -= retained

    async def close(self) -> None:
        self.closing = True
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for writer in list(self.writers):
            writer.close()
        if self.connections:
            done, pending = await asyncio.wait(self.connections, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
