import uuid

import pytest

from noreply_gateway.config import Config
from noreply_gateway.message import prepare_message
from noreply_gateway.security import Vault
from noreply_gateway.store import Store


@pytest.fixture
def config(tmp_path):
    tmp_path.chmod(0o700)
    result = Config(data_dir=tmp_path)
    result.account.sender = "gateway@example.test"
    result.queue.min_free_bytes = 0
    result.smtp.port = 0  # Ephemeral loopback test server, not a production config.
    result.delivery.messages_per_second = 1000
    return result


@pytest.fixture
def vault(config):
    return Vault(config.data_dir, create=True)


@pytest.fixture
async def store(config):
    instance = Store(config)
    await instance.start()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
def message(config):
    def create(extra=b"", sender=None, recipients=None, body=b"hello\r\n", subject=b"Subject: Example\r\n"):
        raw = b"From: " + (sender or config.account.sender).encode() + b"\r\nTo: recipient@example.test\r\n" + subject + extra + b"\r\n" + body
        return prepare_message(raw, recipients or ["recipient@example.test"], "ignored-envelope@example.test", str(uuid.uuid4()), config)
    return create
