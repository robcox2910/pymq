"""The broker core: named queues, topic routing and consumer bookkeeping.

Think of the post office sorting room. Parcels go into named trays (queues),
announcements go up on the notice board (topics), and standing orders
(bindings) copy certain announcements into trays. The broker forgets
everything when it restarts; the scheduler keeps the real records.
"""

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from pymq.broker.errors import QueueConfigConflictError, QueueFullError, QueueNotFoundError
from pymq.broker.model import (
    DeadLetter,
    JsonValue,
    PutResult,
    QueueConfig,
    QueueStats,
    utc_now_iso,
    validate_queue_name,
)
from pymq.broker.router import EventSink, TopicEvent, TopicRouter
from pymq.broker.topics import SYSTEM_PREFIX, validate_topic
from pymq.broker.workqueue import DeliverySink, WorkQueue

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishResult:
    """How many subscribers and queues an announcement reached."""

    subscribers: int
    queues: int


@dataclass(frozen=True)
class BrokerStats:
    """A snapshot of broker-wide counts."""

    queues: int
    subscriptions: int
    bindings: int
    binding_drops: int


class Broker:
    """Hold queues, route topics and track consumers. Create inside a running loop."""

    def __init__(self) -> None:
        """Create an empty broker."""
        self._queues: dict[str, WorkQueue] = {}
        self._router = TopicRouter()
        self._consumer_queue: dict[str, str] = {}
        self._ctag = 0
        self._binding_drops = 0

    def declare_queue(self, name: str, config: QueueConfig | None = None) -> QueueConfig:
        """Create a queue, or confirm it exists with an equal config.

        Raises:
            InvalidNameError: If the name is not allowed.
            QueueConfigConflictError: If it exists with different settings.

        """
        validate_queue_name(name)
        wanted = config if config is not None else QueueConfig()
        existing = self._queues.get(name)
        if existing is not None:
            if existing.config != wanted:
                msg = f"queue {name!r} already exists with different settings"
                raise QueueConfigConflictError(msg)
            return existing.config
        self._queues[name] = WorkQueue(
            name, wanted, on_dead=lambda letter, queue=name: self._announce_dead(queue, letter)
        )
        return wanted

    def delete_queue(self, name: str) -> None:
        """Delete a queue, its bindings and its consumers."""
        queue = self._queue(name)
        del self._queues[name]
        self._router.unbind_queue(name)
        for tag in [t for t, q in self._consumer_queue.items() if q == name]:
            del self._consumer_queue[tag]
        queue.close()

    def purge_queue(self, name: str) -> int:
        """Drop waiting messages and return how many."""
        return self._queue(name).purge()

    def list_queues(self) -> list[str]:
        """Return queue names, sorted."""
        return sorted(self._queues)

    def stats(self, name: str) -> QueueStats:
        """Return one queue's stats."""
        return self._queue(name).stats()

    def broker_stats(self) -> BrokerStats:
        """Return broker-wide counts."""
        return BrokerStats(
            queues=len(self._queues),
            subscriptions=self._router.subscription_count,
            bindings=len(self._router.bindings()),
            binding_drops=self._binding_drops,
        )

    def put(
        self,
        queue: str,
        body: JsonValue,
        *,
        id: str | None = None,  # noqa: A002
        priority: int = 0,
        headers: Mapping[str, str] | None = None,
        attachments: Sequence[bytes] = (),
        delay: float = 0.0,
    ) -> PutResult:
        """Put a message on a queue (see ``WorkQueue.put``)."""
        return self._queue(queue).put(
            body, id=id, priority=priority, headers=headers, attachments=attachments, delay=delay
        )

    def consume(self, queue: str, sink: DeliverySink, *, prefetch: int = 10) -> str:
        """Register a consumer and return its tag (``ctag-N``, unique broker-wide)."""
        target = self._queue(queue)
        tag = f"ctag-{self._ctag + 1}"
        target.add_consumer(tag, sink, prefetch)
        self._ctag += 1
        self._consumer_queue[tag] = queue
        return tag

    def cancel(self, consumer_tag: str) -> int:
        """Remove a consumer and return how many leases were given back."""
        name = self._consumer_queue.pop(consumer_tag, None)
        queue = self._queues.get(name) if name is not None else None
        if queue is None:
            return 0
        return queue.remove_consumer(consumer_tag)

    def ack(self, queue: str, delivery_tag: int) -> bool:
        """Settle a delivery as done. False if stale."""
        return self._queue(queue).ack(delivery_tag)

    def nack(
        self, queue: str, delivery_tag: int, *, requeue: bool = True, error: str | None = None
    ) -> bool:
        """Give a delivery back. False if stale."""
        return self._queue(queue).nack(delivery_tag, requeue=requeue, error=error)

    def touch(self, queue: str, delivery_tag: int) -> bool:
        """Renew a lease. False if stale."""
        return self._queue(queue).touch(delivery_tag)

    def publish(
        self,
        topic: str,
        body: JsonValue,
        *,
        headers: Mapping[str, str] | None = None,
        attachments: Sequence[bytes] = (),
    ) -> PublishResult:
        """Announce on a topic: call matching sinks and copy into bound queues.

        Raises:
            InvalidTopicError: If the topic is not valid.

        """
        validate_topic(topic)
        subs, queues = self._router.route(topic)
        event = TopicEvent(topic, body, dict(headers or {}), tuple(attachments), utc_now_iso())
        for sub_id, sink in subs:
            self._call_sink(sub_id, sink, event)
        delivered = 0
        copy_headers = {**event.headers, "topic": topic}
        for name in queues:
            queue = self._queues.get(name)
            if queue is None:
                continue
            try:
                queue.put(body, headers=copy_headers, attachments=event.attachments)
            except QueueFullError:
                self._binding_drops += 1
            else:
                delivered += 1
        return PublishResult(len(subs), delivered)

    def subscribe(self, pattern: str, sink: EventSink) -> str:
        """Subscribe to a topic pattern and return the subscription id."""
        return self._router.subscribe(pattern, sink)

    def unsubscribe(self, sub_id: str) -> bool:
        """Cancel a subscription. False if unknown."""
        return self._router.unsubscribe(sub_id)

    def bind(self, pattern: str, queue: str) -> None:
        """Bind an existing queue to a topic pattern."""
        self._queue(queue)
        self._router.bind(pattern, queue)

    def unbind(self, pattern: str, queue: str) -> bool:
        """Remove a binding. False if it did not exist."""
        self._queue(queue)
        return self._router.unbind(pattern, queue)

    def dead_letters(self, queue: str, limit: int | None = None) -> list[DeadLetter]:
        """Return a queue's dead letters, oldest first."""
        return self._queue(queue).dead_letters(limit)

    def replay_dead(self, queue: str, ids: Collection[str] | None = None) -> int:
        """Replay dead letters and return how many."""
        return self._queue(queue).replay_dead(ids)

    def purge_dead(self, queue: str) -> int:
        """Forget a queue's dead letters and return how many."""
        return self._queue(queue).purge_dead()

    def close(self) -> None:
        """Close every queue and forget all routing state."""
        for queue in self._queues.values():
            queue.close()
        self._queues.clear()
        self._consumer_queue.clear()
        self._router = TopicRouter()

    def _queue(self, name: str) -> WorkQueue:
        """Return the named queue or raise QueueNotFoundError."""
        queue = self._queues.get(name)
        if queue is None:
            msg = f"no such queue: {name!r}"
            raise QueueNotFoundError(msg)
        return queue

    def _call_sink(self, sub_id: str, sink: EventSink, event: TopicEvent) -> None:
        """Call one subscriber, logging instead of raising."""
        try:
            sink(sub_id, event)
        except Exception:
            logger.exception("subscriber %r failed on topic %r", sub_id, event.topic)

    def _announce_dead(self, queue: str, letter: DeadLetter) -> None:
        """Publish a ``$sys.dead.<queue>`` event for a dead letter."""
        body: JsonValue = {
            "queue": queue,
            "id": letter.envelope.id,
            "reason": str(letter.reason),
            "error": letter.error,
            "attempt": letter.envelope.attempt,
            "dead_at": letter.dead_at,
        }
        self.publish(f"{SYSTEM_PREFIX}.dead.{queue}", body)
