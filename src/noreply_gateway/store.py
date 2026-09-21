from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, TypeVar

from .config import Config
from .message import PreparedMessage

T = TypeVar("T")


class QueueUnavailable(RuntimeError):
    pass


@dataclass
class Submission:
    message: PreparedMessage
    future: asyncio.Future


class Store:
    """One SQLite owner thread; entire transactions run as one executor job.

    MIME and metadata are committed together in WAL/FULL mode before SMTP 250.
    A group-commit writer amortizes fsync without acknowledging uncommitted mail.
    """

    def __init__(self, config: Config):
        self.config = config
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gateway-db")
        self.pending: asyncio.Queue[Submission | None] = asyncio.Queue(config.queue.pending_submissions)
        self.writer: asyncio.Task | None = None
        self.db: sqlite3.Connection | None = None
        self.healthy = True
        self.accepting = False
        self.wakeup = asyncio.Event()

    async def call(self, function: Callable[..., T], *args) -> T:
        return await asyncio.get_running_loop().run_in_executor(self.executor, functools.partial(function, *args))

    async def start(self) -> None:
        await self.call(self._open)
        self.accepting = True
        self.writer = asyncio.create_task(self._writer(), name="queue-commit-writer")

    def _open(self) -> None:
        path = self.config.data_dir / "queue.sqlite3"
        self.db = sqlite3.connect(path, timeout=5)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_size_limit=67108864")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise ValueError("Unsupported queue database schema")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY, created REAL NOT NULL, cycle_started REAL NOT NULL, updated REAL NOT NULL,
                status TEXT NOT NULL, next_attempt REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                envelope_sender TEXT NOT NULL, original_from TEXT NOT NULL,
                recipients TEXT NOT NULL, recipient_count INTEGER NOT NULL,
                subject TEXT NOT NULL, message_id TEXT NOT NULL,
                nat INTEGER NOT NULL, size INTEGER NOT NULL, mime BLOB,
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS messages_ready ON messages(status, next_attempt, created);
            CREATE INDEX IF NOT EXISTS messages_created ON messages(created DESC, id DESC);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS counters (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS buckets (
                minute INTEGER NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL,
                PRIMARY KEY (minute, name)
            );
            CREATE TABLE IF NOT EXISTS quota (
                id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, mailbox TEXT NOT NULL,
                at REAL NOT NULL, recipients INTEGER NOT NULL, state TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS quota_time ON quota(mailbox, at);
            CREATE INDEX IF NOT EXISTS quota_message ON quota(message_id, state);
            PRAGMA user_version=1;
        """)
        with self.db:
            count, size = self.db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM messages WHERE mime IS NOT NULL").fetchone()
            previous = self._setting("sender", "")
            if previous not in ("", self.config.account.sender.casefold()) and count:
                raise ValueError("Cannot change sending mailbox while retained/unsent messages exist")
            self._set("sender", self.config.account.sender.casefold())
            for key, value in (("retained_messages", count), ("retained_bytes", size)):
                self.db.execute("INSERT OR REPLACE INTO counters VALUES (?,?)", (key, value))
            recovered = self.db.execute("SELECT COUNT(*) FROM messages WHERE status='sending'").fetchone()[0]
            self.db.execute("UPDATE messages SET status='uncertain', error='Process stopped during an upstream attempt; review before retry', updated=? WHERE status='sending'", (time.time(),))
            self.db.execute("UPDATE quota SET state='counted' WHERE state='reserved'")
            if recovered:
                self._count("uncertain", recovered)

    def _setting(self, key: str, default: str) -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def _count(self, key: str, value: int = 1, bucket: bool = True) -> None:
        self.db.execute("INSERT INTO counters VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value", (key, value))
        if bucket:
            self.db.execute("INSERT INTO buckets VALUES (?,?,?) ON CONFLICT(minute,name) DO UPDATE SET value=value+excluded.value", (int(time.time()) // 60 * 60, key, value))

    async def submit(self, message: PreparedMessage) -> str:
        if self.accepting is False or self.healthy is False:
            raise QueueUnavailable("Queue is not accepting messages")
        future = asyncio.get_running_loop().create_future()
        # Consume exceptions even when the SMTP connection disappears before ACK.
        future.add_done_callback(lambda f: f.exception() if f.cancelled() is False else None)
        try:
            self.pending.put_nowait(Submission(message, future))
        except asyncio.QueueFull:
            raise QueueUnavailable("Submission queue is full") from None
        return await asyncio.shield(future)

    async def _writer(self) -> None:
        stopping = False
        while stopping is False:
            first = await self.pending.get()
            if first is None:
                break
            batch = [first]
            await asyncio.sleep(self.config.queue.batch_seconds)
            while len(batch) < self.config.queue.batch_size:
                try:
                    item = self.pending.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    stopping = True
                    break
                batch.append(item)
            try:
                results = await self.call(self._insert_batch, [x.message for x in batch])
                for item, result in zip(batch, results):
                    if result is True:
                        item.future.set_result(item.message.id)
                    else:
                        item.future.set_exception(QueueUnavailable(result))
                self.wakeup.set()
            except Exception as exc:
                self.healthy = False
                logging.getLogger(__name__).error("Queue commit failed (%s); intake and delivery halted", type(exc).__name__)
                for item in batch:
                    item.future.set_exception(QueueUnavailable("Durable queue commit failed"))

    def _insert_batch(self, batch: list[PreparedMessage]) -> list[bool | str]:
        if self.healthy is False:
            return ["Queue needs operator attention"] * len(batch)
        if shutil.disk_usage(self.config.data_dir).free < self.config.queue.min_free_bytes + sum(len(m.mime) for m in batch) * 2:
            return ["Insufficient free disk space"] * len(batch)
        counters = dict(self.db.execute("SELECT key,value FROM counters WHERE key IN ('retained_messages','retained_bytes')"))
        retained = counters.get("retained_messages", 0)
        size = counters.get("retained_bytes", 0)
        results: list[bool | str] = []
        accepted = []
        now = time.time()
        for m in batch:
            if retained >= self.config.queue.max_messages or size + len(m.mime) > self.config.queue.max_bytes:
                results.append("Durable queue capacity exceeded")
                continue
            retained += 1
            size += len(m.mime)
            accepted.append(m)
            results.append(True)
        with self.db:
            self.db.executemany("""INSERT INTO messages
                (id,created,cycle_started,updated,status,next_attempt,envelope_sender,original_from,recipients,
                 recipient_count,subject,message_id,nat,size,mime)
                VALUES (?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?)""", [
                (m.id, now, now, now, now, m.envelope_sender, m.original_from, json.dumps(m.recipients), len(m.recipients), m.subject[:2000], m.message_id[:1000], int(m.nat), len(m.mime), m.mime)
                for m in accepted
            ])
            if accepted:
                self._count("accepted", len(accepted))
                self._count("nat", sum(m.nat for m in accepted))
                self._count("retained_messages", len(accepted), False)
                self._count("retained_bytes", sum(len(m.mime) for m in accepted), False)
        return results

    async def claim(self) -> dict | None:
        return await self.call(self._claim)

    def _claim(self) -> dict | None:
        now = time.time()
        with self.db:
            if self._setting("paused", "false") == "true" or float(self._setting("cooldown_until", "0")) > now:
                return None
            # Two bounded index seeks, rather than sorting all ready rows
            # (and potentially loading their MIME) for every submission.
            candidates = [self.db.execute("SELECT * FROM messages WHERE status=? AND next_attempt<=? ORDER BY next_attempt,created LIMIT 1", (state, now)).fetchone() for state in ("queued", "retry")]
            candidates = [row for row in candidates if row is not None]
            if not candidates:
                return None
            row = min(candidates, key=lambda item: (item["next_attempt"], item["created"]))
            if now - row["cycle_started"] > self.config.delivery.max_age_hours * 3600:
                self.db.execute("UPDATE messages SET status='failed',error='Queue maximum age exceeded',updated=? WHERE id=?", (now, row["id"]))
                self._count("failed")
                return None
            limit = self.config.delivery.recipient_limit_24h
            if limit:
                used = self.db.execute("SELECT COALESCE(SUM(recipients),0) FROM quota WHERE mailbox=? AND at>?", (self.config.account.sender.casefold(), now - 86400)).fetchone()[0]
                if used + row["recipient_count"] > limit:
                    return None
            self.db.execute("UPDATE messages SET status='sending',attempts=attempts+1,updated=? WHERE id=?", (now, row["id"]))
            self.db.execute("INSERT INTO quota(message_id,mailbox,at,recipients,state) VALUES (?,?,?,?,'reserved')", (row["id"], self.config.account.sender.casefold(), now, row["recipient_count"]))
            result = dict(row)
            result["attempts"] += 1
            return result

    async def finish(self, identifier: str, status: str, error: str = "", delay: float = 0, global_cooldown: bool = False) -> None:
        await self.call(self._finish, identifier, status, error, delay, global_cooldown)
        self.wakeup.set()

    def _finish(self, identifier: str, status: str, error: str, delay: float, global_cooldown: bool) -> None:
        if status not in {"submitted", "retry", "failed", "uncertain"}:
            raise ValueError("Invalid result state")
        now = time.time()
        with self.db:
            row = self.db.execute("SELECT status,size FROM messages WHERE id=?", (identifier,)).fetchone()
            if row is None or row["status"] != "sending":
                raise RuntimeError("Attempt is no longer in sending state")
            self.db.execute("UPDATE messages SET status=?,error=?,updated=?,next_attempt=? WHERE id=?", (status, error[:500], now, now + delay, identifier))
            if status == "submitted":
                self.db.execute("UPDATE messages SET mime=NULL WHERE id=?", (identifier,))
                self._count("retained_messages", -1, False)
                self._count("retained_bytes", -row["size"], False)
            if status in {"submitted", "uncertain"}:
                self.db.execute("UPDATE quota SET state='counted' WHERE message_id=? AND state='reserved'", (identifier,))
            else:
                self.db.execute("DELETE FROM quota WHERE message_id=? AND state='reserved'", (identifier,))
            self._count(status)
            if global_cooldown:
                until = max(now + delay, float(self._setting("cooldown_until", "0")))
                self._set("cooldown_until", str(until))

    async def settings(self) -> dict:
        return await self.call(lambda: dict(self.db.execute("SELECT key,value FROM settings")))

    async def set_setting(self, key: str, value: str) -> None:
        def update():
            with self.db:
                self._set(key, value)
        await self.call(update)
        self.wakeup.set()

    async def action(self, identifier: str, action: str, acknowledge_duplicate: bool = False) -> None:
        await self.call(self._action, identifier, action, acknowledge_duplicate)
        self.wakeup.set()

    def _action(self, identifier: str, action: str, acknowledge_duplicate: bool) -> None:
        with self.db:
            row = self.db.execute("SELECT * FROM messages WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("Unknown queue ID")
            if row["status"] in {"sending", "submitted", "cancelled"}:
                raise ValueError("This message cannot be changed in its current state")
            if action == "retry":
                if row["status"] == "uncertain" and acknowledge_duplicate is False:
                    raise ValueError("Retrying an uncertain send requires explicit duplicate-risk acknowledgement")
                # An explicit operator retry starts a fresh retry age/count budget.
                self.db.execute("UPDATE messages SET status='queued',attempts=0,cycle_started=?,updated=?,next_attempt=?,error='' WHERE id=?", (time.time(), time.time(), time.time(), identifier))
                self._count("manual_retry")
            elif action == "cancel":
                self.db.execute("UPDATE messages SET status='cancelled',mime=NULL,updated=? WHERE id=?", (time.time(), identifier))
                self._count("retained_messages", -1, False)
                self._count("retained_bytes", -row["size"], False)
                self._count("cancelled")
            else:
                raise ValueError("Unknown action")

    async def messages(self, status: str = "", limit: int = 100, before: float | None = None, before_id: str = "") -> list[dict]:
        def read():
            conditions, values = [], []
            if status:
                conditions.append("status=?")
                values.append(status)
            if before is not None:
                conditions.append("(created<? OR (created=? AND id<?))")
                values.extend((before, before, before_id))
            where = " WHERE " + " AND ".join(conditions) if conditions else ""
            sql = "SELECT id,created,updated,status,attempts,original_from,recipient_count,subject,message_id,nat,size,error,next_attempt FROM messages" + where + " ORDER BY created DESC,id DESC LIMIT ?"
            return [dict(r) for r in self.db.execute(sql, (*values, min(max(limit, 1), 200)))]
        return await self.call(read)

    async def export(self, identifier: str) -> bytes | None:
        def read():
            row = self.db.execute("SELECT mime FROM messages WHERE id=?", (identifier,)).fetchone()
            return row[0] if row else None
        return await self.call(read)

    async def stats(self) -> dict:
        def read():
            now = time.time()
            return {
                "counters": dict(self.db.execute("SELECT key,value FROM counters")),
                "states": dict(self.db.execute("SELECT status,COUNT(*) FROM messages GROUP BY status")),
                "buckets": [dict(r) for r in self.db.execute("SELECT minute,name,value FROM buckets WHERE minute>=? AND name IN ('accepted','submitted','retry','failed','uncertain') ORDER BY minute", (int(now) // 60 * 60 - 3540,))],
                "oldest_pending_seconds": self.db.execute("SELECT COALESCE(?-MIN(created),0) FROM messages WHERE status IN ('queued','retry','sending')", (now,)).fetchone()[0],
                "recipient_budget_used_24h": self.db.execute("SELECT COALESCE(SUM(recipients),0) FROM quota WHERE mailbox=? AND at>?", (self.config.account.sender.casefold(), now - 86400)).fetchone()[0],
                "disk_free_bytes": shutil.disk_usage(self.config.data_dir).free,
                "database_bytes": sum(p.stat().st_size for p in self.config.data_dir.glob("queue.sqlite3*")),
                "healthy": self.healthy,
            }
        return await self.call(read)

    async def housekeeping(self) -> None:
        def cleanup():
            now = time.time()
            with self.db:
                self.db.execute("DELETE FROM messages WHERE id IN (SELECT id FROM messages WHERE status IN ('submitted','cancelled') AND updated<? LIMIT 10000)", (now - self.config.queue.history_days * 86400,))
                self.db.execute("DELETE FROM buckets WHERE minute<?", (now - self.config.queue.history_days * 86400,))
                self.db.execute("DELETE FROM quota WHERE at<? AND state='counted'", (now - 86400,))
            self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
        await self.call(cleanup)

    async def close(self) -> None:
        self.accepting = False
        if self.writer is not None:
            await self.pending.put(None)
            await self.writer
        if self.db is not None:
            await self.call(self.db.close)
        self.executor.shutdown(wait=True)
