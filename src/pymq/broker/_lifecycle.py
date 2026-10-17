"""Starting, stopping and settling: the client's paperwork with the broker.

``Lifecycle`` asks the server for consumers and subscriptions (on first use
and again after every reconnect), cancels them, and sends the ack, nack and
touch requests for deliveries. It knows which connection is current, so a
reply or a delivery from a line that has since dropped is never trusted.
"""

import logging

from pymq.broker import protocol as p
from pymq.broker._replay import replay
from pymq.broker._routing import PushRouter, Registry
from pymq.broker._session import Session
from pymq.broker.errors import BrokerUnavailableError
from pymq.broker.handles import Consumer, Delivery, Subscription
from pymq.broker.protocol import Json

logger = logging.getLogger(__name__)

DROPPED_MESSAGE = "the connection to the broker dropped"


class Lifecycle:
    """Start, cancel and settle over the current connection.

    Args:
        session: The line to the broker.
        router: Routes pushes and counts connections (generations).
        registry: What the client has set up.
        request_timeout: Seconds to wait for each request.

    """

    def __init__(
        self, session: Session, router: PushRouter, registry: Registry, request_timeout: float
    ) -> None:
        """Remember the collaborators."""
        self._session = session
        self._router = router
        self._registry = registry
        self._timeout = request_timeout

    async def now(self, method: str, body: Json) -> Json:
        """Send a request over the line as it is now (no waiting for a reconnect)."""
        return await self._session.request_now(method, body, timeout=self._timeout)

    async def quietly(self, method: str, body: Json) -> None:
        """Send a tidy-up request; a dropped line already tidied up on the server."""
        try:
            await self.now(method, body)
        except BrokerUnavailableError:
            logger.debug("%s skipped: not connected", method)

    async def settle(self, delivery: Delivery, method: str, body: Json) -> bool:
        """Send ack/nack/touch; return False if stale or the connection is gone.

        A delivery that came on an older connection is always stale: the
        server gave its lease back when that line dropped (and may have sent
        the message again since), so no request is sent at all.
        """
        if delivery.generation != self._router.generation:
            return False
        try:
            reply = await self.now(method, body)
        except BrokerUnavailableError:
            return False  # the server released the lease when the line dropped
        return p.opt_bool(reply, "stale") is not True

    async def replay(self) -> None:
        """Set up queues, bindings, subscriptions and consumers on a new line."""
        await replay(
            self._registry,
            self.now,
            lambda consumer: self.start_consumer(consumer, replaying=True),
            lambda sub: self.start_subscription(sub, replaying=True),
        )

    # -- consumers --

    async def start_consumer(self, consumer: Consumer, *, replaying: bool = False) -> None:
        """Ask the server for a consumer, then hand it any deliveries that beat the reply.

        Raises:
            BrokerUnavailableError: If the line dropped before the reply was used
                (the server forgets that connection's consumers anyway).

        """
        generation = self._router.generation
        early = self._router.early_deliveries
        early.begin()
        try:
            body: Json = {"queue": consumer.queue, "prefetch": consumer.prefetch}
            tag = p.req_str(await self.now(p.CONSUME_START, body), "consumer_tag")
            self._check_same_line(generation)
            if replaying and consumer not in self._registry.wanted_consumers:
                await self.quietly(p.CONSUME_CANCEL, {"consumer_tag": tag})  # cancelled meanwhile
                return
            self._router.claim_consumer(tag, consumer)
        finally:
            early.end()

    async def cancel_consumer(self, consumer: Consumer) -> None:
        """Forget a consumer and tell the server (unless the line is down)."""
        if consumer in self._registry.wanted_consumers:
            self._registry.wanted_consumers.remove(consumer)
        tag = consumer.tag
        if tag is None or self._registry.consumers.pop(tag, None) is None:
            return
        consumer.tag = None
        await self.quietly(p.CONSUME_CANCEL, {"consumer_tag": tag})

    # -- subscriptions --

    async def start_subscription(self, sub: Subscription, *, replaying: bool = False) -> None:
        """Ask the server for a subscription, then feed it any events that beat the reply.

        Raises:
            BrokerUnavailableError: If the line dropped before the reply was used.

        """
        generation = self._router.generation
        early = self._router.early_events
        early.begin()
        try:
            body: Json = {"pattern": sub.pattern, "max_pending": sub.max_pending}
            sub_id = p.req_str(await self.now(p.TOPIC_SUBSCRIBE, body), "sub_id")
            self._check_same_line(generation)
            if replaying and sub not in self._registry.wanted_subscriptions:
                await self.quietly(p.TOPIC_UNSUBSCRIBE, {"sub_id": sub_id})  # cancelled meanwhile
                return
            self._router.claim_subscription(sub_id, sub)
        finally:
            early.end()

    async def cancel_subscription(self, sub: Subscription) -> None:
        """Forget a subscription and tell the server (unless the line is down)."""
        if sub in self._registry.wanted_subscriptions:
            self._registry.wanted_subscriptions.remove(sub)
        sub.stop()
        sub_id = sub.sub_id
        if sub_id is None or self._registry.subscriptions.pop(sub_id, None) is None:
            return
        sub.sub_id = None
        await self.quietly(p.TOPIC_UNSUBSCRIBE, {"sub_id": sub_id})

    def _check_same_line(self, generation: int) -> None:
        """Raise BrokerUnavailableError if the line dropped since ``generation``."""
        if self._router.generation != generation or not self._session.connected:
            raise BrokerUnavailableError(DROPPED_MESSAGE)
