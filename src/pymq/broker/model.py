"""Data model for the broker: settings, messages, stats and name checks.

Like the labels and forms at a post office: small, fixed records that say what
a parcel is, who holds it and how the counter is doing.
"""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pynet.messaging import JsonValue as _PynetJsonValue

from pymq.broker.errors import InvalidNameError

type JsonValue = _PynetJsonValue

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def _check_float(name: str, value: float) -> None:
    """Raise ValueError unless value is a finite positive number."""
    if isinstance(value, bool) or not isinstance(value, int | float):  # pyright: ignore[reportUnnecessaryIsInstance]
        msg = f"{name} must be a number, got {value!r}"
        raise ValueError(msg)  # noqa: TRY004
    if not math.isfinite(value) or value <= 0:
        msg = f"{name} must be finite and > 0, got {value!r}"
        raise ValueError(msg)


def _check_int(name: str, value: int) -> None:
    """Raise ValueError unless value is an int (not bool) of at least 1."""
    if isinstance(value, bool) or not isinstance(value, int):  # pyright: ignore[reportUnnecessaryIsInstance]
        msg = f"{name} must be an int, got {value!r}"
        raise ValueError(msg)  # noqa: TRY004
    if value < 1:
        msg = f"{name} must be >= 1, got {value!r}"
        raise ValueError(msg)


@dataclass(frozen=True)
class QueueConfig:
    """Settings for one queue."""

    ack_timeout: float = 30.0
    max_attempts: int = 5
    retry_delay: float = 1.0
    max_retry_delay: float = 60.0
    max_length: int = 10_000
    message_ttl: float | None = None
    max_dead_letters: int = 1_000
    dedupe_window: float = 300.0

    def __post_init__(self) -> None:
        """Validate every setting."""
        _check_float("ack_timeout", self.ack_timeout)
        _check_float("retry_delay", self.retry_delay)
        _check_float("max_retry_delay", self.max_retry_delay)
        _check_float("dedupe_window", self.dedupe_window)
        if self.message_ttl is not None:
            _check_float("message_ttl", self.message_ttl)
        _check_int("max_attempts", self.max_attempts)
        _check_int("max_length", self.max_length)
        _check_int("max_dead_letters", self.max_dead_letters)
        if self.max_retry_delay < self.retry_delay:
            msg = "max_retry_delay must be >= retry_delay"
            raise ValueError(msg)

    def retry_wait(self, attempt: int) -> float:
        """Return the seconds to wait before retrying after the given attempt."""
        return min(self.retry_delay * 2 ** (attempt - 1), self.max_retry_delay)


@dataclass(frozen=True)
class Envelope:
    """A message together with its delivery metadata."""

    id: str
    queue: str
    body: JsonValue
    headers: Mapping[str, str]
    attachments: tuple[bytes, ...]
    priority: int
    attempt: int
    enqueued_at: str


@dataclass(frozen=True)
class LeasedMessage:
    """A message currently leased to a consumer."""

    queue: str
    consumer_tag: str
    delivery_tag: int
    envelope: Envelope


class DeadReason(StrEnum):
    """Why a message was dead-lettered."""

    MAX_ATTEMPTS = "max_attempts"
    REJECTED = "rejected"
    TTL_EXPIRED = "ttl_expired"


@dataclass(frozen=True)
class DeadLetter:
    """A message that will not be delivered again."""

    envelope: Envelope
    reason: DeadReason
    dead_at: str
    error: str | None


@dataclass(frozen=True)
class PutResult:
    """The outcome of putting a message on a queue."""

    id: str
    duplicate: bool


@dataclass(frozen=True)
class QueueStats:
    """A snapshot of a queue's depth and counters."""

    name: str
    ready: int
    delayed: int
    in_flight: int
    dead: int
    consumers: int
    oldest_ready_age: float | None
    enqueued: int
    delivered: int
    acked: int
    retried: int
    dead_lettered: int
    dead_dropped: int
    duplicates: int


def validate_queue_name(name: str) -> str:
    """Return name if it is a valid queue name, else raise InvalidNameError."""
    if not _NAME_RE.fullmatch(name) or ".." in name:
        msg = f"invalid queue name: {name!r}"
        raise InvalidNameError(msg)
    return name


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(tz=UTC).isoformat()
