from __future__ import annotations

import asyncio
from copy import deepcopy
from email import policy
from email.parser import BytesHeaderParser

from .backend import MicrosoftBackend
from .config import Config
from .errors import AuthenticationRequired
from .message import mailbox
from .oauth import TokenManager, account_fingerprint
from .security import Vault


class AdditionalAuthenticationRequired(AuthenticationRequired):
    pass


class AdditionalSender:
    """Optional, separately authenticated Graph sender alongside the original account."""

    def __init__(self, config: Config, vault: Vault, session, original_backend: MicrosoftBackend):
        self.config = config
        self.vault = vault
        self.original_backend = original_backend
        additional_config = deepcopy(config)
        additional_config.account.backend = "graph"
        additional_config.account.send_shared = True
        additional_config.account.save_in_sent = True
        for name in ("tenant_id", "client_id", "redirect_uri", "client_secret_env"):
            value = getattr(config.additional, name)
            if value:
                setattr(additional_config.account, name, value)
        legacy_fingerprint = account_fingerprint(additional_config)
        additional_config.account.sender = ""
        additional_config.account.login_username = ""
        record = vault.read("additional_account")
        if record.get("fingerprint") == legacy_fingerprint and legacy_fingerprint != account_fingerprint(additional_config):
            record["fingerprint"] = account_fingerprint(additional_config)
            vault.write("additional_account", record)
        self.tokens = TokenManager(additional_config, vault, session, record_name="additional_account", allow_any_username=True)
        self.backend = MicrosoftBackend(additional_config, self.tokens, session)
        self.choice = vault.read("additional_sender")
        if self.choice.get("username") != self.tokens.record.get("username") or self.choice.get("mode") not in {"default", "custom"}:
            self.choice = {}
        if self.choice.get("mode") == "custom":
            try:
                mailbox(self.choice.get("custom_sender", ""))
            except ValueError:
                self.choice = {}

    @property
    def default_sender(self) -> str:
        return self.tokens.record.get("default_sender", "") if self.tokens.status()["credential_present"] else ""

    @property
    def selected_sender(self) -> str:
        if not self.choice or self.tokens.status()["needs_login"]:
            return ""
        return self.default_sender if self.choice.get("mode") == "default" else self.choice.get("custom_sender", "")

    def status(self) -> dict:
        return {"account": self.tokens.status(), "default_sender": self.default_sender,
                "selected_sender": self.selected_sender, "mode": self.choice.get("mode", ""),
                "choice_needed": bool(self.default_sender and not self.selected_sender)}

    async def completed_login(self) -> None:
        self.choice = {}
        await self._save_choice()

    async def disconnect(self) -> None:
        await self.tokens.disconnect()
        self.choice = {}
        await self._save_choice()

    async def choose(self, mode: str, custom_sender: str = "") -> str:
        if not self.default_sender:
            raise ValueError("Connect a Microsoft account first")
        if mode not in {"default", "custom"}:
            raise ValueError("Choose the default or a custom address")
        address = self.default_sender if mode == "default" else mailbox(custom_sender)
        if address.casefold() == self.config.account.sender.casefold():
            raise ValueError("The original gateway address already uses the existing connection")
        self.choice = {"username": self.tokens.record["username"], "mode": mode,
                       "custom_sender": address if mode == "custom" else ""}
        await self._save_choice()
        return address

    async def _save_choice(self) -> None:
        await asyncio.to_thread(self.vault.write, "additional_sender", self.choice)

    def is_additional(self, message: dict) -> bool:
        header = BytesHeaderParser(policy=policy.SMTP).parsebytes(message["mime"].partition(b"\r\n\r\n")[0] + b"\r\n\r\n")["From"]
        return header.addresses[0].addr_spec.casefold() != self.config.account.sender.casefold()

    async def send(self, message: dict) -> None:
        if not self.is_additional(message):
            await self.original_backend.send(message)
            return
        if not self.tokens.status()["credential_present"]:
            raise AdditionalAuthenticationRequired("Additional Microsoft account is not connected")
        try:
            await self.backend.send(message)
        except AuthenticationRequired as exc:
            raise AdditionalAuthenticationRequired(str(exc)) from None
