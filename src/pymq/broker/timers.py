"""A min-heap of due times that wakes the loop only for the earliest one.

Like one kitchen timer set for whichever pie finishes first: when it rings you
take out every pie that is ready, then set it again for the next one.
"""

import heapq
import itertools
from collections.abc import Callable

from pynet.loop import EventLoop, TimerHandle


class TimerHeap[T]:
    """Hold items with due times and fire a callback for each when it is due.

    Only one loop timer is ever armed, for the earliest live entry. Entries are
    never removed from the middle; instead ``is_live`` is asked and dead ones
    are skipped when they reach the top (lazy deletion).
    """

    def __init__(
        self,
        loop: EventLoop,
        on_due: Callable[[T], None],
        is_live: Callable[[T], bool],
    ) -> None:
        """Create an empty heap that calls ``on_due(item)`` for live due items."""
        self._loop = loop
        self._on_due = on_due
        self._is_live = is_live
        self._heap: list[tuple[float, int, T]] = []
        self._seq = itertools.count()
        self._handle: TimerHandle | None = None
        self._closed = False

    def push(self, due: float, item: T) -> None:
        """Add ``item`` to fire at loop time ``due``."""
        heapq.heappush(self._heap, (due, next(self._seq), item))
        self._arm()

    def clear(self) -> None:
        """Forget every entry and cancel the timer."""
        self._heap.clear()
        self._disarm()

    def close(self) -> None:
        """Clear the heap and refuse to arm again."""
        self._closed = True
        self.clear()

    def _disarm(self) -> None:
        """Cancel the armed loop timer, if any."""
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _arm(self) -> None:
        """Point the single loop timer at the earliest live entry."""
        if self._closed:
            return
        while self._heap and not self._is_live(self._heap[0][2]):
            heapq.heappop(self._heap)
        if not self._heap:
            self._disarm()
            return
        due = self._heap[0][0]
        if self._handle is not None and self._handle.when == due:
            return
        self._disarm()
        self._handle = self._loop.call_at(due, self._fire)

    def _fire(self) -> None:
        """Run ``on_due`` for every live entry that is due, then re-arm."""
        self._handle = None
        now = self._loop.time()
        while self._heap and self._heap[0][0] <= now:
            _, _, item = heapq.heappop(self._heap)
            if self._is_live(item):
                self._on_due(item)
        self._arm()
