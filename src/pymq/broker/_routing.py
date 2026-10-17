"""The client's sorting room: every push from the broker is put in the right pigeonhole.

A delivery push goes to the consumer named by its tag and its handler runs
in a task of its own. An event push goes to its subscription, whose worker
hands events over one at a time. A push that beats the reply naming its
consumer or subscription waits in an ``EarlyBuffer`` until that reply lands.
"""

import logging
from dataclasses import dataclass, field

from pynet.loop import Task, spawn
from pynet.messaging import Message, Peer

from pymq.broker import protocol as p
from pymq.broker.errors import InvalidRequestError
from pymq.broker.handles import Consumer, Delivery, Settle, Subscription
from pymq.broker.model import JsonValue, QueueConfig
from pymq.broker.router import TopicEvent

logger = logging.getLogger(__name__)


@dataclass
class Registry:
    """Everything a client has set up on the broker, in one place.

    After a reconnect this is what has to be declared, bound and started again.
    """

    queues: dict[str, QueueConfig | None] = field(default_factory=dict[str, QueueConfig | None])
    bindings: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    consumers: dict[str, Consumer] = field(default_factory=dict[str, Consumer])  # by server tag
    subscriptions: dict[str, Subscription] = field(default_factory=dict[str, Subscription])


class EarlyBuffer[T]:
    """Hold pushes that arrive before the reply naming their consumer or subscription.

    Pushes are only held while at least one start request is in flight;
    once none is, anything still unclaimed is thrown away.
    """

    def __init__(self, kind: str) -> None:
        """Create an empty buffer for pushes of ``kind`` (used in logs)."""
        self._kind = kind
        self._waiting = 0
        self._held: dict[str, list[T]] = {}

    @property
    def empty(self) -> bool:
        """Return True when nothing is being held."""
        return not self._held

    def begin(self) -> None:
        """Note that a start request is in flight."""
        self._waiting += 1

    def end(self) -> None:
        """Note that a start request finished; drop leftovers once none are left."""
        self._waiting -= 1
        if self._waiting == 0 and self._held:
            logger.debug("dropping unclaimed %s pushes for %s", self._kind, list(self._held))
            self._held.clear()

    def hold(self, key: str, item: T) -> bool:
        """Keep ``item`` for ``key`` if a start is in flight; return whether it was kept."""
        if self._waiting == 0:
            return False
        self._held.setdefault(key, []).append(item)
        return True

    def take(self, key: str) -> list[T]:
        """Remove and return everything held for ``key``."""
        return self._held.pop(key, [])


class PushRouter:
    """Route delivery and event pushes to the client's consumers and subscriptions.

    Args:
        registry: Where the client's consumers and subscriptions are recorded.
        settle: Sends ack/nack/touch requests for deliveries.

    """

    def __init__(self, registry: Registry, settle: Settle) -> None:
        """Create a router with empty buffers."""
        self._registry = registry
        self._settle = settle
        self.early_deliveries: EarlyBuffer[Delivery] = EarlyBuffer("delivery")
        self.early_events: EarlyBuffer[TopicEvent] = EarlyBuffer("event")
        self._handler_tasks: set[Task[None]] = set()
        self._generation = 0  # counts connections; each delivery remembers its own

    def setup(self, peer: Peer) -> None:
        """Register push handlers on a new connection, tagged with its generation."""
        self._generation += 1
        generation = self._generation
        peer.on(p.PUSH_DELIVER, lambda msg: self.on_deliver(msg, generation))
        peer.on(p.PUSH_EVENT, self.on_event)

    def claim_consumer(self, tag: str, consumer: Consumer) -> None:
        """Record a started consumer and hand it any deliveries that beat the reply."""
        consumer.tag = tag
        self._registry.consumers[tag] = consumer
        for delivery in self.early_deliveries.take(tag):
            self._dispatch(consumer, delivery)

    def claim_subscription(self, sub_id: str, sub: Subscription) -> None:
        """Record a started subscription and feed it any events that beat the reply."""
        sub.sub_id = sub_id
        self._registry.subscriptions[sub_id] = sub
        for event in self.early_events.take(sub_id):
            sub.feed(event)

    def stop(self) -> None:
        """Stop every subscription worker and cancel running handlers."""
        for sub in self._registry.subscriptions.values():
            sub.stop()
        for task in list(self._handler_tasks):
            task.cancel()

    async def on_deliver(self, msg: Message, generation: int) -> JsonValue:
        """Route a delivery push to its consumer (or hold it until the tag is known)."""
        try:
            tag = p.req_str(msg.body, "consumer_tag")
            delivery = Delivery(
                p.envelope_from_json(_field(msg.body, "envelope"), msg.attachments),
                consumer_tag=tag,
                delivery_tag=p.req_int(msg.body, "delivery_tag"),
                generation=generation,
                settle=self._settle,
            )
        except InvalidRequestError:
            logger.warning("dropping malformed delivery push")
            return None
        consumer = self._registry.consumers.get(tag)
        if consumer is not None:
            self._dispatch(consumer, delivery)
        elif not self.early_deliveries.hold(tag, delivery):
            logger.debug("dropping delivery for unknown consumer %s", tag)
        return None

    async def on_event(self, msg: Message) -> JsonValue:
        """Route an event push to its subscription (or hold it until the id is known)."""
        try:
            sub_id = p.req_str(msg.body, "sub_id")
            event = p.event_from_json(_field(msg.body, "event"), msg.attachments)
        except InvalidRequestError:
            logger.warning("dropping malformed event push")
            return None
        sub = self._registry.subscriptions.get(sub_id)
        if sub is not None:
            sub.feed(event)
        elif not self.early_events.hold(sub_id, event):
            logger.debug("dropping event for unknown subscription %s", sub_id)
        return None

    def _dispatch(self, consumer: Consumer, delivery: Delivery) -> None:
        """Run the consumer's handler for one delivery in its own task."""
        task = spawn(_run_handler(consumer, delivery), name=f"pymq-handler-{consumer.queue}")
        self._handler_tasks.add(task)
        task.add_done_callback(lambda _done: self._handler_tasks.discard(task))


async def _run_handler(consumer: Consumer, delivery: Delivery) -> None:
    """Run a handler; with auto-ack, ack on return and nack on error."""
    try:
        try:
            await consumer.handler(delivery)
        except Exception as exc:
            logger.exception("handler for queue %r failed", consumer.queue)
            if consumer.auto_ack and not delivery.settled:
                await delivery.nack(requeue=True, error=f"{type(exc).__name__}: {exc}")
            return
        if consumer.auto_ack and not delivery.settled:
            await delivery.ack()
    except Exception:
        logger.exception("could not settle a delivery from queue %r", consumer.queue)


def _field(body: JsonValue, key: str) -> JsonValue:
    """Return ``body[key]``, raising InvalidRequestError unless body is an object."""
    if not isinstance(body, dict):
        msg = "push body must be an object"
        raise InvalidRequestError(msg)
    return body.get(key)
