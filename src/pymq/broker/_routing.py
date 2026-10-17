"""The client's sorting room: every push from the broker is put in the right pigeonhole.

A delivery push goes to the consumer named by its tag and its handler runs
in a task of its own. An event push goes to its subscription, whose worker
hands events over one at a time. A push that beats the reply naming its
consumer or subscription waits in an ``EarlyBuffer`` until that reply lands.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from pynet.loop import Task, spawn
from pynet.messaging import Message, Peer

from pymq.broker import protocol as p
from pymq.broker.errors import InvalidRequestError
from pymq.broker.handles import Consumer, Delivery, Settle, Subscription
from pymq.broker.model import JsonValue, QueueConfig
from pymq.broker.router import TopicEvent

logger = logging.getLogger(__name__)

ORPHAN_CANCEL_TIMEOUT = 5.0  # seconds to wait for the server to cancel an orphan


@dataclass
class Registry:
    """Everything a client has set up on the broker, in one place.

    ``queues``, ``bindings``, ``wanted_subscriptions`` and ``wanted_consumers``
    are the record that is replayed after a reconnect, in the order they were
    made. ``consumers`` and ``subscriptions`` map the *current* connection's
    server tags and ids to their handles; a new connection hands out new
    names, so those two maps are emptied whenever the line drops.
    """

    queues: dict[str, QueueConfig | None] = field(default_factory=dict[str, QueueConfig | None])
    bindings: dict[tuple[str, str], None] = field(default_factory=dict[tuple[str, str], None])
    wanted_consumers: list[Consumer] = field(default_factory=list[Consumer])
    wanted_subscriptions: list[Subscription] = field(default_factory=list[Subscription])
    consumers: dict[str, Consumer] = field(default_factory=dict[str, Consumer])  # by server tag
    subscriptions: dict[str, Subscription] = field(default_factory=dict[str, Subscription])

    def forget_connection(self) -> None:
        """Forget the old connection's tags and ids (the handles stay wanted)."""
        for consumer in self.consumers.values():
            consumer.tag = None
        for sub in self.subscriptions.values():
            sub.sub_id = None
        self.consumers.clear()
        self.subscriptions.clear()


class EarlyBuffer[T]:
    """Hold pushes that arrive before the reply naming their consumer or subscription.

    Pushes are only held while at least one start request is in flight;
    once none is, anything still unclaimed is thrown away.
    """

    def __init__(self, kind: str, on_drop: Callable[[str], None] | None = None) -> None:
        """Create an empty buffer for pushes of ``kind`` (used in logs).

        ``on_drop`` is called with each key whose pushes were never claimed.
        """
        self._kind = kind
        self._on_drop = on_drop
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
            keys = list(self._held)
            logger.debug("dropping unclaimed %s pushes for %s", self._kind, keys)
            self._held.clear()
            if self._on_drop is not None:
                for key in keys:
                    self._on_drop(key)

    def clear(self) -> None:
        """Forget everything held (the connection it came from has gone)."""
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

    Every connection gets a new *generation* number and each delivery
    remembers the one it came on, so the client can tell an old parcel
    from a new one. Pushes from an older connection, and pushes that land
    after ``stop``, are dropped.

    Args:
        registry: Where the client's consumers and subscriptions are recorded.
        settle: Sends ack/nack/touch requests for deliveries.

    """

    def __init__(self, registry: Registry, settle: Settle) -> None:
        """Create a router with empty buffers."""
        self._registry = registry
        self._settle = settle
        self.early_deliveries: EarlyBuffer[Delivery] = EarlyBuffer(
            "delivery", on_drop=lambda tag: self._cancel_orphan(tag, self._generation)
        )
        self.early_events: EarlyBuffer[TopicEvent] = EarlyBuffer("event")
        self._tasks: set[Task[None]] = set()
        self._generation = 0  # counts connections; each delivery remembers its own
        self._peer: Peer | None = None
        self._orphans: set[str] = set()  # tags already cancelled on this connection
        self._stopped = False

    @property
    def generation(self) -> int:
        """Return the number of the newest connection (0 before the first)."""
        return self._generation

    def setup(self, peer: Peer) -> None:
        """Register push handlers on a new connection, tagged with its generation."""
        self._generation += 1
        self._peer = peer
        self._orphans = set()
        generation = self._generation
        peer.on(p.PUSH_DELIVER, lambda msg: self.on_deliver(msg, generation))
        peer.on(p.PUSH_EVENT, lambda msg: self.on_event(msg, generation))

    def disconnected(self) -> None:
        """Forget everything tied to the connection that just dropped."""
        self.early_deliveries.clear()
        self.early_events.clear()
        self._registry.forget_connection()

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
            if not self._stopped:
                sub.feed(event)

    def stop(self) -> None:
        """Stop every subscription worker and handler, and ignore later pushes."""
        self._stopped = True
        for sub in self._registry.wanted_subscriptions:
            sub.stop()
        for sub in self._registry.subscriptions.values():
            sub.stop()
        for task in list(self._tasks):
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
        if self._stopped or generation != self._generation:
            logger.debug("dropping delivery for %s: closing or from an old connection", tag)
            return None
        consumer = self._registry.consumers.get(tag)
        if consumer is not None:
            self._dispatch(consumer, delivery)
        elif not self.early_deliveries.hold(tag, delivery):
            logger.debug("dropping delivery for unknown consumer %s", tag)
            self._cancel_orphan(tag, generation)
        return None

    async def on_event(self, msg: Message, generation: int) -> JsonValue:
        """Route an event push to its subscription (or hold it until the id is known)."""
        try:
            sub_id = p.req_str(msg.body, "sub_id")
            event = p.event_from_json(_field(msg.body, "event"), msg.attachments)
        except InvalidRequestError:
            logger.warning("dropping malformed event push")
            return None
        if self._stopped or generation != self._generation:
            logger.debug("dropping event for %s: closing or from an old connection", sub_id)
            return None
        sub = self._registry.subscriptions.get(sub_id)
        if sub is not None:
            sub.feed(event)
        elif not self.early_events.hold(sub_id, event):
            logger.debug("dropping event for unknown subscription %s", sub_id)
        return None

    def _dispatch(self, consumer: Consumer, delivery: Delivery) -> None:
        """Run the consumer's handler for one delivery in its own task."""
        if self._stopped:
            return
        self._track(spawn(_run_handler(consumer, delivery), name=f"pymq-handler-{consumer.queue}"))

    def _cancel_orphan(self, tag: str, generation: int) -> None:
        """Tell the server to stop a consumer we know nothing about (best effort).

        Its parcels would otherwise sit unacked until their leases ran out,
        again and again, using up their attempts.
        """
        peer = self._peer
        if self._stopped or peer is None or generation != self._generation:
            return
        if tag in self._orphans:
            return
        self._orphans.add(tag)
        self._track(spawn(_cancel_on(peer, tag), name=f"pymq-orphan-{tag}"))

    def _track(self, task: Task[None]) -> None:
        """Keep ``task`` until it finishes, so ``stop`` can cancel it."""
        self._tasks.add(task)
        task.add_done_callback(lambda _done: self._tasks.discard(task))


async def _cancel_on(peer: Peer, tag: str) -> None:
    """Send ``consume.cancel`` for ``tag``, logging (not raising) a failure."""
    try:
        await peer.request(p.CONSUME_CANCEL, {"consumer_tag": tag}, timeout=ORPHAN_CANCEL_TIMEOUT)
    except Exception as err:  # noqa: BLE001 -- best effort; the lease expires anyway
        logger.debug("could not cancel orphan consumer %s: %s", tag, err)
    else:
        logger.debug("cancelled orphan consumer %s", tag)


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
