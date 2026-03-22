"""Pub/Sub is like a newsletter -- one publisher, many subscribers."""

from __future__ import annotations

import contextlib
from collections import defaultdict
from collections.abc import Callable


class PubSub:
    """Publish-subscribe messaging system.

    Publishers send messages to named topics. Every subscriber registered
    for that topic receives a copy of the message. Subscribers can join
    and leave at any time.
    """

    __slots__ = ("_subscribers",)

    def __init__(self) -> None:
        """Create a new pub/sub system with no topics."""
        self._subscribers: dict[str, list[Callable[[str, str], None]]] = defaultdict(list)

    @property
    def topics(self) -> list[str]:
        """Return the list of active topics (those with at least one subscriber)."""
        return [topic for topic, handlers in self._subscribers.items() if handlers]

    def subscribe(self, topic: str, handler: Callable[[str, str], None]) -> None:
        """Register a handler for a topic.

        Args:
            topic: The topic name to subscribe to.
            handler: A callable receiving (topic, message) when a message is published.

        """
        if handler not in self._subscribers[topic]:
            self._subscribers[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Callable[[str, str], None]) -> None:
        """Remove a handler from a topic.

        Args:
            topic: The topic name to unsubscribe from.
            handler: The handler to remove.

        """
        with contextlib.suppress(ValueError):
            self._subscribers[topic].remove(handler)
        if not self._subscribers[topic]:
            del self._subscribers[topic]

    def publish(self, topic: str, message: str) -> None:
        """Deliver a message to all subscribers of a topic.

        Args:
            topic: The topic to publish to.
            message: The message body.

        """
        for handler in list(self._subscribers.get(topic, [])):
            handler(topic, message)
