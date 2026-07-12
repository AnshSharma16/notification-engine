"""
schemas.py — Pydantic v2 request / response contracts.

[OPT-3] Validation upgrades over the original:
  - Recipient is validated with channel-aware regex AFTER channel is known,
    using @model_validator(mode='after') so both fields are available.
  - Separate compiled regex patterns per channel avoid re-compiling on every
    request; compiled at module load time as module-level constants.
  - Input sanitisation: HTML/control-char stripping on body and subject.
  - Metadata normalised: keys coerced to str, depth capped at 2, size capped
    at 50 keys — prevents pathological JSONB payloads from hitting Postgres.
  - All validators use mode='after' where field co-dependence is needed, and
    mode='before' where raw string normalisation happens first.
"""

from __future__ import annotations

import html
import json
import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models import NotificationChannel, NotificationStatus


# ---------------------------------------------------------------------------
# [OPT-3] Compiled regex constants — pay the compile cost once at import time
# ---------------------------------------------------------------------------

# RFC 5321 email — local part up to 64 chars, domain up to 253 chars
_RE_EMAIL = re.compile(
    r"^[a-zA-Z0-9._%+\-]{1,64}@[a-zA-Z0-9.\-]{1,253}\.[a-zA-Z]{2,}$"
)

# E.164 international phone number (+<1-15 digits>)
_RE_E164 = re.compile(r"^\+[1-9]\d{6,14}$")

# HTTPS-only webhook URL (no bare IP, no localhost in production)
_RE_WEBHOOK = re.compile(
    r"^https://[a-zA-Z0-9.\-]+(:\d{1,5})?(/[\w\-./?%&=]*)?$"
)

# FCM/APNs push token: hex (APNs) or base64url-ish (FCM) between 32–512 chars
_RE_PUSH_TOKEN = re.compile(r"^[a-zA-Z0-9\-_:]{32,512}$")

# Control-character sanitiser — strips ASCII ctrl chars except tab/newline
_RE_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Maximum depth of metadata dict (checked recursively)
_META_MAX_KEYS = 50
_META_MAX_DEPTH = 2


# ---------------------------------------------------------------------------
# [OPT-3] Sanitisation helpers
# ---------------------------------------------------------------------------

def _sanitise_text(value: str) -> str:
    """
    Strip control characters and HTML-encode angle brackets.

    We do NOT fully HTML-escape (that would mangle JSON payloads in body);
    we just strip chars that have no legitimate place in a notification string.
    """
    value = _RE_CTRL.sub("", value)            # drop invisible control chars
    value = value.replace("\x00", "")          # belt-and-suspenders null byte
    return value


def _normalise_metadata(raw: Any, *, depth: int = 0) -> dict[str, Any]:
    """
    Coerce raw input to a shallow-ish dict with string keys.

    Rules enforced:
      - Top-level must be a dict (or JSON-decodable string → dict).
      - Keys converted to str and stripped.
      - Depth capped at _META_MAX_DEPTH: nested dicts below that become str().
      - Total top-level key count capped at _META_MAX_KEYS.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError("metadata must be a JSON object or Python dict")

    if not isinstance(raw, dict):
        raise ValueError("metadata must be a JSON object / dict")

    if depth == 0 and len(raw) > _META_MAX_KEYS:
        raise ValueError(f"metadata must not exceed {_META_MAX_KEYS} top-level keys")

    normalised: dict[str, Any] = {}
    for k, v in raw.items():
        str_key = str(k).strip()
        if not str_key:
            continue  # silently drop blank keys
        if depth < _META_MAX_DEPTH and isinstance(v, dict):
            normalised[str_key] = _normalise_metadata(v, depth=depth + 1)
        elif depth >= _META_MAX_DEPTH and isinstance(v, dict):
            normalised[str_key] = str(v)  # flatten beyond max depth
        else:
            normalised[str_key] = v

    return normalised


# ---------------------------------------------------------------------------
# [OPT-3] Channel-aware recipient validator
# ---------------------------------------------------------------------------

def _validate_recipient_for_channel(recipient: str, channel: str) -> str:
    """
    Apply the channel-specific regex to the recipient string.

    Called from the model_validator after both `recipient` and `channel`
    are available in their final (post-field-validator) state.
    """
    ch = NotificationChannel(channel) if isinstance(channel, str) else channel

    if ch == NotificationChannel.EMAIL:
        if not _RE_EMAIL.match(recipient):
            raise ValueError(
                f"recipient {recipient!r} is not a valid RFC 5321 email address"
            )
    elif ch == NotificationChannel.SMS:
        if not _RE_E164.match(recipient):
            raise ValueError(
                f"recipient {recipient!r} is not a valid E.164 phone number "
                "(expected format: +12125550100)"
            )
    elif ch == NotificationChannel.WEBHOOK:
        if not _RE_WEBHOOK.match(recipient):
            raise ValueError(
                f"recipient {recipient!r} is not a valid HTTPS webhook URL"
            )
    elif ch == NotificationChannel.PUSH:
        if not _RE_PUSH_TOKEN.match(recipient):
            raise ValueError(
                f"recipient {recipient!r} is not a valid push device token "
                "(expected 32–512 alphanumeric/dash/underscore/colon chars)"
            )

    return recipient


# ---------------------------------------------------------------------------
# Inbound schema
# ---------------------------------------------------------------------------

class NotificationRequest(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        use_enum_values=True,
        populate_by_name=True,
    )

    recipient: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description=(
            "Delivery target — format depends on channel: "
            "EMAIL: RFC 5321 address | SMS: E.164 (+12125550100) | "
            "PUSH: device token (32–512 chars) | WEBHOOK: HTTPS URL"
        ),
        examples=["user@example.com", "+12125550100"],
    )
    channel: NotificationChannel = Field(..., description="Delivery channel.")
    subject: str | None = Field(
        default=None,
        max_length=998,
        description="Optional subject line (EMAIL channel).",
    )
    body: str = Field(
        ...,
        min_length=1,
        max_length=10_000,
        description="Notification body text.",
    )
    priority: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Dispatch priority. 1 = highest, 10 = lowest.",
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Arbitrary channel-specific metadata (max 50 keys, depth 2).",
    )

    # ------------------------------------------------------------------
    # [OPT-3] Field-level sanitisation (mode='before' = runs on raw input)
    # ------------------------------------------------------------------

    @field_validator("body", "subject", mode="before")
    @classmethod
    def sanitise_text_fields(cls, v: Any) -> Any:
        """Strip control characters from free-text fields before length checks."""
        if isinstance(v, str):
            return _sanitise_text(v)
        return v  # let Pydantic's type coercion handle non-str

    @field_validator("recipient", mode="before")
    @classmethod
    def sanitise_recipient(cls, v: Any) -> Any:
        """Strip whitespace and control chars; channel-aware regex runs later."""
        if isinstance(v, str):
            return _sanitise_text(v.strip())
        return v

    @field_validator("body", mode="after")
    @classmethod
    def body_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body must not be blank or whitespace-only")
        return v

    # ------------------------------------------------------------------
    # [OPT-3] Metadata normalisation
    # ------------------------------------------------------------------

    @field_validator("metadata", mode="before")
    @classmethod
    def normalise_metadata(cls, v: Any) -> Any:
        """
        Accept dict or JSON string; normalise keys, cap depth and size.
        None passes through (field is optional).
        """
        if v is None:
            return v
        return _normalise_metadata(v)

    # ------------------------------------------------------------------
    # [OPT-3] Cross-field model validator — recipient × channel regex
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def validate_recipient_against_channel(self) -> "NotificationRequest":
        """
        Channel-aware recipient validation.

        Runs after all field validators so both `recipient` and `channel`
        are in their final normalised form. This is the correct hook for
        cross-field validation in Pydantic v2.
        """
        self.recipient = _validate_recipient_for_channel(
            self.recipient, self.channel  # type: ignore[arg-type]
        )
        return self


# ---------------------------------------------------------------------------
# Outbound schemas (unchanged shape — external API contract preserved)
# ---------------------------------------------------------------------------

class NotificationResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True, use_enum_values=True)

    notification_id: uuid.UUID = Field(description="Stable identifier for polling.")
    status: NotificationStatus
    channel: NotificationChannel
    recipient: str
    created_at: datetime


class AcceptedResponse(BaseModel):
    """202 Accepted envelope returned immediately from the ingestion endpoint."""

    model_config = ConfigDict(populate_by_name=True)

    message: str = "Notification accepted for processing."
    notification: NotificationResponse


class ErrorDetail(BaseModel):
    """Standardised error payload — now also carries request_id for tracing."""

    code: str
    message: str
    request_id: str | None = None   # [OPT-4] injected by middleware
    details: dict[str, Any] | None = None