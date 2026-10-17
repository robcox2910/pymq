"""Tests for topic names, patterns and wildcard matching."""

import time

import pytest

from pymq.broker import (
    SYSTEM_PREFIX,
    InvalidTopicError,
    topic_matches,
    validate_pattern,
    validate_topic,
)

MANY_WORDS = 50
MAX_LEN = 255
SPEED_LIMIT_SECONDS = 1.0

MATCH_TABLE = [
    ("job.*.failed", "job.nightly.failed", True),
    ("job.*.failed", "job.a.b.failed", False),
    ("job.#", "job", True),
    ("job.#", "job.a.b", True),
    ("#", "anything.at.all", True),
    ("#.failed", "failed", True),
    ("#.failed", "a.b.failed", True),
    ("#.failed", "a.b.ok", False),
    ("job.#.failed", "job.failed", True),
    ("job.#.failed", "job.x.y.failed", True),
    ("job.*", "job", False),
    ("job.*", "job.a", True),
    ("job.done", "job.done", True),
    ("job.done", "job.other", False),
    ("$sys.dead.*", "$sys.dead.jobs", True),
    ("#", "$sys.dead.jobs", True),
    ("*", "a.b", False),
]


class TestValidateTopic:
    """Check topic validation."""

    @pytest.mark.parametrize(
        "topic", ["a", "job.done", "$sys.dead.jobs", "a-b_c.D9", "x" * MAX_LEN]
    )
    def test_good_topics(self, topic: str) -> None:
        """Accept well-formed topics."""
        assert validate_topic(topic) == topic

    @pytest.mark.parametrize(
        "topic",
        ["", "a..b", ".a", "a.", "a.*", "a.#", "a b", "x" * (MAX_LEN + 1), "a.$b", "#.x", "a/b"],
    )
    def test_bad_topics(self, topic: str) -> None:
        """Reject malformed topics."""
        with pytest.raises(InvalidTopicError):
            validate_topic(topic)

    def test_system_prefix(self) -> None:
        """Expose the reserved system prefix."""
        assert SYSTEM_PREFIX == "$sys"


class TestValidatePattern:
    """Check pattern validation."""

    @pytest.mark.parametrize("pattern", ["#", "*", "job.*.failed", "job.#", "$sys.dead.*", "a"])
    def test_good_patterns(self, pattern: str) -> None:
        """Accept well-formed patterns."""
        assert validate_pattern(pattern) == pattern

    @pytest.mark.parametrize(
        "pattern", ["a*", "#x", "a..b", "", "a.**", "a.b#", "x" * (MAX_LEN + 1)]
    )
    def test_bad_patterns(self, pattern: str) -> None:
        """Reject malformed patterns."""
        with pytest.raises(InvalidTopicError):
            validate_pattern(pattern)


class TestTopicMatches:
    """Check wildcard matching."""

    @pytest.mark.parametrize(("pattern", "topic", "expected"), MATCH_TABLE)
    def test_table(self, pattern: str, topic: str, expected: bool) -> None:
        """Match the documented table of examples."""
        assert topic_matches(pattern, topic) is expected

    def test_many_hashes_do_not_explode(self) -> None:
        """Finish quickly with many hashes against a long topic."""
        pattern = ".".join(["#"] * 10 + ["z"])
        topic = ".".join(["a"] * MANY_WORDS)
        start = time.perf_counter()
        assert topic_matches(pattern, topic) is False
        assert time.perf_counter() - start < SPEED_LIMIT_SECONDS
        assert topic_matches(pattern, topic + ".z") is True
