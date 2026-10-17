"""The wire protocol: names and JSON codecs for talking to the broker.

Like the agreed phrasebook between two pen pals: both sides use the same
words for each request and the same layout for each letter, so nothing gets
lost in translation. This module is pure; it never touches a socket.
"""

from collections.abc import Mapping, Sequence

from pynet.errors import RemoteError

from pymq.broker.core import BrokerStats, PublishResult
from pymq.broker.errors import (
    BrokerError,
    BrokerUnavailableError,
    InvalidNameError,
    InvalidRequestError,
    InvalidTopicError,
    QueueConfigConflictError,
    QueueFullError,
    QueueNotFoundError,
)
from pymq.broker.model import DeadLetter, DeadReason, Envelope, JsonValue, QueueConfig, QueueStats
from pymq.broker.router import TopicEvent

QUEUE_DECLARE = "queue.declare"
QUEUE_DELETE = "queue.delete"
QUEUE_PURGE = "queue.purge"
QUEUE_STATS = "queue.stats"
QUEUE_LIST = "queue.list"
BROKER_STATS = "broker.stats"
MSG_PUT = "msg.put"
MSG_ACK = "msg.ack"
MSG_NACK = "msg.nack"
MSG_TOUCH = "msg.touch"
CONSUME_START = "consume.start"
CONSUME_CANCEL = "consume.cancel"
TOPIC_PUBLISH = "topic.publish"
TOPIC_SUBSCRIBE = "topic.subscribe"
TOPIC_UNSUBSCRIBE = "topic.unsubscribe"
TOPIC_BIND = "topic.bind"
TOPIC_UNBIND = "topic.unbind"
DEAD_LIST = "dead.list"
DEAD_REPLAY = "dead.replay"
DEAD_PURGE = "dead.purge"
PUSH_DELIVER = "deliver"
PUSH_EVENT = "event"

type Json = dict[str, JsonValue]


def _fail(key: str, expected: str) -> InvalidRequestError:
    """Build the error for a missing or wrongly typed field."""
    return InvalidRequestError(f"field {key!r} must be {expected}")


def _as_body(body: JsonValue) -> Mapping[str, JsonValue]:
    """Return body if it is a JSON object, else raise InvalidRequestError."""
    if not isinstance(body, dict):
        msg = "request body must be an object"
        raise InvalidRequestError(msg)
    return body


def req_str(body: JsonValue, key: str) -> str:
    """Return a required string field."""
    value = _as_body(body).get(key)
    if not isinstance(value, str):
        raise _fail(key, "a string")
    return value


def req_int(body: JsonValue, key: str) -> int:
    """Return a required integer field (booleans are not integers)."""
    value = _as_body(body).get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(key, "an integer")
    return value


def opt_str(body: JsonValue, key: str) -> str | None:
    """Return an optional string field, or None when absent or null."""
    value = _as_body(body).get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _fail(key, "a string")
    return value


def opt_float(body: JsonValue, key: str) -> float | None:
    """Return an optional number field as a float; ints are accepted, bools are not."""
    value = _as_body(body).get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _fail(key, "a number")
    return float(value)


def opt_int(body: JsonValue, key: str) -> int | None:
    """Return an optional integer field, or None when absent or null."""
    value = _as_body(body).get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(key, "an integer")
    return value


def opt_bool(body: JsonValue, key: str) -> bool | None:
    """Return an optional boolean field, or None when absent or null."""
    value = _as_body(body).get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _fail(key, "a boolean")
    return value


def opt_headers(body: JsonValue, key: str) -> dict[str, str]:
    """Return an optional object of strings to strings (empty when absent)."""
    value = _as_body(body).get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _fail(key, "an object of strings")
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(v, str):
            raise _fail(key, "an object of strings")
        out[k] = v
    return out


def opt_str_list(body: JsonValue, key: str) -> list[str]:
    """Return an optional list of strings (empty when absent)."""
    value = _as_body(body).get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _fail(key, "a list of strings")
    return [v for v in value if isinstance(v, str)]


def _float_or(body: JsonValue, key: str, default: float) -> float:
    """Return an optional float field, or default when absent."""
    value = opt_float(body, key)
    return default if value is None else value


def _int_or(body: JsonValue, key: str, default: int) -> int:
    """Return an optional int field, or default when absent."""
    value = opt_int(body, key)
    return default if value is None else value


def config_to_json(c: QueueConfig) -> Json:
    """Encode a queue config."""
    return {
        "ack_timeout": c.ack_timeout,
        "max_attempts": c.max_attempts,
        "retry_delay": c.retry_delay,
        "max_retry_delay": c.max_retry_delay,
        "max_length": c.max_length,
        "message_ttl": c.message_ttl,
        "max_dead_letters": c.max_dead_letters,
        "dedupe_window": c.dedupe_window,
    }


def config_from_json(v: JsonValue) -> QueueConfig:
    """Decode a queue config; absent fields take the defaults."""
    d = QueueConfig()
    try:
        return QueueConfig(
            ack_timeout=_float_or(v, "ack_timeout", d.ack_timeout),
            max_attempts=_int_or(v, "max_attempts", d.max_attempts),
            retry_delay=_float_or(v, "retry_delay", d.retry_delay),
            max_retry_delay=_float_or(v, "max_retry_delay", d.max_retry_delay),
            max_length=_int_or(v, "max_length", d.max_length),
            message_ttl=opt_float(v, "message_ttl"),
            max_dead_letters=_int_or(v, "max_dead_letters", d.max_dead_letters),
            dedupe_window=_float_or(v, "dedupe_window", d.dedupe_window),
        )
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from exc


def envelope_to_json(e: Envelope) -> Json:
    """Encode an envelope; attachments travel separately as pynet attachments."""
    return {
        "id": e.id,
        "queue": e.queue,
        "body": e.body,
        "headers": dict(e.headers),
        "priority": e.priority,
        "attempt": e.attempt,
        "enqueued_at": e.enqueued_at,
    }


def envelope_from_json(v: JsonValue, attachments: Sequence[bytes]) -> Envelope:
    """Decode an envelope, attaching the given bytes."""
    return Envelope(
        id=req_str(v, "id"),
        queue=req_str(v, "queue"),
        body=_as_body(v).get("body"),
        headers=opt_headers(v, "headers"),
        attachments=tuple(attachments),
        priority=req_int(v, "priority"),
        attempt=req_int(v, "attempt"),
        enqueued_at=req_str(v, "enqueued_at"),
    )


_STATS_INT_FIELDS = (
    "ready",
    "delayed",
    "in_flight",
    "dead",
    "consumers",
    "enqueued",
    "delivered",
    "acked",
    "retried",
    "dead_lettered",
    "dead_dropped",
    "duplicates",
)


def stats_to_json(s: QueueStats) -> Json:
    """Encode queue stats."""
    out: Json = {"name": s.name, "oldest_ready_age": s.oldest_ready_age}
    for field in _STATS_INT_FIELDS:
        out[field] = getattr(s, field)
    return out


def stats_from_json(v: JsonValue) -> QueueStats:
    """Decode queue stats."""
    ints = {field: req_int(v, field) for field in _STATS_INT_FIELDS}
    return QueueStats(
        name=req_str(v, "name"),
        oldest_ready_age=opt_float(v, "oldest_ready_age"),
        **ints,
    )


def dead_letter_to_json(d: DeadLetter) -> Json:
    """Encode a dead letter; the envelope carries an attachment count, not bytes."""
    env = envelope_to_json(d.envelope)
    env["attachments"] = len(d.envelope.attachments)
    return {"envelope": env, "reason": d.reason.value, "dead_at": d.dead_at, "error": d.error}


def dead_letter_from_json(v: JsonValue) -> DeadLetter:
    """Decode a dead letter; attachments come back empty."""
    env = _as_body(v).get("envelope")
    req_int(env, "attachments")
    reason = req_str(v, "reason")
    try:
        dead_reason = DeadReason(reason)
    except ValueError as exc:
        err = _fail("reason", "a known dead-letter reason")
        raise err from exc
    return DeadLetter(
        envelope=envelope_from_json(env, ()),
        reason=dead_reason,
        dead_at=req_str(v, "dead_at"),
        error=opt_str(v, "error"),
    )


def event_to_json(e: TopicEvent) -> Json:
    """Encode a topic event; attachments travel separately."""
    return {
        "topic": e.topic,
        "body": e.body,
        "headers": dict(e.headers),
        "published_at": e.published_at,
    }


def event_from_json(v: JsonValue, attachments: Sequence[bytes]) -> TopicEvent:
    """Decode a topic event, attaching the given bytes."""
    return TopicEvent(
        topic=req_str(v, "topic"),
        body=_as_body(v).get("body"),
        headers=opt_headers(v, "headers"),
        attachments=tuple(attachments),
        published_at=req_str(v, "published_at"),
    )


def broker_stats_to_json(s: BrokerStats) -> Json:
    """Encode broker stats."""
    return {
        "queues": s.queues,
        "subscriptions": s.subscriptions,
        "bindings": s.bindings,
        "binding_drops": s.binding_drops,
    }


def broker_stats_from_json(v: JsonValue) -> BrokerStats:
    """Decode broker stats."""
    return BrokerStats(
        queues=req_int(v, "queues"),
        subscriptions=req_int(v, "subscriptions"),
        bindings=req_int(v, "bindings"),
        binding_drops=req_int(v, "binding_drops"),
    )


def publish_result_to_json(r: PublishResult) -> Json:
    """Encode a publish result."""
    return {"subscribers": r.subscribers, "queues": r.queues}


def publish_result_from_json(v: JsonValue) -> PublishResult:
    """Decode a publish result."""
    return PublishResult(subscribers=req_int(v, "subscribers"), queues=req_int(v, "queues"))


_ERROR_CLASSES: dict[str, type[BrokerError]] = {
    cls.code: cls
    for cls in (
        BrokerError,
        QueueNotFoundError,
        QueueFullError,
        QueueConfigConflictError,
        InvalidNameError,
        InvalidTopicError,
        InvalidRequestError,
        BrokerUnavailableError,
    )
}


def error_to_remote(err: BrokerError | ValueError) -> RemoteError:
    """Turn a broker error (or a ValueError, as invalid_request) into a RemoteError."""
    code = err.code if isinstance(err, BrokerError) else InvalidRequestError.code
    return RemoteError(code, str(err))


def remote_to_error(err: RemoteError) -> BrokerError | RemoteError:
    """Turn a RemoteError with a known code back into its broker error class."""
    cls = _ERROR_CLASSES.get(err.code)
    if cls is None:
        return err
    return cls(err.remote_message)
