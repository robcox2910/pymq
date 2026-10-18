"""Queues are like bakery counters -- messages line up and wait."""

import heapq
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, replace
from typing import ClassVar, Self

from pymq.message import Message, create_message


class BaseMessageQueue(ABC):
    """Shared foundation for all message queues.

    Every message handed back (by ``put``, ``get`` and
    ``get_dead_letter``) is a copy, like a receipt: scribbling on it never
    changes the message the queue holds. ``from_messages`` copies the
    messages it is given, too. ``acknowledge`` and ``reject``
    find the queue's own message by its ``id``.

    Think of this as the blueprint for a bakery counter. Every counter
    needs a name sign, a way to hand out tickets (messages), and a
    spot for unclaimed orders (the dead-letter shelf). Subclasses
    decide *how* the line is ordered -- first-come-first-served or
    by priority.

    Args:
        name: Human-readable queue name.
        max_retries: Maximum requeue attempts before dead-lettering
            (a whole number, 0 or more).

    Raises:
        ValueError: If ``max_retries`` isn't a whole number of 0 or more
            (``1.5``, ``True`` and ``-1`` are all refused).
            A save file holding it could never be loaded back.

    """

    __slots__ = ("_dead_letters", "_in_flight", "_max_retries", "_name")

    kind: ClassVar[str]
    """How the line is ordered, written into save files (``fifo`` or ``priority``)."""

    def __init__(self, name: str, max_retries: int = 3) -> None:
        """Create a new message queue."""
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)  # pyright: ignore[reportUnnecessaryIsInstance]
            or max_retries < 0
        ):
            msg = f"max_retries must be a whole number, 0 or more, got {max_retries!r}"
            raise ValueError(msg)
        self._name = name
        self._max_retries = max_retries
        self._in_flight: dict[str, Message] = {}
        self._dead_letters: deque[Message] = deque()

    @property
    def name(self) -> str:
        """Return the queue name."""
        return self._name

    @property
    @abstractmethod
    def size(self) -> int:
        """Return the number of messages waiting in the queue."""

    @property
    def dead_letter_count(self) -> int:
        """Return the number of messages in the dead-letter queue."""
        return len(self._dead_letters)

    @abstractmethod
    def put(self, body: str, priority: int = 0) -> Message:
        """Create a message and add it to the queue.

        Args:
            body: The message content.
            priority: Priority level (lower number = higher priority).

        Returns:
            A copy of the newly created message.

        """

    @abstractmethod
    def get(self) -> Message | None:
        """Dequeue the next message and mark it as in-flight.

        Returns:
            A copy of the next message, or None if the queue is empty.

        """

    @abstractmethod
    def _requeue(self, msg: Message) -> None:
        """Put a rejected message back into the queue.

        Each subclass decides where the message goes based on its
        ordering strategy.

        Args:
            msg: The message to requeue.

        """

    def acknowledge(self, msg: Message) -> None:
        """Permanently remove an in-flight message.

        Args:
            msg: The message to acknowledge.

        """
        self._in_flight.pop(msg.id, None)

    def reject(self, msg: Message) -> None:
        """Reject an in-flight message, requeuing or dead-lettering it.

        Only messages that are currently in-flight can be rejected, and
        ``msg`` is matched by its id: the queue's own copy is the one that
        goes back, so a copy from before a restart can't reset its retries.
        Increment the retry counter. If retries exceed ``max_retries``,
        move the message to the dead-letter queue; otherwise requeue it.
        Rejecting a message that was already acknowledged (or never
        in-flight) does nothing, so a message is never resurrected.

        Args:
            msg: The message to reject.

        """
        own = self._in_flight.pop(msg.id, None)
        if own is None:
            return
        own.retries += 1
        if own.retries > self._max_retries:
            self._dead_letters.append(own)
        else:
            self._requeue(own)

    @abstractmethod
    def _waiting(self) -> list[Message]:
        """Return the waiting messages in the order they would be handed out."""

    def to_dict(self) -> dict[str, object]:
        """Describe the whole queue as plain data, ready to save as JSON.

        Messages that were taken but never acknowledged go first in the
        waiting line (in a priority queue, first among messages of the same
        priority): a real broker hands them out again after a restart,
        because nobody confirmed they were dealt with.

        Returns:
            The queue's name, kind, retry limit, waiting messages and
            dead letters.

        """
        waiting = [*self._in_flight.values(), *self._waiting()]
        return {
            "name": self._name,
            "kind": self.kind,
            "max_retries": self._max_retries,
            "waiting": [asdict(msg) for msg in waiting],
            "dead_letters": [asdict(msg) for msg in self._dead_letters],
        }

    @classmethod
    def from_messages(
        cls,
        name: str,
        max_retries: int,
        waiting: Iterable[Message],
        dead_letters: Iterable[Message],
    ) -> Self:
        """Rebuild a queue from saved messages, keeping their order.

        Args:
            name: Human-readable queue name.
            max_retries: Maximum requeue attempts before dead-lettering.
            waiting: The waiting messages, in delivery order.
            dead_letters: The dead-lettered messages, oldest first.

        Returns:
            A queue that hands the messages out in the same order as before.
            It holds copies, so changing the messages passed in afterwards
            never changes the queue.

        """
        queue = cls(name, max_retries)
        for msg in waiting:
            queue._requeue(replace(msg))
        queue._dead_letters.extend(replace(msg) for msg in dead_letters)
        return queue

    def get_dead_letter(self) -> Message | None:
        """Dequeue the next message from the dead-letter queue.

        Returns:
            A copy of the next dead-lettered message, or None if empty.

        """
        if not self._dead_letters:
            return None
        return replace(self._dead_letters.popleft())


class MessageQueue(BaseMessageQueue):
    """FIFO message queue -- first in, first out.

    Like a bakery line: whoever arrives first gets served first.
    A consumer must acknowledge each message; rejected messages are
    requeued up to ``max_retries`` times before moving to the
    dead-letter queue.

    Args:
        name: Human-readable queue name.
        max_retries: Maximum requeue attempts before dead-lettering.

    """

    __slots__ = ("_messages",)

    kind = "fifo"

    def __init__(self, name: str, max_retries: int = 3) -> None:
        """Create a new FIFO message queue."""
        super().__init__(name, max_retries)
        self._messages: deque[Message] = deque()

    @property
    def size(self) -> int:
        """Return the number of messages waiting in the queue."""
        return len(self._messages)

    def put(self, body: str, priority: int = 0) -> Message:
        """Create a message and add it to the back of the line.

        Args:
            body: The message content.
            priority: Ignored in FIFO queue, kept for API compatibility.

        Returns:
            A copy of the newly created message.

        """
        msg = create_message(body, priority=priority)
        self._messages.append(msg)
        return replace(msg)

    def get(self) -> Message | None:
        """Dequeue the next message and mark it as in-flight.

        Returns:
            A copy of the next message, or None if the queue is empty.

        """
        if not self._messages:
            return None
        msg = self._messages.popleft()
        self._in_flight[msg.id] = msg
        return replace(msg)

    def _waiting(self) -> list[Message]:
        """Return the waiting messages, front of the line first."""
        return list(self._messages)

    def _requeue(self, msg: Message) -> None:
        """Put a rejected message back at the end of the line.

        Args:
            msg: The message to requeue.

        """
        self._messages.append(msg)


class PriorityMessageQueue(BaseMessageQueue):
    """Priority-based message queue -- most urgent first.

    Like an emergency room: patients with higher urgency (lower
    priority number) get seen before those who arrived earlier.
    Messages with equal priority are returned in FIFO order.

    Args:
        name: Human-readable queue name.
        max_retries: Maximum requeue attempts before dead-lettering.

    """

    __slots__ = ("_counter", "_messages")

    kind = "priority"

    def __init__(self, name: str, max_retries: int = 3) -> None:
        """Create a new priority message queue."""
        super().__init__(name, max_retries)
        self._messages: list[tuple[int, int, Message]] = []
        self._counter = 0

    @property
    def size(self) -> int:
        """Return the number of messages waiting in the queue."""
        return len(self._messages)

    def put(self, body: str, priority: int = 0) -> Message:
        """Create a message and add it to the priority queue.

        Args:
            body: The message content.
            priority: Priority level (lower number = higher priority).

        Returns:
            A copy of the newly created message.

        """
        msg = create_message(body, priority=priority)
        heapq.heappush(self._messages, (priority, self._counter, msg))
        self._counter += 1
        return replace(msg)

    def get(self) -> Message | None:
        """Dequeue the highest-priority message and mark it as in-flight.

        Returns:
            A copy of the highest-priority message, or None if the queue is empty.

        """
        if not self._messages:
            return None
        _, _, msg = heapq.heappop(self._messages)
        self._in_flight[msg.id] = msg
        return replace(msg)

    def _waiting(self) -> list[Message]:
        """Return the waiting messages, most urgent first, ties in arrival order."""
        return [msg for _, _, msg in sorted(self._messages)]

    def _requeue(self, msg: Message) -> None:
        """Put a rejected message back into the heap at its original priority.

        Args:
            msg: The message to requeue.

        """
        heapq.heappush(self._messages, (msg.priority, self._counter, msg))
        self._counter += 1
