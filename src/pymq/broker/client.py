"""The broker client: a phone line to the post office.

``BrokerClient`` dials a ``BrokerServer`` with PyNet's ``ReconnectingClient``
and turns every call into a request over that line. Parcels for your
consumers and events for your subscriptions arrive as pushes; each parcel's
handler runs in its own task (so up to ``prefetch`` run at once), while one
subscription's events are handled one at a time, in order.

Example:
    async with BrokerClient("127.0.0.1", port) as client:
        await client.declare_queue("jobs")
        await client.put("jobs", {"task": "wash the car"})

"""

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from types import TracebackType
from typing import Any, Self

from pynet.errors import ConnectionClosedError, NotConnectedError, RemoteError
from pynet.messaging import ConnectionEvent, ReconnectingClient

from pymq.broker import protocol as p
from pymq.broker._routing import PushRouter, Registry
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
        put_timeout: Seconds to wait for a ``put`` to be answered.
        request_timeout: Seconds to wait for any other request.
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
        self._router = PushRouter(self._registry, self._settle)
        self._rc = ReconnectingClient(host, port, setup=self._router.setup, **reconnect_kwargs)
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
        """Wait until connected (``TimeoutError`` after ``timeout`` seconds)."""
        await self._rc.wait_connected(timeout)

    async def close(self) -> None:
        """Stop handlers and subscriptions and hang up (safe to call twice)."""
        if self._closed:
            return
        self._closed = True
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
        body: Json = {"name": name}
        if config is not None:
            body["config"] = p.config_to_json(config)
        reply = await self._call(p.QUEUE_DECLARE, body)
        self._registry.queues[name] = config
        return p.config_from_json(reply.get("config"))

    async def delete_queue(self, name: str) -> None:
        """Delete a queue and everything in it."""
        validate_queue_name(name)
        await self._call(p.QUEUE_DELETE, {"name": name})
        self._registry.queues.pop(name, None)
        self._registry.bindings = {b for b in self._registry.bindings if b[1] != name}

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
        """Put a message on a queue and return its id (and whether it was a duplicate)."""
        validate_queue_name(queue)
        request: Json = {
            "queue": queue,
            "body": body,
            "priority": priority,
            "headers": dict(headers or {}),
            "delay": delay,
        }
        if id is not None:
            request["id"] = id
        reply = await self._call(
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
            queue, handler, prefetch=prefetch, auto_ack=auto_ack, cancel=self._cancel_consumer
        )
        await self._start_consumer(consumer)
        return consumer

    async def _start_consumer(self, consumer: Consumer) -> None:
        """Ask the server for a consumer, then hand it any deliveries that beat the reply."""
        early = self._router.early_deliveries
        early.begin()
        try:
            reply = await self._call(
                p.CONSUME_START, {"queue": consumer.queue, "prefetch": consumer.prefetch}
            )
            self._router.claim_consumer(p.req_str(reply, "consumer_tag"), consumer)
        finally:
            early.end()

    async def _cancel_consumer(self, consumer: Consumer) -> None:
        """Forget a consumer and tell the server (unless the line is down)."""
        tag = consumer.tag
        if tag is None or self._registry.consumers.pop(tag, None) is None:
            return
        consumer.tag = None
        await self._quietly(p.CONSUME_CANCEL, {"consumer_tag": tag})

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
            pattern, handler, max_pending=max_pending, cancel=self._cancel_subscription
        )
        await self._start_subscription(sub)
        return sub

    async def _start_subscription(self, sub: Subscription) -> None:
        """Ask the server for a subscription, then feed it any events that beat the reply."""
        early = self._router.early_events
        early.begin()
        try:
            reply = await self._call(
                p.TOPIC_SUBSCRIBE, {"pattern": sub.pattern, "max_pending": sub.max_pending}
            )
            self._router.claim_subscription(p.req_str(reply, "sub_id"), sub)
        finally:
            early.end()

    async def _cancel_subscription(self, sub: Subscription) -> None:
        """Forget a subscription and tell the server (unless the line is down)."""
        sub_id = sub.sub_id
        if sub_id is None or self._registry.subscriptions.pop(sub_id, None) is None:
            return
        sub.sub_id = None
        sub.stop()
        await self._quietly(p.TOPIC_UNSUBSCRIBE, {"sub_id": sub_id})

    async def bind(self, pattern: str, queue: str) -> None:
        """Copy events matching ``pattern`` into ``queue``."""
        validate_pattern(pattern)
        validate_queue_name(queue)
        await self._call(p.TOPIC_BIND, {"pattern": pattern, "queue": queue})
        self._registry.bindings.add((pattern, queue))

    async def unbind(self, pattern: str, queue: str) -> None:
        """Stop copying events matching ``pattern`` into ``queue``."""
        validate_pattern(pattern)
        validate_queue_name(queue)
        await self._call(p.TOPIC_UNBIND, {"pattern": pattern, "queue": queue})
        self._registry.bindings.discard((pattern, queue))

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
        self,
        method: str,
        body: JsonValue = None,
        *,
        attachments: Sequence[bytes] = (),
        timeout: float | None = None,
    ) -> Json:
        """Send a request and return its object reply, converting broker errors."""
        try:
            reply = await self._rc.request(
                method,
                body,
                attachments=attachments,
                timeout=self._request_timeout if timeout is None else timeout,
            )
        except RemoteError as err:
            converted = p.remote_to_error(err)
            if converted is err:
                raise
            raise converted from err
        return _obj(reply.body)

    async def _quietly(self, method: str, body: Json) -> None:
        """Send a tidy-up request; a dropped line already tidied up on the server."""
        try:
            await self._call(method, body)
        except NotConnectedError, ConnectionClosedError:
            logger.debug("%s skipped: not connected", method)

    async def _settle(self, _delivery: Delivery, method: str, body: Json) -> bool:
        """Send ack/nack/touch; return False if stale or the connection is gone.

        The delivery is passed so a check of which connection it came from can
        be added here (an ack from an old connection is always stale).
        """
        try:
            reply = await self._call(method, body)
        except NotConnectedError, ConnectionClosedError:
            return False  # the server released the lease when the line dropped
        return p.opt_bool(reply, "stale") is not True


def _obj(body: JsonValue) -> Json:
    """Return ``body`` if it is a JSON object, else raise InvalidRequestError."""
    if not isinstance(body, dict):
        msg = "reply body must be an object"
        raise InvalidRequestError(msg)
    return body
