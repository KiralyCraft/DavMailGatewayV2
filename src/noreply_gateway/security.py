from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet


def atomic_write(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class InstanceLock:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.stat().st_mode & 0o077:
            raise ValueError("State directory must have permissions 0700")
        self.file = (directory / "instance.lock").open("a+b")
        os.fchmod(self.file.fileno(), 0o600)
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError("Another gateway or administrative command is using this state directory") from None

    def close(self) -> None:
        self.file.close()


class Vault:
    def __init__(self, directory: Path, create: bool = False):
        self.directory = directory
        key_path = directory / "vault.key"
        if create and key_path.exists() is False:
            atomic_write(key_path, Fernet.generate_key())
        if key_path.exists() is False:
            raise ValueError("Gateway is not initialized: run noreply-gateway init")
        if key_path.stat().st_mode & 0o077:
            raise ValueError("vault.key must have permissions 0600")
        self.cipher = Fernet(key_path.read_bytes())

    def read(self, name: str) -> dict:
        path = self.directory / (name + ".enc")
        if path.exists() is False:
            return {}
        return json.loads(self.cipher.decrypt(path.read_bytes()))

    def write(self, name: str, value: dict) -> None:
        data = json.dumps(value, separators=(",", ":")).encode()
        atomic_write(self.directory / (name + ".enc"), self.cipher.encrypt(data))


def hash_password(password: str) -> dict[str, str]:
    if len(password) < 12:
        raise ValueError("Use an administration password of at least 12 characters")
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return {"salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(digest).decode()}


def verify_password(password: str, stored: dict) -> bool:
    if len(password) > 1024:
        return False
    try:
        salt = base64.b64decode(stored["salt"], validate=True)
        expected = base64.b64decode(stored["hash"], validate=True)
        actual = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except (KeyError, ValueError):
        return False
