"""Tests for the broker data model, errors and name validation."""

import math

import pytest

from pymq.broker import (
    BrokerError,
    DeadReason,
    InvalidNameError,
    QueueConfig,
    QueueFullError,
    validate_queue_name,
)
from pymq.broker.errors import (
    BrokerUnavailableError,
    InvalidRequestError,
    InvalidTopicError,
    QueueConfigConflictError,
    QueueNotFoundError,
)
from pymq.broker.model import utc_now_iso

MAX_NAME = 200
DEFAULT_ACK_TIMEOUT = 30.0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_DELAY = 1.0
DEFAULT_MAX_RETRY_DELAY = 60.0
DEFAULT_MAX_LENGTH = 10_000
DEFAULT_MAX_DEAD = 1_000
DEFAULT_DEDUPE = 300.0
CAP = 5.0


class TestQueueConfig:
    """Tests for QueueConfig."""

    def test_defaults_match_spec(self) -> None:
        """Verify the defaults equal the specification."""
        c = QueueConfig()
        assert (c.ack_timeout, c.max_attempts, c.retry_delay, c.max_retry_delay) == (
            DEFAULT_ACK_TIMEOUT,
            DEFAULT_MAX_ATTEMPTS,
            DEFAULT_RETRY_DELAY,
            DEFAULT_MAX_RETRY_DELAY,
        )
        assert (c.max_length, c.message_ttl, c.max_dead_letters, c.dedupe_window) == (
            DEFAULT_MAX_LENGTH,
            None,
            DEFAULT_MAX_DEAD,
            DEFAULT_DEDUPE,
        )

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"ack_timeout": 0},
            {"ack_timeout": math.nan},
            {"ack_timeout": math.inf},
            {"max_attempts": 0},
            {"max_attempts": True},
            {"max_attempts": 1.5},
            {"retry_delay": -1},
            {"retry_delay": 10.0, "max_retry_delay": 5.0},
            {"max_length": 0},
            {"message_ttl": 0},
            {"max_dead_letters": 0},
            {"dedupe_window": 0},
        ],
    )
    def test_bad_values_rejected(self, kwargs: dict[str, object]) -> None:
        """Verify invalid settings raise ValueError."""
        with pytest.raises(ValueError):  # noqa: PT011
            QueueConfig(**kwargs)  # pyright: ignore[reportArgumentType]

    def test_retry_wait_doubles_then_caps(self) -> None:
        """Verify the retry wait doubles and is capped."""
        c = QueueConfig(retry_delay=1.0, max_retry_delay=CAP)
        assert [c.retry_wait(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, CAP]


class TestQueueNames:
    """Tests for validate_queue_name."""

    @pytest.mark.parametrize("name", ["jobs", "job.q-1_x", "a" * MAX_NAME])
    def test_good_names(self, name: str) -> None:
        """Verify valid names are returned unchanged."""
        assert validate_queue_name(name) == name

    @pytest.mark.parametrize(
        "name", ["", "a" * (MAX_NAME + 1), "jobs/../x", "a..b", "sp ace", "ünï"]
    )
    def test_bad_names(self, name: str) -> None:
        """Verify invalid names are rejected."""
        with pytest.raises(InvalidNameError):
            validate_queue_name(name)


class TestErrors:
    """Tests for the error hierarchy."""

    def test_codes(self) -> None:
        """Verify each error carries its wire code."""
        assert QueueFullError.code == "queue_full"
        assert issubclass(QueueFullError, BrokerError)
        assert BrokerError.code == "broker_error"
        assert QueueNotFoundError.code == "queue_not_found"
        assert QueueConfigConflictError.code == "config_conflict"
        assert InvalidNameError.code == "invalid_name"
        assert InvalidTopicError.code == "invalid_topic"
        assert InvalidRequestError.code == "invalid_request"
        assert BrokerUnavailableError.code == "broker_unavailable"


class TestMisc:
    """Tests for small model helpers."""

    def test_dead_reason_values(self) -> None:
        """Verify dead-letter reasons are their wire strings."""
        assert DeadReason.TTL_EXPIRED == "ttl_expired"

    def test_utc_now_iso_is_utc(self) -> None:
        """Verify the timestamp carries a UTC offset."""
        assert utc_now_iso().endswith("+00:00")
