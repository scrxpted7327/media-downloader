"""Validation for watchMyWallet's signed internal acting-user envelope.

This is deliberately wire-compatible with market-ai.  The browser never
creates this envelope; watchMyWallet creates it after authenticating its own
session and signs the exact compact JSON bytes carried in the header.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class ActingContextError(ValueError):
    """A signed acting-user envelope failed an authentication check."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ActingUserContext:
    version: int
    user_id: str
    username: str
    permission: str
    permissions: tuple[str, ...]
    request_id: str
    session_id: str | None
    issued_at: int
    expires_at: int

    def has_permission(self, permission: str) -> bool:
        return permission == self.permission or permission in self.permissions


def _decode_payload(encoded: str) -> bytes:
    if not encoded or len(encoded) > 32_768 or any(character.isspace() for character in encoded):
        raise ActingContextError("malformed_context", "acting context encoding is invalid")
    padding = "=" * (-len(encoded) % 4)
    try:
        return base64.b64decode(
            (encoded + padding).encode("ascii"), altchars=b"-_", validate=True
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ActingContextError("malformed_context", "acting context encoding is invalid") from exc


def _required_string(payload: Mapping[str, Any], name: str, max_length: int = 256) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ActingContextError("malformed_context", f"acting context field {name} is invalid")
    return value


def _timestamp(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ActingContextError("malformed_context", f"acting context field {name} is invalid")
    return value


def validate_acting_context(
    *,
    encoded_payload: str | None,
    signature: str | None,
    request_id_header: str | None,
    signing_secret: str | None,
    max_age_seconds: int = 60,
    clock_skew_seconds: int = 5,
    now: int | None = None,
) -> ActingUserContext:
    if not signing_secret:
        raise ActingContextError("signing_not_configured", "acting context signing is not configured")
    if not signature or len(signature) != hashlib.sha256().digest_size * 2:
        raise ActingContextError("invalid_signature", "acting context signature is invalid")
    try:
        int(signature, 16)
    except ValueError as exc:
        raise ActingContextError("invalid_signature", "acting context signature is invalid") from exc

    raw_payload = _decode_payload(encoded_payload or "")
    expected = hmac.new(signing_secret.encode("utf-8"), raw_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.lower(), expected):
        raise ActingContextError("invalid_signature", "acting context signature is invalid")
    try:
        decoded = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActingContextError("malformed_context", "acting context JSON is invalid") from exc
    if not isinstance(decoded, dict):
        raise ActingContextError("malformed_context", "acting context must be a JSON object")
    if decoded.get("version") != 1:
        raise ActingContextError("unsupported_context", "acting context version is unsupported")

    user_id = _required_string(decoded, "user_id")
    username = _required_string(decoded, "username")
    permission = _required_string(decoded, "permission", 160)
    request_id = _required_string(decoded, "request_id")
    if request_id_header != request_id:
        raise ActingContextError("request_mismatch", "acting context request ID does not match header")
    permissions_value = decoded.get("permissions", [])
    if not isinstance(permissions_value, list) or any(
        not isinstance(item, str) or not item or len(item) > 160 for item in permissions_value
    ):
        raise ActingContextError("malformed_context", "acting context permissions are invalid")

    issued_at = _timestamp(decoded, "issued_at")
    expires_at = _timestamp(decoded, "expires_at")
    if expires_at <= issued_at:
        raise ActingContextError("malformed_context", "acting context lifetime is invalid")
    now_value = int(time.time()) if now is None else int(now)
    skew = max(0, int(clock_skew_seconds))
    max_age = max(1, int(max_age_seconds))
    if expires_at - issued_at > max_age + skew:
        raise ActingContextError("context_too_long", "acting context lifetime exceeds the configured limit")
    if issued_at > now_value + skew:
        raise ActingContextError("context_from_future", "acting context was issued in the future")
    if expires_at < now_value - skew or issued_at < now_value - max_age - skew:
        raise ActingContextError("context_expired", "acting context has expired")

    session_id = decoded.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or len(session_id) > 256):
        raise ActingContextError("malformed_context", "acting context session ID is invalid")
    return ActingUserContext(
        version=1,
        user_id=user_id,
        username=username,
        permission=permission,
        permissions=tuple(sorted(set(permissions_value))),
        request_id=request_id,
        session_id=session_id,
        issued_at=issued_at,
        expires_at=expires_at,
    )


__all__ = ["ActingContextError", "ActingUserContext", "validate_acting_context"]
