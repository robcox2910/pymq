"""Tests for the broker wire protocol codecs."""

import pytest
from pynet.errors import RemoteError

from pymq.broker import protocol as p
from pymq.broker.core import BrokerStats, PublishResult
from pymq.broker.errors import (
    BrokerError,
    BrokerUnavailableError,
    InvalidNameError,
    InvalidRequestError,
    QueueFullError,
)
from pymq.broker.model import DeadLetter, DeadReason, Envelope, JsonValue, QueueConfig, QueueStats
from pymq.broker.router import TopicEvent

PRIORITY = 3
ATTEMPT = 2
COUNT = 7
TWO = 2
FIVE = 5
AGE = 1.5
NOW = "2026-10-17T10:00:00+00:00"


def _envelope(attachments: tuple[bytes, ...] = ()) -> Envelope:
    """Build a sample envelope."""
    return Envelope(
        id="m1",
        queue="jobs",
        body={"a": [1, 2, None]},
        headers={"k": "v"},
        attachments=attachments,
        priority=PRIORITY,
        attempt=ATTEMPT,
        enqueued_at=NOW,
    )


class TestRoundTrips:
    """Check every codec survives a round trip."""

    def test_config(self) -> None:
        """Round-trip a config, with and without a ttl."""
        for cfg in (QueueConfig(), QueueConfig(message_ttl=5.0, max_attempts=TWO)):
            assert p.config_from_json(p.config_to_json(cfg)) == cfg

    def test_config_accepts_int_for_float(self) -> None:
        """Accept JSON 5 where a float is expected."""
        cfg = p.config_from_json({"ack_timeout": FIVE})
        assert cfg.ack_timeout == float(FIVE)

    def test_config_rejects_bool_float_and_invalid(self) -> None:
        """Reject bools and values QueueConfig refuses."""
        with pytest.raises(InvalidRequestError):
            p.config_from_json({"ack_timeout": True})
        with pytest.raises(InvalidRequestError):
            p.config_from_json({"ack_timeout": -1})
        with pytest.raises(InvalidRequestError):
            p.config_from_json([1])
        with pytest.raises(InvalidRequestError):
            p.config_from_json({"max_attempts": "x"})

    def test_envelope(self) -> None:
        """Round-trip an envelope with attachments supplied separately."""
        env = _envelope((b"x", b"y"))
        wire = p.envelope_to_json(env)
        assert "attachments" not in wire
        assert p.envelope_from_json(wire, (b"x", b"y")) == env

    def test_envelope_bad(self) -> None:
        """Reject a malformed envelope."""
        wire = p.envelope_to_json(_envelope())
        wire["priority"] = "high"
        with pytest.raises(InvalidRequestError):
            p.envelope_from_json(wire, ())
        with pytest.raises(InvalidRequestError):
            p.envelope_from_json("nope", ())

    def test_stats(self) -> None:
        """Round-trip queue stats, with and without an oldest age."""
        for age in (None, AGE):
            st = QueueStats("q", 1, 2, 3, 4, 5, age, 6, 7, 8, 9, 10, 11, 12)
            assert p.stats_from_json(p.stats_to_json(st)) == st

    def test_dead_letter(self) -> None:
        """Round-trip a dead letter, keeping only the attachment count."""
        dl = DeadLetter(_envelope((b"x", b"y")), DeadReason.REJECTED, NOW, "boom")
        wire = p.dead_letter_to_json(dl)
        env_wire = wire["envelope"]
        assert isinstance(env_wire, dict)
        assert env_wire["attachments"] == TWO
        back = p.dead_letter_from_json(wire)
        assert back == DeadLetter(_envelope(()), DeadReason.REJECTED, NOW, "boom")
        none_err = DeadLetter(_envelope(), DeadReason.TTL_EXPIRED, NOW, None)
        assert p.dead_letter_from_json(p.dead_letter_to_json(none_err)) == none_err

    def test_dead_letter_bad_reason(self) -> None:
        """Reject an unknown dead reason."""
        wire = p.dead_letter_to_json(DeadLetter(_envelope(), DeadReason.REJECTED, NOW, None))
        wire["reason"] = "whatever"
        with pytest.raises(InvalidRequestError):
            p.dead_letter_from_json(wire)

    def test_topic_event(self) -> None:
        """Round-trip a topic event with separate attachments."""
        ev = TopicEvent("a.b", {"x": 1}, {"h": "v"}, (b"z",), NOW)
        wire = p.event_to_json(ev)
        assert "attachments" not in wire
        assert p.event_from_json(wire, (b"z",)) == ev

    def test_broker_stats_and_publish_result(self) -> None:
        """Round-trip broker stats and publish results."""
        bs = BrokerStats(1, 2, 3, 4)
        assert p.broker_stats_from_json(p.broker_stats_to_json(bs)) == bs
        pr = PublishResult(1, 2)
        assert p.publish_result_from_json(p.publish_result_to_json(pr)) == pr


class TestReaders:
    """Check the field readers."""

    def test_good_values(self) -> None:
        """Read present, correctly typed fields."""
        body: dict[str, JsonValue] = {
            "s": "x",
            "i": COUNT,
            "f": AGE,
            "b": True,
            "h": {"a": "b"},
            "l": ["a"],
        }
        assert p.req_str(body, "s") == "x"
        assert p.req_int(body, "i") == COUNT
        assert p.opt_str(body, "s") == "x"
        assert p.opt_float(body, "f") == AGE
        assert p.opt_float(body, "i") == float(COUNT)
        assert p.opt_int(body, "i") == COUNT
        assert p.opt_bool(body, "b") is True
        assert p.opt_headers(body, "h") == {"a": "b"}
        assert p.opt_str_list(body, "l") == ["a"]

    def test_optional_missing(self) -> None:
        """Return defaults for absent or null optional fields."""
        assert p.opt_str({}, "s") is None
        assert p.opt_str({"s": None}, "s") is None
        assert p.opt_float({}, "f") is None
        assert p.opt_int({}, "i") is None
        assert p.opt_bool({}, "b") is None
        assert p.opt_headers({}, "h") == {}
        assert p.opt_str_list({}, "l") == []

    @pytest.mark.parametrize(
        "call",
        [
            lambda: p.req_str({}, "k"),
            lambda: p.req_str({"k": 1}, "k"),
            lambda: p.req_int({}, "k"),
            lambda: p.req_int({"k": True}, "k"),
            lambda: p.req_int({"k": 1.5}, "k"),
            lambda: p.opt_str({"k": 1}, "k"),
            lambda: p.opt_float({"k": True}, "k"),
            lambda: p.opt_float({"k": "x"}, "k"),
            lambda: p.opt_int({"k": True}, "k"),
            lambda: p.opt_int({"k": "1"}, "k"),
            lambda: p.opt_bool({"k": 1}, "k"),
            lambda: p.opt_headers({"k": [1]}, "k"),
            lambda: p.opt_headers({"k": {"a": 1}}, "k"),
            lambda: p.opt_str_list({"k": "abc"}, "k"),
            lambda: p.opt_str_list({"k": [1]}, "k"),
            lambda: p.req_str("notdict", "k"),
        ],
    )
    def test_bad_values(self, call: object) -> None:
        """Reject missing or wrongly typed fields."""
        assert callable(call)
        with pytest.raises(InvalidRequestError):
            call()


class TestErrors:
    """Check error mapping."""

    def test_to_remote(self) -> None:
        """Map broker errors and ValueError to remote errors."""
        err = p.error_to_remote(QueueFullError("full"))
        assert (err.code, err.remote_message) == ("queue_full", "full")
        err = p.error_to_remote(ValueError("bad"))
        assert err.code == "invalid_request"
        assert err.remote_message == "bad"

    def test_from_remote_known(self) -> None:
        """Rebuild the matching class with the remote message."""
        back = p.remote_to_error(RemoteError("queue_full", "x"))
        assert isinstance(back, QueueFullError)
        assert "x" in str(back)
        assert isinstance(p.remote_to_error(RemoteError("invalid_name", "n")), InvalidNameError)
        assert isinstance(
            p.remote_to_error(RemoteError("broker_unavailable", "n")), BrokerUnavailableError
        )
        generic = p.remote_to_error(RemoteError("broker_error", "n"))
        assert type(generic) is BrokerError

    def test_from_remote_unknown(self) -> None:
        """Pass unknown codes through unchanged."""
        err = RemoteError("mystery", "?")
        assert p.remote_to_error(err) is err


class TestNames:
    """Check the protocol names."""

    def test_names(self) -> None:
        """Spot-check wire names."""
        assert p.QUEUE_DECLARE == "queue.declare"
        assert p.MSG_PUT == "msg.put"
        assert p.PUSH_DELIVER == "deliver"
        assert p.PUSH_EVENT == "event"
        assert p.DEAD_REPLAY == "dead.replay"
