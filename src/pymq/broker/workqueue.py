"""A work queue: storage plus consumers, leases, acks and retries.

Think of the post office counter clerks. Each clerk takes parcels on a signed
loan (a lease): hand it back done (ack), hand it back to try again (nack), ask
for more time (touch), or do nothing and the loan runs out and the parcel goes
back on the tray for someone else.
"""

import heapq
import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from pymq.broker.model import DeadLetter, DeadReason, LeasedMessage, QueueConfig
from pymq.broker.queuestore import QueueStore, Record, State
from pymq.broker.timers import TimerHeap

logger = logging.getLogger(__name__)

type DeliverySink = Callable[[LeasedMessage], None]


@dataclass(slots=True)
class _Consumer:
    """One registered consumer and the deliveries it currently holds."""

    sink: DeliverySink
    prefetch: int
    tags: set[int] = field(default_factory=lambda: set[int]())


@dataclass(slots=True)
class _Lease:
    """A loan of one message to one consumer, valid until ``deadline``."""

    message_id: str
    consumer_tag: str
    deadline: float


class WorkQueue(QueueStore):
    """Store messages for one queue and lease them to consumers.

    Must be created inside a running pynet loop, which supplies the clock and
    timers. Delivery is at-least-once: an unsettled lease expires and the
    message is retried, so a handler may see the same message twice.
    """

    def __init__(
        self,
        name: str,
        config: QueueConfig,
        *,
        on_dead: Callable[[DeadLetter], None] | None = None,
    ) -> None:
        """Create an empty queue called ``name`` using ``config``."""
        super().__init__(name, config, on_dead=on_dead)
        self._leases: dict[int, _Lease] = {}
        self._lease_timers = TimerHeap[tuple[int, float]](
            self._loop, self._lease_due, self._lease_live
        )
        self._tags = itertools.count(1)
        self._consumers: dict[str, _Consumer] = {}
        self._turn = 0
        self._dispatching = False

    def close(self) -> None:
        """Cancel every timer and drop consumers (no requeue); refuse later calls."""
        super().close()
        self._consumers.clear()
        self._leases.clear()
        self._lease_timers.close()

    @property
    def consumer_count(self) -> int:
        """Return how many consumers are registered."""
        return len(self._consumers)

    def add_consumer(self, consumer_tag: str, sink: DeliverySink, prefetch: int = 10) -> None:
        """Register a consumer and start feeding it. ``sink`` must not block.

        Raises:
            ValueError: If ``prefetch`` is below 1 or the tag is already used.

        """
        self._check_open()
        if prefetch < 1:
            msg = f"prefetch must be >= 1, got {prefetch!r}"
            raise ValueError(msg)
        if consumer_tag in self._consumers:
            msg = f"duplicate consumer tag {consumer_tag!r}"
            raise ValueError(msg)
        self._consumers[consumer_tag] = _Consumer(sink, prefetch)
        self._dispatch()

    def remove_consumer(self, consumer_tag: str) -> int:
        """Remove a consumer, nack its leases for retry and return how many."""
        self._check_open()
        order = list(self._consumers)
        consumer = self._consumers.pop(consumer_tag, None)
        if consumer is None:
            return 0
        if order.index(consumer_tag) < self._turn:
            self._turn -= 1
        tags = sorted(consumer.tags)
        for tag in tags:
            self.nack(tag, error="consumer removed")
        return len(tags)

    def ack(self, delivery_tag: int) -> bool:
        """Settle a delivery as done. Return False if it is stale."""
        record = self._release(delivery_tag)
        if record is None:
            return False
        self._acked += 1
        self._dispatch()
        return True

    def nack(self, delivery_tag: int, *, requeue: bool = True, error: str | None = None) -> bool:
        """Give a delivery back: retry with backoff, or dead-letter it. False if stale."""
        record = self._release(delivery_tag)
        if record is None:
            return False
        envelope = record.envelope
        if not requeue:
            self._dead(envelope, DeadReason.REJECTED, error)
        elif envelope.attempt >= self._config.max_attempts:
            self._dead(envelope, DeadReason.MAX_ATTEMPTS, error)
        else:
            self._retried += 1
            self._enter(envelope, delay=self._config.retry_wait(envelope.attempt))
        self._dispatch()
        return True

    def touch(self, delivery_tag: int) -> bool:
        """Restart a lease's clock. Return False if the delivery is stale."""
        lease = self._leases.get(delivery_tag)
        if lease is None:
            return False
        lease.deadline = self._loop.time() + self._config.ack_timeout
        self._lease_timers.push(lease.deadline, (delivery_tag, lease.deadline))
        return True

    def _release(self, delivery_tag: int) -> Record | None:
        """End a lease and take its message out of the store, or None if stale."""
        lease = self._leases.pop(delivery_tag, None)
        if lease is None:
            return None
        consumer = self._consumers.get(lease.consumer_tag)
        if consumer is not None:
            consumer.tags.discard(delivery_tag)
        record = self._records.pop(lease.message_id)
        self._counts[State.LEASED] -= 1
        return record

    def _lease_live(self, item: tuple[int, float]) -> bool:
        """Return whether the lease deadline entry is the current one."""
        lease = self._leases.get(item[0])
        return lease is not None and lease.deadline == item[1]

    def _lease_due(self, item: tuple[int, float]) -> None:
        """Treat an unsettled, expired lease as a nack."""
        self.nack(item[0], error="ack timeout")

    def _next_consumer(self) -> str | None:
        """Return the next consumer with room, round-robin, and move the turn on."""
        order = list(self._consumers)
        for i in range(len(order)):
            index = (self._turn + i) % len(order)
            consumer = self._consumers[order[index]]
            if len(consumer.tags) < consumer.prefetch:
                self._turn = (index + 1) % len(order)
                return order[index]
        return None

    def _pop_ready(self) -> Record | None:
        """Pop the best live ready record, discarding stale heap entries."""
        while self._ready:
            _, _, message_id, epoch = heapq.heappop(self._ready)
            record = self._record_for((message_id, epoch))
            if record is not None and record.state is State.READY:
                return record
        return None

    def _dispatch(self) -> None:
        """Lease ready messages to consumers with room, in round-robin order."""
        if self._dispatching or self._closed:
            return
        self._dispatching = True
        try:
            while self._counts[State.READY] and (tag := self._next_consumer()) is not None:
                record = self._pop_ready()
                if record is None:
                    break
                self._deliver(record, tag)
        finally:
            self._dispatching = False

    def _deliver(self, record: Record, consumer_tag: str) -> None:
        """Lease ``record`` to a consumer and hand it to its sink."""
        record.envelope = replace(record.envelope, attempt=record.envelope.attempt + 1)
        record.state = State.LEASED
        self._counts[State.READY] -= 1
        self._counts[State.LEASED] += 1
        tag = next(self._tags)
        deadline = self._loop.time() + self._config.ack_timeout
        self._leases[tag] = _Lease(record.envelope.id, consumer_tag, deadline)
        consumer = self._consumers[consumer_tag]
        consumer.tags.add(tag)
        self._lease_timers.push(deadline, (tag, deadline))
        self._delivered += 1
        try:
            consumer.sink(LeasedMessage(self._name, consumer_tag, tag, record.envelope))
        except Exception:
            logger.exception("sink for consumer %r on queue %r failed", consumer_tag, self._name)
            self.nack(tag, error="sink failed")
