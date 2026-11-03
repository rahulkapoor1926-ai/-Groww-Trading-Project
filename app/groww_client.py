"""Groww login (TOTP flow) and a cached API client.

Groww access tokens expire every day at 6:00 AM IST, so we generate one on
demand from the TOTP key and cache it in memory until then.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pyotp
from growwapi import GrowwAPI

IST = timezone(timedelta(hours=5, minutes=30))
TOKEN_RESET = time(6, 0)


class GrowwConfigError(RuntimeError):
    """Raised when the API key or TOTP secret is missing or malformed."""


_BASE32 = re.compile(r"^[A-Z2-7]+=*$")


def normalize_totp_secret(raw: str) -> str:
    """Clean a TOTP secret pasted from Groww into a valid base32 string.

    Accepts the secret with spaces, tabs, newlines, dashes, surrounding quotes
    or lowercase letters, and also a full ``otpauth://`` URI (the text behind
    the QR code). Raises GrowwConfigError with a description of the problem
    that never includes the secret itself.
    """
    s = (raw or "").strip().strip("\"'").strip()
    if s.lower().startswith("otpauth://"):
        s = (parse_qs(urlparse(s).query).get("secret") or [""])[0]
    s = re.sub(r"[\s\-]", "", s).upper()
    if not s:
        raise GrowwConfigError("GROWW_TOTP_SECRET is empty.")
    if s.startswith("EYJ"):
        raise GrowwConfigError(
            "GROWW_TOTP_SECRET looks like an API key (a long token starting "
            "with 'eyJ'), not a TOTP secret. The two secrets may be swapped."
        )
    if not _BASE32.match(s):
        bad = sorted(
            {c for c in s.rstrip("=") if not ("A" <= c <= "Z" or "2" <= c <= "7")}
        )
        kinds = []
        if any(c in "0189" for c in bad):
            kinds.append("the digits 0, 1, 8 or 9")
        if any(not c.isalnum() for c in bad):
            kinds.append("symbols such as . _ / + or quotes")
        raise GrowwConfigError(
            f"GROWW_TOTP_SECRET is not a valid base32 TOTP secret ({len(s)} "
            f"characters; it contains {' and '.join(kinds) or 'invalid characters'}). "
            "A TOTP secret only uses the letters A-Z and the digits 2-7."
        )
    return s


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
        key = self.api_key.strip().strip("\"'")
        if len(key) < 64 and _BASE32.match(re.sub(r"[\s\-]", "", key).upper()):
            raise GrowwConfigError(
                "GROWW_API_KEY looks like a TOTP secret (short, letters A-Z and "
                "digits 2-7). The two secrets may be swapped."
            )
        secret = normalize_totp_secret(self.totp_secret)
        with self._lock:
            totp = pyotp.TOTP(secret).now()
            token = GrowwAPI.get_access_token(api_key=key, totp=totp)
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
