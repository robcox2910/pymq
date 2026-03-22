"""Queues are like bakery counters -- messages line up and wait."""

from __future__ import annotations

import heapq
from collections import deque

from pymq.message import Message, create_message


class MessageQueue:
    """FIFO message queue with acknowledgment and dead-letter support.

    Messages are dequeued in the order they were enqueued. A consumer
    must acknowledge each message; rejected messages are requeued up to
    ``max_retries`` times before moving to the dead-letter queue.

    Args:
        name: Human-readable queue name.
        max_retries: Maximum requeue attempts before dead-lettering.

    """

    __slots__ = ("_dead_letters", "_in_flight", "_max_retries", "_messages", "_name")

    def __init__(self, name: str, max_retries: int = 3) -> None:
        """Create a new message queue."""
        self._name = name
        self._max_retries = max_retries
        self._messages: deque[Message] = deque()
        self._in_flight: dict[str, Message] = {}
        self._dead_letters: deque[Message] = deque()

    @property
    def name(self) -> str:
        """Return the queue name."""
        return self._name

    @property
    def size(self) -> int:
        """Return the number of messages waiting in the queue."""
        return len(self._messages)

    @property
    def dead_letter_count(self) -> int:
        """Return the number of messages in the dead-letter queue."""
        return len(self._dead_letters)

    def put(self, body: str, priority: int = 0) -> Message:
        """Create a message and add it to the queue.

        Args:
            body: The message content.
            priority: Priority level (ignored in FIFO queue, kept for API compatibility).

        Returns:
            The newly created message.

        """
        msg = create_message(body, priority=priority)
        self._messages.append(msg)
        return msg

    def get(self) -> Message | None:
        """Dequeue the next message and mark it as in-flight.

        Returns:
            The next message, or None if the queue is empty.

        """
        if not self._messages:
            return None
        msg = self._messages.popleft()
        self._in_flight[msg.id] = msg
        return msg

    def acknowledge(self, msg: Message) -> None:
        """Permanently remove an in-flight message.

        Args:
            msg: The message to acknowledge.

        """
        self._in_flight.pop(msg.id, None)

    def reject(self, msg: Message) -> None:
        """Reject an in-flight message, requeuing or dead-lettering it.

        Increment the retry counter. If retries exceed ``max_retries``,
        move the message to the dead-letter queue; otherwise requeue it.

        Args:
            msg: The message to reject.

        """
        self._in_flight.pop(msg.id, None)
        msg.retries += 1
        if msg.retries > self._max_retries:
            self._dead_letters.append(msg)
        else:
            self._messages.append(msg)

    def get_dead_letter(self) -> Message | None:
        """Dequeue the next message from the dead-letter queue.

        Returns:
            The next dead-lettered message, or None if empty.

        """
        if not self._dead_letters:
            return None
        return self._dead_letters.popleft()


class PriorityMessageQueue:
    """Priority-based message queue with acknowledgment and dead-letter support.

    Messages are dequeued by priority (lowest number first). Messages with
    equal priority are returned in FIFO order.

    Args:
        name: Human-readable queue name.
        max_retries: Maximum requeue attempts before dead-lettering.

    """

    __slots__ = ("_counter", "_dead_letters", "_in_flight", "_max_retries", "_messages", "_name")

    def __init__(self, name: str, max_retries: int = 3) -> None:
        """Create a new priority message queue."""
        self._name = name
        self._max_retries = max_retries
        self._messages: list[tuple[int, int, Message]] = []
        self._in_flight: dict[str, Message] = {}
        self._dead_letters: deque[Message] = deque()
        self._counter = 0

    @property
    def name(self) -> str:
        """Return the queue name."""
        return self._name

    @property
    def size(self) -> int:
        """Return the number of messages waiting in the queue."""
        return len(self._messages)

    @property
    def dead_letter_count(self) -> int:
        """Return the number of messages in the dead-letter queue."""
        return len(self._dead_letters)

    def put(self, body: str, priority: int = 0) -> Message:
        """Create a message and add it to the priority queue.

        Args:
            body: The message content.
            priority: Priority level (lower number = higher priority).

        Returns:
            The newly created message.

        """
        msg = create_message(body, priority=priority)
        heapq.heappush(self._messages, (priority, self._counter, msg))
        self._counter += 1
        return msg

    def get(self) -> Message | None:
        """Dequeue the highest-priority message and mark it as in-flight.

        Returns:
            The highest-priority message, or None if the queue is empty.

        """
        if not self._messages:
            return None
        _, _, msg = heapq.heappop(self._messages)
        self._in_flight[msg.id] = msg
        return msg

    def acknowledge(self, msg: Message) -> None:
        """Permanently remove an in-flight message.

        Args:
            msg: The message to acknowledge.

        """
        self._in_flight.pop(msg.id, None)

    def reject(self, msg: Message) -> None:
        """Reject an in-flight message, requeuing or dead-lettering it.

        Args:
            msg: The message to reject.

        """
        self._in_flight.pop(msg.id, None)
        msg.retries += 1
        if msg.retries > self._max_retries:
            self._dead_letters.append(msg)
        else:
            heapq.heappush(self._messages, (msg.priority, self._counter, msg))
            self._counter += 1

    def get_dead_letter(self) -> Message | None:
        """Dequeue the next message from the dead-letter queue.

        Returns:
            The next dead-lettered message, or None if empty.

        """
        if not self._dead_letters:
            return None
        return self._dead_letters.popleft()
