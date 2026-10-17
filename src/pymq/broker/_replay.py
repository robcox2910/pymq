"""Remind the broker what we were doing: replay the client's setup after a reconnect.

Like ringing a friend back after the call dropped and saying "where were
we?": the queues are declared again, then the bindings, then the
subscriptions, then the consumers, in the order they were first made.
A step that fails is logged and the rest still run.
"""

import logging
from collections.abc import Awaitable, Callable

from pymq.broker import protocol as p
from pymq.broker._routing import Registry
from pymq.broker.handles import Consumer, Subscription
from pymq.broker.model import QueueConfig
from pymq.broker.protocol import Json

logger = logging.getLogger(__name__)

type Call = Callable[[str, Json], Awaitable[Json]]


def declare_body(name: str, config: QueueConfig | None) -> Json:
    """Return the ``queue.declare`` request body for ``name``."""
    body: Json = {"name": name}
    if config is not None:
        body["config"] = p.config_to_json(config)
    return body


async def replay(
    registry: Registry,
    call: Call,
    start_consumer: Callable[[Consumer], Awaitable[None]],
    start_subscription: Callable[[Subscription], Awaitable[None]],
) -> None:
    """Declare queues, bind, subscribe and consume again, in that order.

    Args:
        registry: What the client has set up.
        call: Sends one request over the new connection.
        start_consumer: Starts a recorded consumer again.
        start_subscription: Starts a recorded subscription again.

    """
    for name, config in list(registry.queues.items()):
        await _step(f"declare queue {name!r}", call(p.QUEUE_DECLARE, declare_body(name, config)))
    for pattern, queue in list(registry.bindings):
        body: Json = {"pattern": pattern, "queue": queue}
        await _step(f"bind {pattern!r} to {queue!r}", call(p.TOPIC_BIND, body))
    for sub in list(registry.wanted_subscriptions):
        await _step(f"subscribe to {sub.pattern!r}", start_subscription(sub))
    for consumer in list(registry.wanted_consumers):
        await _step(f"consume {consumer.queue!r}", start_consumer(consumer))


async def _step(what: str, action: Awaitable[object]) -> None:
    """Run one replay step, logging a failure instead of raising it."""
    try:
        await action
    except Exception as err:  # noqa: BLE001 -- one failed step must not stop the rest
        logger.warning("replay: could not %s: %s", what, err)
    else:
        logger.debug("replay: %s", what)
