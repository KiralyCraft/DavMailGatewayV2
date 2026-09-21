#!/usr/bin/env python3
"""Offline integration benchmark: real SMTP -> SQLite WAL/FULL -> mock HTTP.

Never reads gateway.toml, never loads credentials, and never contacts Microsoft.
The only HTTP destination is a fresh loopback mock. The explicit mock backend
is built here, not selectable by the production service.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import platform
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import aiohttp
from aiohttp import web
from defusedxml import ElementTree

from noreply_gateway.backend import MicrosoftBackend
from noreply_gateway.config import Config
from noreply_gateway.delivery import Dispatcher
from noreply_gateway.smtp import SMTPServer
from noreply_gateway.store import Store


class OfflineTokens:
    async def get_token(self):
        return "OFFLINE-BENCHMARK-NOT-A-CREDENTIAL"

    async def rejected_token(self, token):
        raise RuntimeError("The offline mock unexpectedly rejected a fake token")

    def accepted_token(self):
        pass


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


async def read_response(reader):
    lines = []
    while True:
        line = await asyncio.wait_for(reader.readline(), 30)
        if len(line) < 4:
            raise RuntimeError("SMTP connection closed unexpectedly")
        lines.append(line)
        if line[3:4] != b"-":
            return int(line[:3]), b"".join(lines)


async def command(reader, writer, data, expected):
    writer.write(data + b"\r\n")
    await writer.drain()
    status, content = await read_response(reader)
    if status != expected:
        raise RuntimeError("Unexpected SMTP reply: " + content.decode("ascii", "replace"))


def make_message(index: int, size: int) -> bytes:
    header = (f"From: _NAT_source@example.test\r\nTo: recipient@example.test\r\nSubject: Offline load test {index}\r\nMessage-ID: <offline-{index}@example.test>\r\nContent-Type: text/plain; charset=us-ascii\r\n\r\n").encode()
    body_size = max(0, size - len(header))
    lines, remainder = divmod(body_size, 78)
    body = (b"x" * 76 + b"\r\n") * lines
    if remainder >= 2:
        body += b"x" * (remainder - 2) + b"\r\n"
    return header + body


async def benchmark(args):
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="gateway-benchmark-") as temporary:
        config = Config(data_dir=Path(temporary))
        config.account.sender = "gateway@example.test"
        config.account.backend = args.backend
        config.smtp.port = 0
        config.smtp.max_connections = args.clients + 8
        config.smtp.max_message_bytes = max(2_000_000, args.message_bytes + 2048)
        config.queue.min_free_bytes = 0
        config.queue.max_messages = args.messages + 100
        config.delivery.messages_per_second = args.delivery_rate
        config.delivery.recipient_limit_24h = 0  # Offline synthetic test only.
        config.delivery.shutdown_seconds = 5
        count, active, maximum_active, seen = 0, 0, 0, set()
        accepted_times = []
        success_xml = b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages"><s:Body><m:CreateItemResponse><m:ResponseMessages><m:CreateItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode></m:CreateItemResponseMessage></m:ResponseMessages></m:CreateItemResponse></s:Body></s:Envelope>'

        async def mock(request):
            nonlocal count, active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                payload = await request.read()
                if args.backend == "graph":
                    mime = base64.b64decode(payload, validate=True)
                else:
                    root = ElementTree.fromstring(payload)
                    mime = base64.b64decode(root.findtext(".//{http://schemas.microsoft.com/exchange/services/2006/types}MimeContent"), validate=True)
                assert b"From: gateway@example.test\r\n" in mime
                assert b"Sender: source@example.test" in mime
                identifier = request.headers["client-request-id"]
                assert identifier not in seen
                seen.add(identifier)
                await asyncio.sleep(args.mock_latency_ms / 1000)
                count += 1
                accepted_times.append(time.perf_counter())
                return web.Response(status=202 if args.backend == "graph" else 200, body=b"" if args.backend == "graph" else success_xml)
            finally:
                active -= 1

        app = web.Application(client_max_size=max(5_000_000, args.message_bytes * 2))
        app.router.add_post("/send", mock)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        endpoint = "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1]) + "/send"
        store = Store(config)
        await store.start()
        smtp = SMTPServer(config, store)
        await smtp.start()
        latencies = []
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), connector=aiohttp.TCPConnector(limit=4)) as session:
                backend = MicrosoftBackend(config, OfflineTokens(), session)
                backend.endpoint = endpoint
                dispatcher = Dispatcher(config, store, backend)
                await dispatcher.start()
                try:
                    async def producer(number):
                        reader, writer = await asyncio.open_connection("127.0.0.1", smtp.port)
                        try:
                            assert (await read_response(reader))[0] == 220
                            await command(reader, writer, b"EHLO benchmark", 250)
                            for index in range(number, args.messages, args.clients):
                                before = time.perf_counter()
                                await command(reader, writer, b"MAIL FROM:<ignored@example.test>", 250)
                                await command(reader, writer, b"RCPT TO:<recipient@example.test>", 250)
                                await command(reader, writer, b"DATA", 354)
                                writer.write(make_message(index, args.message_bytes) + b".\r\n")
                                await writer.drain()
                                code, response = await read_response(reader)
                                if code != 250:
                                    raise RuntimeError(response.decode())
                                latencies.append(time.perf_counter() - before)
                            await command(reader, writer, b"QUIT", 221)
                        finally:
                            writer.close()
                            await writer.wait_closed()
                    started = time.perf_counter()
                    await asyncio.gather(*(producer(n) for n in range(args.clients)))
                    intake_done = time.perf_counter()
                    async with asyncio.timeout(args.timeout):
                        while (await store.stats())["counters"].get("submitted", 0) < args.messages:
                            if store.healthy is False or (await store.stats())["states"].get("uncertain", 0):
                                raise RuntimeError("Pipeline fault in offline benchmark")
                            await asyncio.sleep(.02)
                    completed = time.perf_counter()
                    stats = await store.stats()
                    assert count == args.messages == len(seen)
                    assert stats["counters"]["accepted"] == count
                    assert stats["counters"]["submitted"] == count
                    assert stats["counters"]["retained_messages"] == 0
                    result = {
                        "test": "OFFLINE SMTP to SQLite WAL/FULL to mock Microsoft HTTP",
                        "backend": args.backend,
                        "python": platform.python_version(), "platform": platform.platform(),
                        "logical_cpus_visible": os.cpu_count(),
                        "messages": args.messages, "nominal_input_bytes": args.message_bytes,
                        "sample_input_bytes": len(make_message(0, args.message_bytes)),
                        "persistent_smtp_clients": args.clients,
                        "upstream_workers": config.delivery.workers,
                        "configured_mock_delivery_rate": args.delivery_rate,
                        "mock_response_delay_ms": args.mock_latency_ms,
                        "maximum_mock_concurrency": maximum_active,
                        "intake_seconds": round(intake_done - started, 4),
                        "end_to_end_seconds": round(completed - started, 4),
                        "smtp_durable_accepts_per_second": round(args.messages / (intake_done - started), 2),
                        "end_to_end_mock_submissions_per_second": round(args.messages / (completed - started), 2),
                        "smtp_transaction_latency_ms": {"p50": round(percentile(latencies, .5) * 1000, 2), "p95": round(percentile(latencies, .95) * 1000, 2), "p99": round(percentile(latencies, .99) * 1000, 2)},
                        "accepted": stats["counters"]["accepted"], "mock_submitted": count,
                        "duplicate_mock_submissions": 0,
                        "retained_messages_at_end": stats["counters"]["retained_messages"],
                        "sqlite_journal_mode": await store.call(lambda: store.db.execute("PRAGMA journal_mode").fetchone()[0]),
                        "sqlite_synchronous": await store.call(lambda: store.db.execute("PRAGMA synchronous").fetchone()[0]),
                        "peak_rss_kib_entire_test_process": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                        "live_microsoft_verified": False,
                        "limitations": "One local container, synthetic small messages; clients and mock share the process. No power-loss, real-disk durability, Internet latency, tenant authentication, Microsoft throttling, or final recipient-delivery benchmark."
                    }
                finally:
                    await dispatcher.close()
        finally:
            await smtp.close()
            await store.close()
            await runner.cleanup()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=3000)
    parser.add_argument("--clients", type=int, default=32)
    parser.add_argument("--message-bytes", type=int, default=10_240)
    parser.add_argument("--backend", choices=("ews", "graph"), default="ews")
    parser.add_argument("--mock-latency-ms", type=float, default=5)
    parser.add_argument("--delivery-rate", type=float, default=1000)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.messages, args.clients) < 1 or args.message_bytes < 512 or args.message_bytes > 2_000_000 or args.delivery_rate <= 0 or args.delivery_rate > 1000 or args.mock_latency_ms < 0:
        parser.error("Invalid size, count, rate, or mock latency")
    result = asyncio.run(benchmark(args))
    text = json.dumps(result, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n")


if __name__ == "__main__":
    main()
