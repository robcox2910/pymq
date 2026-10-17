"""The per-connection outbox: a letterbox the postie empties one letter at a time.

Every connection gets its own outbox and its own postie (a drain task). The
broker drops deliveries and events into the box and walks away at once; it
never waits for a slow reader. Deliveries are never thrown away (prefetch
already limits how many there can be). Events are kept per subscription,
and when one subscription's pile is full the oldest letter is thrown out
and counted, so a reader who stopped reading costs only themselves.
"""

import logging
from collections import deque
from collections.abc import Callable, Sequence
from typing import Protocol

from pynet.errors import ConnectionClosedError
from pynet.loop import Event, Task, spawn

from pymq.broker.model import JsonValue, LeasedMessage
from pymq.broker.protocol import PUSH_DELIVER, PUSH_EVENT, envelope_to_json, event_to_json
from pymq.broker.router import TopicEvent

logger = logging.getLogger(__name__)

DEFAULT_MAX_PENDING = 1_000


class PushTarget(Protocol):
    """Anything that can send a one-way push, like a pynet ``Peer``."""

    async def push(
        self, method: str, body: JsonValue = None, *, attachments: Sequence[bytes] = ()
    ) -> None:
        """Send one push."""
        ...


type PushFailed = Callable[[LeasedMessage, str], None]
type _Item = tuple[str, JsonValue, tuple[bytes, ...], LeasedMessage | str]


class ConnectionOutbox:
    """Queue pushes for one connection and send them from a single drain task.

    Args:
        peer: Where pushes go (normally the connection's ``Peer``).
        max_pending: The default number of events kept per subscription.
        on_push_failed: Called with a delivery and an error text when its
            push fails for a reason other than the connection closing, so
            the delivery can be given back (nacked) instead of waiting out
            its lease.

    """

    def __init__(
        self,
        peer: PushTarget,
        *,
        max_pending: int = DEFAULT_MAX_PENDING,
        on_push_failed: PushFailed | None = None,
    ) -> None:
        """Create an empty, not yet started outbox."""
        if max_pending < 1:
            msg = f"max_pending must be >= 1, got {max_pending!r}"
            raise ValueError(msg)
        self._peer = peer
        self._max_pending = max_pending
        self._on_push_failed = on_push_failed
        self._deliveries: deque[LeasedMessage] = deque()
        self._events: dict[str, deque[TopicEvent]] = {}
        self._limits: dict[str, int] = {}
        self._drops: dict[str, int] = {}
        self._ring: deque[str] = deque()  # subscriptions with events waiting, in turn order
        self._total_dropped = 0
        self._wake = Event()
        self._task: Task[None] | None = None
        self._pushing = False
        self._closed = False

    @property
    def total_dropped(self) -> int:
        """Return every event dropped by this outbox, forgotten subscriptions included."""
        return self._total_dropped

    def add_subscription(self, sub_id: str, max_pending: int) -> None:
        """Set how many events ``sub_id`` may have waiting."""
        if max_pending < 1:
            msg = f"max_pending must be >= 1, got {max_pending!r}"
            raise ValueError(msg)
        self._limits[sub_id] = max_pending

    def send_delivery(self, leased: LeasedMessage) -> None:
        """Queue a delivery push; deliveries are never dropped."""
        if self._closed:
            return
        self._deliveries.append(leased)
        self._wake.set()

    def send_event(self, sub_id: str, event: TopicEvent) -> None:
        """Queue an event push, dropping that subscription's oldest event when full."""
        if self._closed:
            return
        pending = self._events.get(sub_id)
        if pending is None:
            pending = self._events[sub_id] = deque()
        if not pending:
            self._ring.append(sub_id)
        if len(pending) >= self._limits.get(sub_id, self._max_pending):
            pending.popleft()
            self._count_drop(sub_id)
        pending.append(event)
        self._wake.set()

    def _count_drop(self, sub_id: str) -> None:
        """Count one event lost for ``sub_id``."""
        self._drops[sub_id] = self._drops.get(sub_id, 0) + 1
        self._total_dropped += 1

    def dropped(self, sub_id: str) -> int:
        """Return how many events were dropped for ``sub_id`` (0 once forgotten)."""
        return self._drops.get(sub_id, 0)

    def forget(self, sub_id: str) -> None:
        """Discard a subscription's waiting events, limit and drop count."""
        self._events.pop(sub_id, None)
        self._limits.pop(sub_id, None)
        self._drops.pop(sub_id, None)

    def start(self) -> None:
        """Start the drain task (once)."""
        if self._task is None and not self._closed:
            self._task = spawn(self._drain(), name="pymq-outbox")

    def close(self) -> None:
        """Stop the drain task and throw away everything still waiting.

        A push already on its way is left to finish (or fail, when the
        connection is closing too); the drain stops straight after it.
        Cancelling it mid-push would leave the peer's failed send unread.
        """
        self._closed = True
        if self._task is not None:
            if not self._pushing:
                self._task.cancel()
            self._task = None
        self._deliveries.clear()
        self._events.clear()
        self._ring.clear()

    def _next(self) -> _Item | None:
        """Return the next push to send: deliveries first, then events in turn."""
        if self._deliveries:
            leased = self._deliveries.popleft()
            body: JsonValue = {
                "consumer_tag": leased.consumer_tag,
                "delivery_tag": leased.delivery_tag,
                "envelope": envelope_to_json(leased.envelope),
            }
            return PUSH_DELIVER, body, leased.envelope.attachments, leased
        while self._ring:
            sub_id = self._ring.popleft()
            pending = self._events.get(sub_id)
            if not pending:
                continue  # forgotten since it joined the ring
            event = pending.popleft()
            if pending:
                self._ring.append(sub_id)
            body = {"sub_id": sub_id, "event": event_to_json(event)}
            return PUSH_EVENT, body, event.attachments, sub_id
        return None

    async def _drain(self) -> None:
        """Push items until cancelled or the connection closes."""
        while not self._closed:
            item = self._next()
            if item is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            method, body, attachments, origin = item
            self._pushing = True
            try:
                await self._peer.push(method, body, attachments=attachments)
            except ConnectionClosedError:
                return  # the connection's close callback cleans up
            except Exception as exc:
                logger.exception("failed to push %s; skipping it", method)
                self._skipped(origin, exc)
            finally:
                self._pushing = False

    def _skipped(self, origin: LeasedMessage | str, exc: Exception) -> None:
        """Count a skipped event, or give a skipped delivery back at once."""
        if isinstance(origin, str):
            self._count_drop(origin)
            return
        if self._on_push_failed is None:
            return
        try:
            self._on_push_failed(origin, f"push failed: {exc}")
        except Exception:
            logger.exception("giving back delivery %d failed", origin.delivery_tag)
