from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .config import Config, load_config
from .migration import apply_davmail, imported_record, read_properties
from .security import InstanceLock, Vault, atomic_write, hash_password
from .service import serve


def config_text(config: Config) -> str:
    result = ["# SMTP has no AUTH. Restrict trusted clients with CIDRs AND a firewall.", "# The WebUI password is separate. Never put Microsoft tokens in this file.", "data_dir = " + json.dumps(str(config.data_dir)), ""]
    for section in ("smtp", "web", "account", "queue", "delivery"):
        result.append("[" + section + "]")
        for key, value in asdict(getattr(config, section)).items():
            result.append(key + " = " + json.dumps(value, ensure_ascii=True))
        result.append("")
    return "\n".join(result)


def password_from(args) -> str:
    if args.admin_password_env:
        value = os.environ.get(args.admin_password_env)
        if value is None:
            raise ValueError("Administration password environment variable is unset")
        return value
    password = getpass.getpass("New WebUI administration password (12+ characters): ")
    if password != getpass.getpass("Repeat administration password: "):
        raise ValueError("Passwords differ")
    return password


def initialize(args) -> None:
    path = args.config.expanduser().resolve()
    if path.exists():
        raise ValueError("Configuration already exists; initialization does not overwrite it")
    config = Config(data_dir=(args.data_dir.expanduser().resolve() if args.data_dir else path.parent / "state"))
    properties = read_properties(args.from_davmail) if args.from_davmail else None
    if properties is not None:
        apply_davmail(config, properties)
    for name in ("sender", "backend", "tenant_id", "client_id", "redirect_uri"):
        value = getattr(args, name)
        if value is not None:
            setattr(config.account, name, value)
    config.validate()
    record = None
    if args.import_token:
        if properties is None:
            raise ValueError("--import-token requires --from-davmail")
        record = imported_record(config, properties)
    password = hash_password(password_from(args))
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(config.data_dir)
    try:
        if (config.data_dir / "vault.key").exists() or (config.data_dir / "queue.sqlite3").exists():
            raise ValueError("State directory is already initialized; use a new directory")
        vault = Vault(config.data_dir, create=True)
        vault.write("admin", password)
        if record is not None:
            vault.write("account", record)
        atomic_write(path, config_text(config).encode())
    finally:
        lock.close()
    print("Created " + str(path))
    print("SMTP is passwordless and loopback-only until trusted CIDRs and bind settings are configured.")
    print("Imported an encrypted legacy credential; it has NOT been validated online." if record else "Sign into Microsoft from the WebUI after starting the service.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send-only Microsoft 365 SMTP gateway")
    parser.add_argument("--version", action="version", version="%(prog)s 0.2.0")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create private state, configuration, and admin password")
    init.add_argument("--config", type=Path, default=Path("gateway.toml"))
    init.add_argument("--data-dir", type=Path)
    init.add_argument("--from-davmail", type=Path)
    init.add_argument("--import-token", action="store_true", help="Explicitly import this patch's local base64 refresh token into the encrypted vault")
    init.add_argument("--admin-password-env", help="Read the WebUI password from this environment variable instead of prompting")
    init.add_argument("--sender")
    init.add_argument("--backend", choices=("ews", "graph"))
    init.add_argument("--tenant-id")
    init.add_argument("--client-id")
    init.add_argument("--redirect-uri")
    for command, help_text in (("serve", "Run SMTP, administration, and delivery workers"), ("check", "Validate configuration and private state without networking"), ("reset-admin", "Replace the administration password; service must be stopped")):
        sub = commands.add_parser(command, help=help_text)
        sub.add_argument("--config", type=Path, default=Path("gateway.toml"))
        if command == "reset-admin":
            sub.add_argument("--admin-password-env")
    args = parser.parse_args(argv)
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.command == "init":
            initialize(args)
        else:
            config = load_config(args.config)
            if args.command == "serve":
                asyncio.run(serve(config))
            else:
                lock = InstanceLock(config.data_dir)
                try:
                    vault = Vault(config.data_dir)
                    if args.command == "reset-admin":
                        vault.write("admin", hash_password(password_from(args)))
                        print("Administration password replaced.")
                    else:
                        if not vault.read("admin"):
                            raise ValueError("Administration credentials are missing")
                        vault.read("account")  # Detect an unreadable vault without exposing its contents.
                        vault.read("additional_account")
                        vault.read("additional_sender")
                        print("Configuration and private state are readable. No network request or mailbox verification was performed.")
                finally:
                    lock.close()
    except (ValueError, RuntimeError, OSError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Avoid printing encrypted records, HTTP arguments, or credential-bearing tracebacks.
        print("Operation failed (" + type(exc).__name__ + "). Check configuration and state permissions.", file=sys.stderr)
        return 1
    return 0
