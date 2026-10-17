"""The broker client: a phone line to the post office.

``BrokerClient`` dials a ``BrokerServer`` with PyNet's ``ReconnectingClient``
and turns every call into a request over that line. Parcels for your
consumers and events for your subscriptions arrive as pushes; each parcel's
handler runs in its own task (so up to ``prefetch`` run at once), while one
subscription's events are handled one at a time, in order.

When the line drops the client redials, replays its setup (queues,
bindings, subscriptions, consumers) and carries on. Requests made while
it is down wait for it to come back (up to their timeout), parcels from
before the drop can no longer be acked, and ``put`` resends with the same
message id so the broker keeps just one copy.

Example:
    async with BrokerClient("127.0.0.1", port) as client:
        await client.declare_queue("jobs")
        await client.put("jobs", {"task": "wash the car"})

"""

import logging
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from types import TracebackType
from typing import Any, Self

from pynet.messaging import ConnectionEvent, ReconnectingClient

from pymq.broker import protocol as p
from pymq.broker._lifecycle import Lifecycle
from pymq.broker._replay import declare_body
from pymq.broker._routing import PushRouter, Registry
from pymq.broker._session import Session
from pymq.broker.core import PublishResult
from pymq.broker.errors import InvalidRequestError
from pymq.broker.handles import (
    Consumer,
    Delivery,
    DeliveryHandler,
    EventHandler,
    Subscription,
)
from pymq.broker.model import (
    DeadLetter,
    JsonValue,
    PutResult,
    QueueConfig,
    QueueStats,
    validate_queue_name,
)
from pymq.broker.protocol import Json
from pymq.broker.topics import validate_pattern, validate_topic

logger = logging.getLogger(__name__)

DEFAULT_PUT_TIMEOUT = 30.0
DEFAULT_REQUEST_TIMEOUT = 10.0
DEFAULT_PREFETCH = 10
DEFAULT_MAX_PENDING = 1_000

__all__ = [
    "BrokerClient",
    "Consumer",
    "Delivery",
    "DeliveryHandler",
    "EventHandler",
    "Subscription",
]


class BrokerClient:
    """Talk to a ``BrokerServer`` over the network.

    Args:
        host: The server's host name or address.
        port: The server's port.
        put_timeout: Seconds a ``put`` may take in total, resends included.
        request_timeout: Seconds to wait for any other request (including
            waiting for a dropped line to come back).
        **reconnect_kwargs: Passed on to ``ReconnectingClient`` (for example
            ``initial_backoff`` or ``heartbeat_interval``).

    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        put_timeout: float = DEFAULT_PUT_TIMEOUT,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        **reconnect_kwargs: Any,
    ) -> None:
        """Remember where the broker is; nothing is dialled until ``start``."""
        self._put_timeout = put_timeout
        self._request_timeout = request_timeout
        self._registry = Registry()
        self._router = PushRouter(self._registry, lambda d, m, b: self._life.settle(d, m, b))
        self._rc = ReconnectingClient(host, port, setup=self._router.setup, **reconnect_kwargs)
        self._session = Session(
            self._rc, replay=lambda: self._life.replay(), on_drop=self._router.disconnected
        )
        self._life = Lifecycle(self._session, self._router, self._registry, request_timeout)
        self._closed = False

    # -- lifecycle --

    @property
    def connected(self) -> bool:
        """Return True while there is a live connection."""
        return self._rc.connected

    async def start(self) -> None:
        """Start dialling the broker in the background."""
        self._rc.start()

    async def wait_connected(self, timeout: float | None = None) -> None:
        """Wait until connected and set up again (``TimeoutError`` after ``timeout``)."""
        await self._session.wait_ready(timeout)

    async def close(self) -> None:
        """Stop handlers and subscriptions and hang up (safe to call twice).

        Pushes that land while the line is being closed are dropped: no new
        handler or subscription worker starts once ``close`` has begun.

        Handlers still running are cancelled, auto-ack ones included, so a
        delivery whose handler had not yet been acked (even one that had
        just finished its work) is not acked. The broker takes its lease
        back when the line closes and hands it out again: delivery is
        at-least-once, so that work may be done twice. Settle by hand and
        wait for your handlers before closing if that matters.
        """
        if self._closed:
            return
        self._closed = True
        self._session.close()
        self._router.stop()
        await self._rc.stop()

    def on_event(self, cb: Callable[[ConnectionEvent], None]) -> None:
        """Call ``cb`` whenever the connection comes, goes or comes back."""
        self._rc.on_event(cb)

    async def __aenter__(self) -> Self:
        """Start and wait (up to ``request_timeout``) for the first connection."""
        await self.start()
        try:
            await self.wait_connected(self._request_timeout)
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the client."""
        await self.close()

    # -- queues --

    async def declare_queue(self, name: str, config: QueueConfig | None = None) -> QueueConfig:
        """Create a queue (or check an existing one) and return its config."""
        validate_queue_name(name)
        reply = await self._call(p.QUEUE_DECLARE, declare_body(name, config))
        self._registry.queues[name] = config
        return p.config_from_json(reply.get("config"))

    async def delete_queue(self, name: str) -> None:
        """Delete a queue and everything in it (its bindings and consumers too)."""
        validate_queue_name(name)
        await self._call(p.QUEUE_DELETE, {"name": name})
        reg = self._registry
        reg.queues.pop(name, None)
        reg.bindings = {b: None for b in reg.bindings if b[1] != name}
        for consumer in [c for c in reg.wanted_consumers if c.queue == name]:
            reg.wanted_consumers.remove(consumer)
            if consumer.tag is not None:
                reg.consumers.pop(consumer.tag, None)
                consumer.tag = None

    async def purge_queue(self, name: str) -> int:
        """Throw away a queue's waiting messages and return how many."""
        validate_queue_name(name)
        return p.req_int(await self._call(p.QUEUE_PURGE, {"name": name}), "count")

    async def list_queues(self) -> list[str]:
        """Return every queue name."""
        return p.opt_str_list(await self._call(p.QUEUE_LIST), "queues")

    async def stats(self, name: str) -> QueueStats:
        """Return one queue's stats."""
        validate_queue_name(name)
        return p.stats_from_json(await self._call(p.QUEUE_STATS, {"name": name}))

    async def broker_stats(self) -> dict[str, JsonValue]:
        """Return the broker's counts, connections and event drops."""
        return await self._call(p.BROKER_STATS)

    # -- messages --

    async def put(
        self,
        queue: str,
        body: JsonValue,
        *,
        id: str | None = None,  # noqa: A002 -- the spec's name for the message id
        priority: int = 0,
        headers: Mapping[str, str] | None = None,
        attachments: Sequence[bytes] = (),
        delay: float = 0.0,
    ) -> PutResult:
        """Put a message on a queue and return its id (and whether it was a duplicate).

        A message id is made up here when none is given, so if the line drops
        before the answer comes the very same message can be sent again once
        it is back; the broker keeps only one copy. A resend that finds its
        own first attempt already stored reports ``duplicate=True``.

        Raises:
            BrokerUnavailableError: If no answer came within ``put_timeout``.

        """
        validate_queue_name(queue)
        request: Json = {
            "id": id if id is not None else uuid.uuid4().hex,
            "queue": queue,
            "body": body,
            "priority": priority,
            "headers": dict(headers or {}),
            "delay": delay,
        }
        reply = await self._session.resend_until_answered(
            p.MSG_PUT, request, attachments=attachments, timeout=self._put_timeout
        )
        return PutResult(p.req_str(reply, "id"), p.opt_bool(reply, "duplicate") is True)

    async def consume(
        self,
        queue: str,
        handler: DeliveryHandler,
        *,
        prefetch: int = DEFAULT_PREFETCH,
        auto_ack: bool = True,
    ) -> Consumer:
        """Start receiving a queue's messages; ``handler`` runs once per delivery."""
        validate_queue_name(queue)
        consumer = Consumer(
            queue, handler, prefetch=prefetch, auto_ack=auto_ack, cancel=self._life.cancel_consumer
        )
        await self._session.wait_ready_or_unavailable(self._request_timeout)
        await self._life.start_consumer(consumer)
        self._registry.wanted_consumers.append(consumer)
        return consumer

    # -- topics --

    async def publish(
        self,
        topic: str,
        body: JsonValue,
        *,
        headers: Mapping[str, str] | None = None,
        attachments: Sequence[bytes] = (),
    ) -> PublishResult:
        """Announce an event on a topic."""
        validate_topic(topic)
        request: Json = {"topic": topic, "body": body, "headers": dict(headers or {})}
        reply = await self._call(p.TOPIC_PUBLISH, request, attachments=attachments)
        return p.publish_result_from_json(reply)

    async def subscribe(
        self, pattern: str, handler: EventHandler, *, max_pending: int = DEFAULT_MAX_PENDING
    ) -> Subscription:
        """Receive events matching ``pattern``; ``handler`` sees them one at a time."""
        validate_pattern(pattern)
        sub = Subscription(
            pattern, handler, max_pending=max_pending, cancel=self._life.cancel_subscription
        )
        await self._session.wait_ready_or_unavailable(self._request_timeout)
        await self._life.start_subscription(sub)
        self._registry.wanted_subscriptions.append(sub)
        return sub

    async def bind(self, pattern: str, queue: str) -> None:
        """Copy events matching ``pattern`` into ``queue``."""
        validate_pattern(pattern)
        validate_queue_name(queue)
        await self._call(p.TOPIC_BIND, {"pattern": pattern, "queue": queue})
        self._registry.bindings[pattern, queue] = None

    async def unbind(self, pattern: str, queue: str) -> None:
        """Stop copying events matching ``pattern`` into ``queue``."""
        validate_pattern(pattern)
        validate_queue_name(queue)
        await self._call(p.TOPIC_UNBIND, {"pattern": pattern, "queue": queue})
        self._registry.bindings.pop((pattern, queue), None)

    # -- dead letters --

    async def dead_letters(self, queue: str, limit: int | None = None) -> list[DeadLetter]:
        """Return a queue's dead letters (attachments are not included)."""
        validate_queue_name(queue)
        request: Json = {"queue": queue}
        if limit is not None:
            request["limit"] = limit
        letters = (await self._call(p.DEAD_LIST, request)).get("letters")
        if not isinstance(letters, list):
            msg = "dead.list reply has no 'letters' list"
            raise InvalidRequestError(msg)
        return [p.dead_letter_from_json(v) for v in letters]

    async def replay_dead(self, queue: str, ids: Collection[str] | None = None) -> int:
        """Put dead letters back on the queue (all of them if ``ids`` is None)."""
        validate_queue_name(queue)
        request: Json = {"queue": queue}
        if ids is not None:
            request["ids"] = list(ids)
        return p.req_int(await self._call(p.DEAD_REPLAY, request), "count")

    async def purge_dead(self, queue: str) -> int:
        """Forget a queue's dead letters and return how many."""
        validate_queue_name(queue)
        return p.req_int(await self._call(p.DEAD_PURGE, {"queue": queue}), "count")

    # -- requests --

    async def _call(
        self, method: str, body: JsonValue = None, *, attachments: Sequence[bytes] = ()
    ) -> Json:
        """Send a request once the line is up, within ``request_timeout``."""
        return await self._session.request(
            method, body, attachments=attachments, timeout=self._request_timeout
        )
