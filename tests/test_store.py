import asyncio
import sqlite3
import time

import pytest

from noreply_gateway.store import QueueUnavailable, Store


async def test_atomic_batch_capacity(store, config, message):
    config.queue.max_messages = 2
    results = await asyncio.gather(*(store.submit(message()) for _ in range(4)), return_exceptions=True)
    assert sum(isinstance(x, str) for x in results) == 2
    assert sum(isinstance(x, QueueUnavailable) for x in results) == 2
    assert (await store.stats())["counters"]["accepted"] == 2
    assert await store.call(lambda: store.db.execute("PRAGMA synchronous").fetchone()[0]) == 2


async def test_completed_body_removed_but_metadata_retained(store, message):
    identifier = await store.submit(message())
    attempt = await store.claim()
    assert attempt["id"] == identifier
    await store.finish(identifier, "submitted")
    assert await store.export(identifier) is None
    stats = await store.stats()
    assert stats["states"] == {"submitted": 1}
    assert stats["counters"]["retained_bytes"] == 0
    assert stats["recipient_budget_used_24h"] == 1


async def test_message_id_is_not_a_deduplication_key(store, message):
    first = message(extra=b"Message-ID: <reused@example.test>\r\n")
    second = message(extra=b"Message-ID: <reused@example.test>\r\n")
    assert await store.submit(first) != await store.submit(second)
    assert (await store.stats())["counters"]["accepted"] == 2


async def test_pagination_does_not_skip_same_commit_timestamp(store, message):
    await asyncio.gather(*(store.submit(message()) for _ in range(15)))
    seen, before, before_id = [], None, ""
    while True:
        page = await store.messages(limit=4, before=before, before_id=before_id)
        if not page:
            break
        seen.extend(row["id"] for row in page)
        before, before_id = page[-1]["created"], page[-1]["id"]
    assert len(seen) == len(set(seen)) == 15


async def test_retry_and_uncertain_quota(store, config, message):
    config.delivery.recipient_limit_24h = 1
    a = await store.submit(message())
    b = await store.submit(message())
    assert (await store.claim())["id"] == a
    assert await store.claim() is None
    await store.finish(a, "retry", delay=3600)
    assert (await store.claim())["id"] == b
    await store.finish(b, "uncertain")
    with pytest.raises(ValueError, match="acknowledgement"):
        await store.action(b, "retry")
    original = next(row for row in await store.messages() if row["id"] == b)["created"]
    await store.action(b, "retry", True)
    assert next(row for row in await store.messages() if row["id"] == b)["created"] == original
    assert await store.claim() is None  # An uncertain attempt still consumes budget.
    await store.action(b, "cancel")
    assert await store.export(b) is None


async def test_crash_recovery_holds_inflight(config, message):
    first = Store(config)
    await first.start()
    identifier = await first.submit(message())
    await first.claim()
    await first.close()  # Equivalent persisted state to abrupt exit mid-attempt.
    second = Store(config)
    try:
        await second.start()
        assert (await second.messages())[0]["status"] == "uncertain"
        assert await second.export(identifier)
        assert (await second.stats())["recipient_budget_used_24h"] == 1
    finally:
        await second.close()


async def test_db_failure_is_not_acknowledged(store, message, monkeypatch):
    def fail(batch):
        raise sqlite3.OperationalError("simulated full disk")
    monkeypatch.setattr(store, "_insert_batch", fail)
    with pytest.raises(QueueUnavailable):
        await store.submit(message())
    assert store.healthy is False


async def test_pause_cooldown_age_and_history(store, config, message):
    identifier = await store.submit(message())
    await store.set_setting("paused", "true")
    assert await store.claim() is None
    await store.set_setting("paused", "false")
    await store.set_setting("cooldown_until", str(time.time() + 60))
    assert await store.claim() is None
    await store.set_setting("cooldown_until", "0")
    config.delivery.max_age_hours = 1e-10
    await asyncio.sleep(.001)
    assert await store.claim() is None
    assert (await store.messages())[0]["status"] == "failed"
    await store.action(identifier, "cancel")
    config.queue.history_days = 0
    await asyncio.sleep(.001)
    await store.housekeeping()
    assert await store.messages() == []
    assert (await store.stats())["counters"]["accepted"] == 1


async def test_ready_selection_uses_index_without_full_queue_sort(store):
    def query_plan():
        return [row[3] for row in store.db.execute("EXPLAIN QUERY PLAN SELECT * FROM messages WHERE status=? AND next_attempt<=? ORDER BY next_attempt,created LIMIT 1", ("queued", time.time()))]
    plan = " ".join(await store.call(query_plan))
    assert "messages_ready" in plan
    assert "TEMP B-TREE" not in plan
