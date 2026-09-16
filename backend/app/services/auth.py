"""Authentication: JWT access tokens, rotating refresh tokens, Telegram initData.

Three mechanisms share one dependency, ``current_user``:

**Web / Android** — Argon2id (or scrypt fallback) password, short-lived JWT access
token, opaque rotating refresh token.

**Telegram Mini App** — ``initData`` verified by HMAC-SHA256 against a key derived
from the bot token.

The refresh-token design is the interesting part. Tokens are stored *hashed* and
grouped into **families**. When a token is rotated it is marked used. If a token
that has already been used is presented again, that means it was stolen and
replayed — so the entire family is revoked, logging out both the thief and the
victim. Detecting theft by making it break the session is strictly better than
silently allowing the attacker to keep refreshing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any

import jwt as pyjwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core import crypto
from app.core.config import Settings, get_settings
from app.core.errors import (
    ProblemError,
    forbidden,
    invalid_credentials,
    invalid_init_data,
    refresh_reuse_detected,
    token_expired,
    unauthorized,
)

log = logging.getLogger(__name__)

# Telegram's own guidance is that initData should be treated as short-lived;
# the default window is generous enough for clock skew but not for replay.
INIT_DATA_MAX_AGE_SECONDS = 3600

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller."""

    user_id: str
    email: str | None
    role: str
    display_name: str


@dataclass(frozen=True, slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "Bearer"


class AuthService:
    def __init__(self, *, repository: Any, settings: Settings) -> None:
        self.repo = repository
        self.settings = settings

    # -- password flows ---------------------------------------------------

    async def register(
        self, *, email: str, password: str, display_name: str = ""
    ) -> dict[str, Any]:
        existing = await self.repo.get_user_by_email(email)
        if existing is not None:
            # Deliberately vague: a distinct "email already registered" response
            # is a user-enumeration oracle.
            raise ProblemError(
                400, "invalid_credentials", "Registration failed for the supplied details"
            )

        self._assert_password_strength(password)
        password_hash = crypto.hash_password(password)
        try:
            user = await self.repo.create_user(
                email=email,
                password_hash=password_hash,
                display_name=display_name or email.split("@")[0],
            )
        except ProblemError as exc:
            # A unique-constraint race can still lose here (two concurrent
            # registrations for the same address), so the same vague error is
            # returned rather than surfacing a conflict code.
            if exc.code == "email_conflict":
                raise ProblemError(
                    400, "invalid_credentials", "Registration failed for the supplied details"
                ) from exc
            raise

        # Provision the per-user DEK, wrapped under the server KEK. The wrapped
        # form is all that is stored; the plaintext DEK never touches the
        # database, and is re-derived as needed.
        dek = crypto.generate_dek()
        wrapped = crypto.wrap_dek(
            dek, self.settings.master_kek, user_id_bytes=uuid.UUID(user["id"]).bytes
        )
        await self.repo.set_user_wrapped_dek(user["id"], wrapped, version=1)

        await self.repo.audit(
            actor_user_id=user["id"], action="auth.register", outcome="success"
        )
        return user

    async def login(
        self, *, email: str, password: str, user_agent: str | None = None, ip: str | None = None
    ) -> tuple[dict[str, Any], TokenPair]:
        user = await self.repo.get_user_by_email(email)

        # Verify against a dummy hash when the user does not exist, so that the
        # response time does not reveal whether the account is real.
        if user is None:
            crypto.verify_password(password, crypto.hash_password("dummy"))
            raise invalid_credentials()

        if user["status"] != "active":
            raise forbidden("This account is not active")

        if not user["password_hash"] or not crypto.verify_password(
            password, user["password_hash"]
        ):
            await self.repo.audit(
                actor_user_id=user["id"], action="auth.login", outcome="failure"
            )
            raise invalid_credentials()

        # Opportunistically upgrade the stored hash when policy has moved on.
        if crypto.needs_rehash(user["password_hash"]):
            try:
                new_hash = crypto.hash_password(password)
                await self.repo.update_password_hash(user["id"], new_hash)
            except Exception:  # noqa: BLE001 - never fail a login over an upgrade
                log.warning("password rehash failed for %s", user["id"], exc_info=True)

        pair = await self._issue_tokens(user, user_agent=user_agent, ip=ip)
        await self.repo.audit(actor_user_id=user["id"], action="auth.login", outcome="success")
        return user, pair

    async def refresh(
        self, *, refresh_token: str, user_agent: str | None = None, ip: str | None = None
    ) -> TokenPair:
        token_hash = _hash_token(refresh_token)
        record = await self.repo.get_refresh_token(token_hash)

        if record is None:
            raise unauthorized("Refresh token is not recognised")

        if record["revoked_at"] is not None:
            raise unauthorized("Refresh token has been revoked")

        if _as_epoch(record["expires_at"]) < time.time():
            raise token_expired("Refresh token has expired")

        if record["used_at"] is not None:
            # Reuse of an already-rotated token means the token leaked. Revoke
            # the whole family so the attacker's stolen session dies with ours.
            await self.repo.revoke_token_family(record["family_id"])
            await self.repo.audit(
                actor_user_id=record["user_id"],
                action="auth.refresh",
                outcome="denied",
                detail={"reason": "token_reuse_detected", "family": record["family_id"]},
            )
            log.error(
                "refresh token reuse detected for user %s; family %s revoked",
                record["user_id"],
                record["family_id"],
            )
            raise refresh_reuse_detected()

        user = await self.repo.get_user(record["user_id"])
        if user is None or user["status"] != "active":
            raise unauthorized("Account is not active")

        new_token = secrets.token_urlsafe(48)
        await self.repo.rotate_refresh_token(
            old_hash=token_hash,
            new_hash=_hash_token(new_token),
            family_id=record["family_id"],
            user_id=user["id"],
            expires_at=_epoch_to_dt(time.time() + self.settings.jwt_refresh_ttl_seconds),
            user_agent=user_agent,
            ip=ip,
        )
        return TokenPair(
            access_token=self._make_access_token(user),
            refresh_token=new_token,
            expires_in=self.settings.jwt_access_ttl_seconds,
        )

    async def logout(self, *, refresh_token: str) -> None:
        record = await self.repo.get_refresh_token(_hash_token(refresh_token))
        if record is not None:
            # Revoking the family logs out every device in that lineage, which is
            # the expected meaning of "log out" for a rotated-token scheme.
            await self.repo.revoke_token_family(record["family_id"])

    # -- Telegram Mini App -------------------------------------------------

    async def login_with_telegram(
        self, *, init_data: str, user_agent: str | None = None, ip: str | None = None
    ) -> tuple[dict[str, Any], TokenPair]:
        """Authenticate a Mini App user from ``Telegram.WebApp.initData``."""
        fields = self._verify_init_data(init_data)

        raw_user = fields.get("user")
        if not raw_user:
            raise invalid_init_data("initData does not contain a user object")

        try:
            tg_user = json.loads(raw_user)
        except json.JSONDecodeError as exc:
            raise invalid_init_data("initData user field is not valid JSON") from exc

        telegram_user_id = tg_user.get("id")
        if not isinstance(telegram_user_id, int):
            raise invalid_init_data("initData user id is missing or malformed")

        user = await self.repo.get_user_by_telegram_id(telegram_user_id)
        if user is None:
            # First Mini App login provisions the account.
            username = tg_user.get("username")
            display = " ".join(
                part for part in (tg_user.get("first_name"), tg_user.get("last_name")) if part
            )
            user = await self.repo.create_user(
                email=None,
                password_hash=None,
                display_name=display or username or f"tg-{telegram_user_id}",
                telegram_user_id=telegram_user_id,
                telegram_username=username,
            )
            dek = crypto.generate_dek()
            wrapped = crypto.wrap_dek(
                dek, self.settings.master_kek, user_id_bytes=uuid.UUID(user["id"]).bytes
            )
            await self.repo.set_user_wrapped_dek(user["id"], wrapped, version=1)

        if user["status"] != "active":
            raise forbidden("This account is not active")

        pair = await self._issue_tokens(user, user_agent=user_agent, ip=ip)
        await self.repo.audit(
            actor_user_id=user["id"], action="auth.telegram_miniapp", outcome="success"
        )
        return user, pair

    def _verify_init_data(self, init_data: str) -> dict[str, str]:
        """Validate the ``initData`` HMAC.

        Telegram's scheme: the secret is ``HMAC_SHA256("WebAppData", bot_token)`` —
        note the unusual argument order, with the literal string as the *key* and
        the bot token as the *message*. The check string is every field except
        ``hash``, sorted by key, joined with newlines as ``key=value``.
        """
        if not self.settings.telegram_bot_token:
            raise invalid_init_data(
                "Telegram Mini App login is not configured (TELEGRAM_BOT_TOKEN is unset)"
            )
        if not init_data:
            raise invalid_init_data("initData is empty")

        try:
            parsed = dict(urllib.parse.parse_qsl(init_data, strict_parsing=True))
        except ValueError as exc:
            raise invalid_init_data("initData is not a valid query string") from exc

        received_hash = parsed.pop("hash", None)
        if not received_hash:
            raise invalid_init_data("initData is missing the hash field")

        data_check_string = "\n".join(
            f"{key}={parsed[key]}" for key in sorted(parsed)
        )

        secret_key = hmac.new(
            b"WebAppData",
            self.settings.telegram_bot_token.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        expected = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        # Constant-time comparison: a timing-variable check here would let an
        # attacker forge a signature byte by byte.
        if not crypto.constant_time_equals(expected, received_hash):
            raise invalid_init_data("initData signature does not match")

        # Freshness. Without this a captured initData string is a permanent
        # credential, since the HMAC never expires on its own.
        auth_date = parsed.get("auth_date")
        if auth_date:
            try:
                age = time.time() - int(auth_date)
            except ValueError as exc:
                raise invalid_init_data("auth_date is not a valid integer") from exc
            if age > INIT_DATA_MAX_AGE_SECONDS:
                raise invalid_init_data(
                    f"initData is too old ({int(age)}s); it must be used within "
                    f"{INIT_DATA_MAX_AGE_SECONDS}s"
                )
            if age < -300:
                raise invalid_init_data("initData auth_date is in the future")

        return parsed

    # -- tokens -----------------------------------------------------------

    def _make_access_token(self, user: dict[str, Any]) -> str:
        now = int(time.time())
        payload = {
            "sub": user["id"],
            "email": user["email"],
            "role": user["role"],
            "iss": self.settings.jwt_issuer,
            "aud": self.settings.jwt_audience,
            "iat": now,
            "nbf": now,
            "exp": now + self.settings.jwt_access_ttl_seconds,
            "jti": str(uuid.uuid4()),
            "typ": "access",
        }
        return pyjwt.encode(
            payload, self.settings.jwt_secret, algorithm=self.settings.jwt_algorithm
        )

    def create_download_token(self, user: dict[str, Any], ttl_seconds: int = 86400 * 7) -> str:
        """Create a signed access token suitable for direct download links in browser/bot."""
        now = int(time.time())
        payload = {
            "sub": user["id"],
            "email": user.get("email"),
            "role": user.get("role", "user"),
            "iss": self.settings.jwt_issuer,
            "aud": self.settings.jwt_audience,
            "iat": now,
            "nbf": now,
            "exp": now + ttl_seconds,
            "jti": str(uuid.uuid4()),
            "typ": "access",
        }
        return pyjwt.encode(
            payload, self.settings.jwt_secret, algorithm=self.settings.jwt_algorithm
        )

    async def _issue_tokens(
        self, user: dict[str, Any], *, user_agent: str | None, ip: str | None
    ) -> TokenPair:
        refresh_token = secrets.token_urlsafe(48)
        await self.repo.store_refresh_token(
            token_hash=_hash_token(refresh_token),
            family_id=str(uuid.uuid4()),
            user_id=user["id"],
            expires_at=_epoch_to_dt(time.time() + self.settings.jwt_refresh_ttl_seconds),
            user_agent=user_agent,
            ip=ip,
        )
        return TokenPair(
            access_token=self._make_access_token(user),
            refresh_token=refresh_token,
            expires_in=self.settings.jwt_access_ttl_seconds,
        )

    def decode_access_token(self, token: str) -> Principal:
        try:
            payload = pyjwt.decode(
                token,
                self.settings.jwt_secret,
                algorithms=[self.settings.jwt_algorithm],
                audience=self.settings.jwt_audience,
                issuer=self.settings.jwt_issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except pyjwt.ExpiredSignatureError as exc:
            raise token_expired() from exc
        except pyjwt.InvalidTokenError as exc:
            raise unauthorized(f"Access token is invalid: {exc}") from exc

        # Reject a refresh token presented as an access token: without this the
        # long-lived credential would be usable everywhere the short one is.
        if payload.get("typ") != "access":
            raise unauthorized("Token is not an access token")

        return Principal(
            user_id=payload["sub"],
            email=payload.get("email"),
            role=payload.get("role", "user"),
            display_name=payload.get("email") or payload["sub"],
        )

    @staticmethod
    def _assert_password_strength(password: str) -> None:
        if len(password) < 12:
            raise invalid_credentials("Password must be at least 12 characters")
        if len(password) > 1024:
            # Bounded because hashing is expensive: an unbounded password is a
            # cheap denial-of-service vector against a memory-hard KDF.
            raise invalid_credentials("Password must be at most 1024 characters")


def _hash_token(token: str) -> bytes:
    """Hash a refresh token for storage.

    A plain SHA-256 is correct here, unlike for passwords: the token is 48 bytes
    of CSPRNG output, so it has full entropy and there is nothing for an attacker
    to brute-force. A slow KDF would only add latency to every refresh.
    """
    return hashlib.sha256(token.encode("utf-8")).digest()


def _epoch_to_dt(epoch: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _as_epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return value.timestamp()


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

def get_auth_service(request: Request) -> AuthService:
    """Build the auth service from the application's own settings.

    Reads ``app.state.settings`` rather than the process-wide cached
    ``get_settings()``. Those two can differ (tests, per-worker overrides), and
    when they do, the master KEK that *wrapped* a user's DEK is not the one used
    to unwrap it — so every download fails authentication with no obvious cause.
    One application, one settings object.
    """
    return AuthService(
        repository=request.app.state.repo, settings=request.app.state.settings
    )


async def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Principal:
    token = credentials.credentials if credentials and credentials.credentials else None
    if not token:
        token = request.query_params.get("token") or request.query_params.get("access_token")
    if not token:
        raise unauthorized()

    service = AuthService(
        repository=request.app.state.repo, settings=request.app.state.settings
    )
    principal = service.decode_access_token(token)

    # Confirm the account still exists and is active: a JWT stays valid until it
    # expires, so a suspended or deleted user would otherwise keep access for the
    # remainder of its lifetime.
    user = await request.app.state.repo.get_user(principal.user_id)
    if user is None:
        raise unauthorized("Account no longer exists")
    if user["status"] != "active":
        raise forbidden("This account is not active")

    return principal


async def require_admin(
    principal: Principal = Depends(current_user),
) -> Principal:
    if principal.role != "admin":
        raise forbidden("Administrator privileges are required")
    return principal