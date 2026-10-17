"""One connection's request handlers: the counter clerk for a single customer.

Each connection gets its own clerk. The clerk reads the request form with
the ``protocol`` readers, asks the broker, and writes back a JSON answer.
The clerk also remembers which consumers and subscriptions belong to this
customer, so nobody can settle or cancel someone else's work, and tidies
them all away when the customer leaves.
"""

import logging
from collections.abc import Awaitable, Callable

from pynet.messaging import Message, Peer

from pymq.broker import protocol as p
from pymq.broker.core import Broker
from pymq.broker.errors import BrokerError, InvalidRequestError, QueueNotFoundError
from pymq.broker.model import JsonValue
from pymq.broker.outbox import ConnectionOutbox
from pymq.broker.protocol import Json
from pymq.broker.router import TopicEvent

logger = logging.getLogger(__name__)

DEFAULT_PREFETCH = 10

type RequestHandler = Callable[[Message], Json]


class Connection:
    """Serve one peer's requests and own its consumers and subscriptions.

    Args:
        peer: The connection's pynet peer.
        broker: The broker every request acts on.
        max_pending: The default event limit per subscription.
        broker_stats: Build the ``broker.stats`` reply.
        on_closed: Called with this connection once it has been cleaned up.

    """

    def __init__(
        self,
        peer: Peer,
        broker: Broker,
        max_pending: int,
        broker_stats: Callable[[], Json],
        on_closed: Callable[[Connection], None],
    ) -> None:
        """Register every handler on ``peer`` and start the outbox."""
        self.peer = peer
        self.outbox = ConnectionOutbox(peer, max_pending=max_pending)
        self._broker = broker
        self._max_pending = max_pending
        self._on_closed = on_closed
        self._consumers: dict[str, str] = {}  # consumer tag -> queue
        self._subs: set[str] = set()
        table: dict[str, RequestHandler] = {
            p.QUEUE_DECLARE: self._declare,
            p.QUEUE_DELETE: self._delete,
            p.QUEUE_PURGE: self._purge,
            p.QUEUE_STATS: self._stats,
            p.QUEUE_LIST: self._list,
            p.BROKER_STATS: lambda _msg: broker_stats(),
            p.MSG_PUT: self._put,
            p.MSG_ACK: self._ack,
            p.MSG_NACK: self._nack,
            p.MSG_TOUCH: self._touch,
            p.CONSUME_START: self._consume,
            p.CONSUME_CANCEL: self._cancel,
            p.TOPIC_PUBLISH: self._publish,
            p.TOPIC_SUBSCRIBE: self._subscribe,
            p.TOPIC_UNSUBSCRIBE: self._unsubscribe,
            p.TOPIC_BIND: self._bind,
            p.TOPIC_UNBIND: self._unbind,
            p.DEAD_LIST: self._dead_list,
            p.DEAD_REPLAY: self._dead_replay,
            p.DEAD_PURGE: self._dead_purge,
        }
        for method, handler in table.items():
            peer.on(method, _guarded(handler))
        peer.on_close(self._closed)
        self.outbox.start()

    # -- queues --

    def _declare(self, msg: Message) -> Json:
        """Declare a queue and return its config."""
        name = p.req_str(msg.body, "name")
        raw = _obj(msg.body).get("config")
        config = None if raw is None else p.config_from_json(raw)
        return {"config": p.config_to_json(self._broker.declare_queue(name, config))}

    def _delete(self, msg: Message) -> Json:
        """Delete a queue."""
        self._broker.delete_queue(p.req_str(msg.body, "name"))
        return _OK

    def _purge(self, msg: Message) -> Json:
        """Purge a queue's waiting messages."""
        return {"count": self._broker.purge_queue(p.req_str(msg.body, "name"))}

    def _stats(self, msg: Message) -> Json:
        """Return one queue's stats."""
        return p.stats_to_json(self._broker.stats(p.req_str(msg.body, "name")))

    def _list(self, msg: Message) -> Json:
        """List queue names."""
        if msg.body is not None:
            _obj(msg.body)
        return {"queues": list(self._broker.list_queues())}

    # -- messages --

    def _put(self, msg: Message) -> Json:
        """Put a message, with the request's attachments."""
        body = msg.body
        result = self._broker.put(
            p.req_str(body, "queue"),
            _obj(body).get("body"),
            id=p.opt_str(body, "id"),
            priority=p.opt_int(body, "priority") or 0,
            headers=p.opt_headers(body, "headers"),
            attachments=msg.attachments,
            delay=p.opt_float(body, "delay") or 0.0,
        )
        return {"id": result.id, "duplicate": result.duplicate}

    def _owned(self, msg: Message) -> tuple[str, int, str] | None:
        """Return ``(queue, delivery_tag, consumer_tag)`` if this connection owns the consumer."""
        queue = p.req_str(msg.body, "queue")
        tag = p.req_str(msg.body, "consumer_tag")
        delivery_tag = p.req_int(msg.body, "delivery_tag")
        if self._consumers.get(tag) != queue:
            return None
        return queue, delivery_tag, tag

    @staticmethod
    def _settle(owned: tuple[str, int, str] | None, act: Callable[[str, int, str], bool]) -> Json:
        """Run ``act`` on an owned delivery; unowned, another consumer's or deleted means stale."""
        if owned is None:
            return {"stale": True}
        try:
            return {"stale": not act(*owned)}
        except QueueNotFoundError:
            return {"stale": True}

    def _ack(self, msg: Message) -> Json:
        """Ack a delivery held by one of this connection's consumers."""
        return self._settle(
            self._owned(msg), lambda q, d, c: self._broker.ack(q, d, consumer_tag=c)
        )

    def _nack(self, msg: Message) -> Json:
        """Nack a delivery held by one of this connection's consumers."""
        owned = self._owned(msg)
        requeue = p.opt_bool(msg.body, "requeue") is not False
        error = p.opt_str(msg.body, "error")
        return self._settle(
            owned,
            lambda q, d, c: self._broker.nack(q, d, requeue=requeue, error=error, consumer_tag=c),
        )

    def _touch(self, msg: Message) -> Json:
        """Renew a lease held by one of this connection's consumers."""
        return self._settle(
            self._owned(msg), lambda q, d, c: self._broker.touch(q, d, consumer_tag=c)
        )

    def _consume(self, msg: Message) -> Json:
        """Start a consumer whose deliveries go through this connection's outbox."""
        queue = p.req_str(msg.body, "queue")
        prefetch = p.opt_int(msg.body, "prefetch")
        tag = self._broker.consume(
            queue,
            self.outbox.send_delivery,
            prefetch=DEFAULT_PREFETCH if prefetch is None else prefetch,
        )
        self._consumers[tag] = queue
        return {"consumer_tag": tag}

    def _cancel(self, msg: Message) -> Json:
        """Cancel an owned consumer (an unknown tag is ignored)."""
        tag = p.req_str(msg.body, "consumer_tag")
        if self._consumers.pop(tag, None) is not None:
            self._broker.cancel(tag)
        return _OK

    # -- topics --

    def _publish(self, msg: Message) -> Json:
        """Publish an event, with the request's attachments."""
        result = self._broker.publish(
            p.req_str(msg.body, "topic"),
            _obj(msg.body).get("body"),
            headers=p.opt_headers(msg.body, "headers"),
            attachments=msg.attachments,
        )
        return p.publish_result_to_json(result)

    def _subscribe(self, msg: Message) -> Json:
        """Subscribe this connection's outbox to a pattern."""
        pattern = p.req_str(msg.body, "pattern")
        limit = p.opt_int(msg.body, "max_pending")
        if limit is not None and limit < 1:
            text = "field 'max_pending' must be >= 1"
            raise InvalidRequestError(text)
        sub_id = self._broker.subscribe(pattern, self._send_event)
        self.outbox.add_subscription(sub_id, self._max_pending if limit is None else limit)
        self._subs.add(sub_id)
        return {"sub_id": sub_id}

    def _send_event(self, sub_id: str, event: TopicEvent) -> None:
        """Hand an event to the outbox."""
        self.outbox.send_event(sub_id, event)

    def _unsubscribe(self, msg: Message) -> Json:
        """Cancel an owned subscription (an unknown id is ignored)."""
        sub_id = p.req_str(msg.body, "sub_id")
        if sub_id in self._subs:
            self._subs.discard(sub_id)
            self._broker.unsubscribe(sub_id)
            self.outbox.forget(sub_id)
        return _OK

    def _bind(self, msg: Message) -> Json:
        """Bind a queue to a pattern."""
        self._broker.bind(p.req_str(msg.body, "pattern"), p.req_str(msg.body, "queue"))
        return _OK

    def _unbind(self, msg: Message) -> Json:
        """Remove a binding."""
        self._broker.unbind(p.req_str(msg.body, "pattern"), p.req_str(msg.body, "queue"))
        return _OK

    # -- dead letters --

    def _dead_list(self, msg: Message) -> Json:
        """List a queue's dead letters."""
        letters = self._broker.dead_letters(
            p.req_str(msg.body, "queue"), p.opt_int(msg.body, "limit")
        )
        return {"letters": [p.dead_letter_to_json(d) for d in letters]}

    def _dead_replay(self, msg: Message) -> Json:
        """Replay some or all dead letters."""
        queue = p.req_str(msg.body, "queue")
        ids = p.opt_str_list(msg.body, "ids") if _obj(msg.body).get("ids") is not None else None
        return {"count": self._broker.replay_dead(queue, ids)}

    def _dead_purge(self, msg: Message) -> Json:
        """Forget a queue's dead letters."""
        return {"count": self._broker.purge_dead(p.req_str(msg.body, "queue"))}

    # -- closing --

    def _closed(self, cause: BaseException | None) -> None:
        """Cancel consumers, drop subscriptions and close the outbox."""
        logger.debug("connection %s closed (%s)", self.peer.peername, cause)
        for tag in self._consumers:
            self._broker.cancel(tag)
        self._consumers.clear()
        for sub_id in self._subs:
            self._broker.unsubscribe(sub_id)
        self._subs.clear()
        self.outbox.close()
        self._on_closed(self)


_OK: Json = {"ok": True}


def _obj(body: JsonValue) -> dict[str, JsonValue]:
    """Return ``body`` if it is a JSON object, else raise InvalidRequestError."""
    if not isinstance(body, dict):
        msg = "request body must be an object"
        raise InvalidRequestError(msg)
    return body


def _guarded(handler: RequestHandler) -> Callable[[Message], Awaitable[Json]]:
    """Wrap a sync handler: run it and turn broker errors into RemoteError."""

    async def run(msg: Message) -> Json:
        """Run the handler, translating BrokerError and ValueError."""
        try:
            return handler(msg)
        except (BrokerError, ValueError) as err:
            raise p.error_to_remote(err) from err

    return run
