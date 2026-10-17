"""Handles the client gives back: a parcel in your hands, a job, a radio channel.

A ``Delivery`` is one parcel the broker lent you; you sign for it (``ack``),
hand it back (``nack``) or ask for more time (``touch``). A ``Consumer`` is
your standing order to receive parcels from one queue, and a
``Subscription`` is the radio channel you tuned to. Each can be cancelled.
"""

import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping

from pynet.loop import Event, Task, spawn

from pymq.broker.model import Envelope, JsonValue
from pymq.broker.protocol import MSG_ACK, MSG_NACK, MSG_TOUCH, Json
from pymq.broker.router import TopicEvent

logger = logging.getLogger(__name__)

type Settle = Callable[[Delivery, str, Json], Awaitable[bool]]
"""Send a settle request for a delivery; return True unless it was stale."""

type DeliveryHandler = Callable[[Delivery], Awaitable[None]]
type EventHandler = Callable[[TopicEvent], Awaitable[None]]


class Delivery:
    """One message leased to one of our consumers.

    Args:
        envelope: The message and its metadata.
        consumer_tag: The server tag of the consumer it was leased to.
        delivery_tag: The lease's tag on its queue.
        generation: Which connection (counting from 1) it arrived on.
        settle: Sends ack/nack/touch requests for this delivery.

    """

    def __init__(
        self,
        envelope: Envelope,
        *,
        consumer_tag: str,
        delivery_tag: int,
        generation: int,
        settle: Settle,
    ) -> None:
        """Wrap a leased envelope."""
        self.envelope = envelope
        self.consumer_tag = consumer_tag
        self.delivery_tag = delivery_tag
        self.generation = generation
        self._settle = settle
        self._settled = False

    @property
    def queue(self) -> str:
        """Return the queue it came from."""
        return self.envelope.queue

    @property
    def id(self) -> str:
        """Return the message id."""
        return self.envelope.id

    @property
    def body(self) -> JsonValue:
        """Return the message body."""
        return self.envelope.body

    @property
    def headers(self) -> Mapping[str, str]:
        """Return the message headers."""
        return self.envelope.headers

    @property
    def attachments(self) -> tuple[bytes, ...]:
        """Return the message attachments."""
        return self.envelope.attachments

    @property
    def priority(self) -> int:
        """Return the message priority."""
        return self.envelope.priority

    @property
    def attempt(self) -> int:
        """Return which delivery attempt this is (1 for the first)."""
        return self.envelope.attempt

    @property
    def settled(self) -> bool:
        """Return True once ack or nack has been called."""
        return self._settled

    def _ids(self) -> Json:
        """Return the fields that name this delivery on the wire."""
        return {
            "queue": self.queue,
            "consumer_tag": self.consumer_tag,
            "delivery_tag": self.delivery_tag,
        }

    async def ack(self) -> bool:
        """Acknowledge the message; return False if stale or already settled."""
        if self._settled:
            return False
        self._settled = True
        return await self._settle(self, MSG_ACK, self._ids())

    async def nack(self, *, requeue: bool = True, error: str | None = None) -> bool:
        """Hand the message back (to retry, or dead-letter it if ``requeue`` is False).

        Returns:
            False if the lease was stale or the delivery was already settled.

        """
        if self._settled:
            return False
        self._settled = True
        body = self._ids()
        body["requeue"] = requeue
        if error is not None:
            body["error"] = error
        return await self._settle(self, MSG_NACK, body)

    async def touch(self) -> bool:
        """Renew the lease; return False if stale or already settled."""
        if self._settled:
            return False
        return await self._settle(self, MSG_TOUCH, self._ids())


class Consumer:
    """A standing order to receive one queue's messages.

    Args:
        queue: The queue to consume.
        handler: Called with each delivery.
        prefetch: How many deliveries may be in flight at once.
        auto_ack: Ack after the handler returns, nack if it raises.
        cancel: Asks the client to cancel this consumer.

    """

    def __init__(
        self,
        queue: str,
        handler: DeliveryHandler,
        *,
        prefetch: int,
        auto_ack: bool,
        cancel: Callable[[Consumer], Awaitable[None]],
    ) -> None:
        """Record the consumer's settings; ``tag`` is set once the server answers."""
        self.queue = queue
        self.handler = handler
        self.prefetch = prefetch
        self.auto_ack = auto_ack
        self.tag: str | None = None
        self._cancel = cancel

    async def cancel(self) -> None:
        """Stop receiving messages (safe to call twice)."""
        await self._cancel(self)


class Subscription:
    """A live topic subscription whose handler sees events one at a time, in order.

    Args:
        pattern: The topic pattern subscribed to.
        handler: Called with each event.
        max_pending: How many events the server may hold for us.
        cancel: Asks the client to cancel this subscription.

    """

    def __init__(
        self,
        pattern: str,
        handler: EventHandler,
        *,
        max_pending: int,
        cancel: Callable[[Subscription], Awaitable[None]],
    ) -> None:
        """Record the subscription; ``sub_id`` is set once the server answers."""
        self.pattern = pattern
        self.handler = handler
        self.max_pending = max_pending
        self.sub_id: str | None = None
        self._cancel = cancel
        self._events: deque[TopicEvent] = deque()
        self._wake = Event()
        self._worker: Task[None] | None = None

    async def cancel(self) -> None:
        """Stop receiving events (safe to call twice)."""
        await self._cancel(self)

    def feed(self, event: TopicEvent) -> None:
        """Queue an event for the handler, starting the worker if needed."""
        self._events.append(event)
        self._wake.set()
        if self._worker is None:
            self._worker = spawn(self._run(), name=f"pymq-sub-{self.pattern}")

    def stop(self) -> None:
        """Stop the worker and drop events not yet handled."""
        self._events.clear()
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None

    async def _run(self) -> None:
        """Hand events to the handler one by one, logging any failure."""
        while True:
            if not self._events:
                self._wake.clear()
                await self._wake.wait()
                continue
            event = self._events.popleft()
            try:
                await self.handler(event)
            except Exception:
                logger.exception("event handler for %r failed", self.pattern)
