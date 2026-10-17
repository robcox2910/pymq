"""A work queue's storage: what is waiting, what is delayed and what has died.

Think of a post office sorting room. Parcels wait in a tray in priority order,
some are held back until a delivery date, repeats of the same parcel number are
turned away, and parcels left too long go into the returns box.
"""

import heapq
import itertools
import logging
import math
import uuid
from collections import deque
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum, auto

from pynet.loop import get_running_loop

from pymq.broker.errors import QueueFullError, QueueNotFoundError
from pymq.broker.model import (
    DeadLetter,
    DeadReason,
    Envelope,
    JsonValue,
    PutResult,
    QueueConfig,
    QueueStats,
    utc_now_iso,
)
from pymq.broker.timers import TimerHeap

logger = logging.getLogger(__name__)


class State(Enum):
    """Where a stored message currently is."""

    READY = auto()
    DELAYED = auto()
    LEASED = auto()


@dataclass(slots=True)
class Record:
    """One stored message plus the bookkeeping the heaps need.

    ``epoch`` changes every time the message (re)enters the queue or is
    dropped, so older heap entries pointing at it can be recognised as stale.
    """

    envelope: Envelope
    state: State
    epoch: int
    ready_at: float


type Ref = tuple[str, int]

_COMPACT_FLOOR = 64


class QueueStore:
    """Store messages for one queue: put, priority, delay, dedupe, TTL, stats.

    Must be created inside a running pynet loop, which supplies the clock and
    timers. Subclasses add consumers and leases through the ``_dispatch`` hook.
    """

    def __init__(
        self,
        name: str,
        config: QueueConfig,
        *,
        on_dead: Callable[[DeadLetter], None] | None = None,
    ) -> None:
        """Create an empty queue called ``name`` using ``config``."""
        self._name = name
        self._config = config
        self._on_dead = on_dead
        self._loop = get_running_loop()
        self._closed = False
        self._records: dict[str, Record] = {}
        self._ready: list[tuple[int, int, str, int]] = []
        self._order = itertools.count()
        self._epochs = itertools.count(1)
        self._counts = dict.fromkeys(State, 0)
        self._delayed = TimerHeap[Ref](self._loop, self._promote, self._is_delayed)
        self._ttl = TimerHeap[Ref](self._loop, self._expire, self._is_waiting)
        self._delivered = 0
        self._acked = 0
        self._retried = 0
        self._seen: dict[str, float] = {}
        self._seen_order: deque[tuple[float, str]] = deque()
        self._dead_letters: deque[DeadLetter] = deque()
        self._enqueued = 0
        self._duplicates = 0
        self._dead_lettered = 0
        self._dead_dropped = 0

    @property
    def name(self) -> str:
        """Return the queue name."""
        return self._name

    @property
    def config(self) -> QueueConfig:
        """Return the queue settings."""
        return self._config

    def put(
        self,
        body: JsonValue,
        *,
        id: str | None = None,  # noqa: A002
        priority: int = 0,
        headers: Mapping[str, str] | None = None,
        attachments: Sequence[bytes] = (),
        delay: float = 0.0,
    ) -> PutResult:
        """Add a message, or ignore it if its id is still queued or was seen recently.

        Raises:
            QueueNotFoundError: If the queue is closed.
            ValueError: If ``delay`` is negative or not finite.
            QueueFullError: If ready plus delayed messages reach ``max_length``.

        """
        self._check_open()
        if not math.isfinite(delay) or delay < 0:
            msg = f"delay must be finite and >= 0, got {delay!r}"
            raise ValueError(msg)
        message_id = id if id is not None else uuid.uuid4().hex
        now = self._loop.time()
        self._prune_seen(now)
        if message_id in self._seen or message_id in self._records:
            self._duplicates += 1
            return PutResult(message_id, duplicate=True)
        if self._waiting() >= self._config.max_length:
            msg = f"queue {self._name!r} is full ({self._config.max_length})"
            raise QueueFullError(msg)
        expiry = now + self._config.dedupe_window
        self._seen[message_id] = expiry
        self._seen_order.append((expiry, message_id))
        envelope = Envelope(
            id=message_id,
            queue=self._name,
            body=body,
            headers=dict(headers or {}),
            attachments=tuple(attachments),
            priority=priority,
            attempt=0,
            enqueued_at=utc_now_iso(),
        )
        self._enqueued += 1
        self._enter(envelope, delay=delay)
        return PutResult(message_id, duplicate=False)

    def ready_ids(self) -> list[str]:
        """Return the ids of ready messages in the order they will be delivered."""
        self._check_open()
        live = [e for e in self._ready if self._is_ready((e[2], e[3]))]
        return [e[2] for e in sorted(live)]

    def purge(self) -> int:
        """Drop every ready and delayed message and return how many were dropped."""
        self._check_open()
        keep = {k: r for k, r in self._records.items() if r.state is State.LEASED}
        dropped = len(self._records) - len(keep)
        self._records = keep
        self._ready.clear()
        self._delayed.clear()
        self._ttl.clear()
        self._counts[State.READY] = self._counts[State.DELAYED] = 0
        return dropped

    def stats(self) -> QueueStats:
        """Return a snapshot of depths and counters."""
        self._check_open()
        ready_times = [r.ready_at for r in self._records.values() if r.state is State.READY]
        age = self._loop.time() - min(ready_times) if ready_times else None
        return QueueStats(
            name=self._name,
            ready=self._counts[State.READY],
            delayed=self._counts[State.DELAYED],
            in_flight=self._counts[State.LEASED],
            dead=len(self._dead_letters),
            consumers=self.consumer_count,
            oldest_ready_age=age,
            enqueued=self._enqueued,
            delivered=self._delivered,
            acked=self._acked,
            retried=self._retried,
            dead_lettered=self._dead_lettered,
            dead_dropped=self._dead_dropped,
            duplicates=self._duplicates,
        )

    def dead_letters(self, limit: int | None = None) -> list[DeadLetter]:
        """Return dead letters, oldest first, at most ``limit`` of them."""
        self._check_open()
        letters = list(self._dead_letters)
        return letters if limit is None else letters[:limit]

    def replay_dead(self, ids: Collection[str] | None = None) -> int:
        """Move dead letters back to ready with a fresh attempt count.

        ``None`` replays all of them. Unknown ids are skipped, and ``max_length``
        is ignored. Return how many were replayed.
        """
        self._check_open()
        wanted = None if ids is None else set(ids)
        keep: deque[DeadLetter] = deque()
        replay: list[Envelope] = []
        chosen: set[str] = set()
        for letter in self._dead_letters:
            eid = letter.envelope.id
            if (wanted is None or eid in wanted) and eid not in self._records and eid not in chosen:
                chosen.add(eid)
                replay.append(replace(letter.envelope, attempt=0))
            else:
                keep.append(letter)
        self._dead_letters = keep
        for envelope in replay:
            self._enter(envelope, dispatch=False)
        self._dispatch()
        return len(replay)

    def purge_dead(self) -> int:
        """Forget every dead letter and return how many there were."""
        self._check_open()
        count = len(self._dead_letters)
        self._dead_letters.clear()
        return count

    def close(self) -> None:
        """Cancel every timer; the queue refuses all calls afterwards."""
        self._closed = True
        self._delayed.close()
        self._ttl.close()

    @property
    def consumer_count(self) -> int:
        """Return how many consumers are registered (none in the storage half)."""
        return 0

    def _dispatch(self) -> None:
        """Hand ready messages to consumers (a hook; subclasses override)."""

    def _check_open(self) -> None:
        """Raise QueueNotFoundError if the queue was closed."""
        if self._closed:
            msg = f"queue {self._name!r} is closed"
            raise QueueNotFoundError(msg)

    def _waiting(self) -> int:
        """Return the number of ready plus delayed messages."""
        return self._counts[State.READY] + self._counts[State.DELAYED]

    def _enter(self, envelope: Envelope, *, delay: float = 0.0, dispatch: bool = True) -> None:
        """Store ``envelope`` as ready (or delayed) and start its TTL clock."""
        now = self._loop.time()
        old = self._records.pop(envelope.id, None)
        if old is not None:
            self._counts[old.state] -= 1
        ref: Ref = (envelope.id, next(self._epochs))
        record = Record(envelope, State.DELAYED, ref[1], now)
        self._records[envelope.id] = record
        if delay > 0:
            self._counts[State.DELAYED] += 1
            self._delayed.push(now + delay, ref)
        else:
            self._make_ready(record)
        if self._config.message_ttl is not None:
            self._ttl.push(now + self._config.message_ttl, ref)
        if dispatch and record.state is State.READY:
            self._dispatch()

    def _make_ready(self, record: Record) -> None:
        """Mark ``record`` ready and add it to the ready heap."""
        record.state = State.READY
        record.ready_at = self._loop.time()
        self._counts[State.READY] += 1
        envelope = record.envelope
        heapq.heappush(
            self._ready, (envelope.priority, next(self._order), envelope.id, record.epoch)
        )

    def _dead(self, envelope: Envelope, reason: DeadReason, error: str | None = None) -> None:
        """Record a dead letter, trim the list to its cap and tell ``on_dead``."""
        letter = DeadLetter(envelope, reason, utc_now_iso(), error)
        self._dead_letters.append(letter)
        self._dead_lettered += 1
        while len(self._dead_letters) > self._config.max_dead_letters:
            self._dead_letters.popleft()
            self._dead_dropped += 1
        if self._on_dead is not None:
            try:
                self._on_dead(letter)
            except Exception:
                logger.exception("on_dead callback failed for queue %r", self._name)

    def _prune_seen(self, now: float) -> None:
        """Forget ids whose dedupe window has passed."""
        while self._seen_order and self._seen_order[0][0] <= now:
            expiry, message_id = self._seen_order.popleft()
            if self._seen.get(message_id) == expiry:
                del self._seen[message_id]

    def _record_for(self, ref: Ref) -> Record | None:
        """Return the record ``ref`` points at, or None if the ref is stale."""
        record = self._records.get(ref[0])
        return record if record is not None and record.epoch == ref[1] else None

    def _is_delayed(self, ref: Ref) -> bool:
        """Return whether ``ref`` is a live delayed message."""
        record = self._record_for(ref)
        return record is not None and record.state is State.DELAYED

    def _is_ready(self, ref: Ref) -> bool:
        """Return whether ``ref`` is a live ready message."""
        record = self._record_for(ref)
        return record is not None and record.state is State.READY

    def _is_waiting(self, ref: Ref) -> bool:
        """Return whether ``ref`` is a live ready or delayed message."""
        record = self._record_for(ref)
        return record is not None and record.state is not State.LEASED

    def _promote(self, ref: Ref) -> None:
        """Move a delayed message to ready when its time comes."""
        record = self._records[ref[0]]
        self._counts[State.DELAYED] -= 1
        self._make_ready(record)
        self._dispatch()

    def _expire(self, ref: Ref) -> None:
        """Dead-letter a waiting message whose TTL ran out."""
        record = self._records.pop(ref[0])
        self._counts[record.state] -= 1
        self._dead(record.envelope, DeadReason.TTL_EXPIRED)
        self._compact_ready()

    def _compact_ready(self) -> None:
        """Rebuild the ready heap without stale entries when they dominate it."""
        if len(self._ready) > _COMPACT_FLOOR and len(self._ready) > 2 * self._counts[State.READY]:
            self._ready = [e for e in self._ready if self._is_ready((e[2], e[3]))]
            heapq.heapify(self._ready)
