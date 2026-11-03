"""Groww login (TOTP flow) and a cached API client.

Groww access tokens expire every day at 6:00 AM IST, so we generate one on
demand from the TOTP key and cache it in memory until then.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

import pyotp
from growwapi import GrowwAPI

IST = timezone(timedelta(hours=5, minutes=30))
TOKEN_RESET = time(6, 0)


class GrowwConfigError(RuntimeError):
    """Raised when the API key or TOTP secret is missing."""


def next_token_expiry(now: datetime) -> datetime:
    """Return the next 6:00 AM IST after `now`."""
    now = now.astimezone(IST)
    expiry = datetime.combine(now.date(), TOKEN_RESET, tzinfo=IST)
    if now >= expiry:
        expiry += timedelta(days=1)
    return expiry


@dataclass
class GrowwSession:
    api_key: str | None
    totp_secret: str | None
    _client: GrowwAPI | None = field(default=None, repr=False)
    _expires_at: datetime | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def from_env(cls) -> GrowwSession:
        return cls(
            api_key=(os.getenv("GROWW_API_KEY") or "").strip() or None,
            totp_secret=(os.getenv("GROWW_TOTP_SECRET") or "").strip() or None,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.totp_secret)

    @property
    def logged_in(self) -> bool:
        return self._client is not None and not self._expired()

    @property
    def expires_at(self) -> datetime | None:
        return self._expires_at if self.logged_in else None

    def _expired(self) -> bool:
        return self._expires_at is None or datetime.now(IST) >= self._expires_at

    def login(self) -> GrowwAPI:
        """Exchange API key + current TOTP code for an access token."""
        if not self.configured:
            raise GrowwConfigError(
                "GROWW_API_KEY and GROWW_TOTP_SECRET must be set in your .env file."
            )
        with self._lock:
            totp = pyotp.TOTP(self.totp_secret.replace(" ", "")).now()
            token = GrowwAPI.get_access_token(api_key=self.api_key, totp=totp)
            self._client = GrowwAPI(token)
            self._expires_at = next_token_expiry(datetime.now(IST))
            return self._client

    def logout(self) -> None:
        with self._lock:
            self._client = None
            self._expires_at = None

    def client(self) -> GrowwAPI:
        """Return a logged-in client, logging in again if the token expired."""
        if self.logged_in:
            return self._client
        return self.login()
