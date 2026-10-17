"""Pub/Sub is like a newsletter -- one publisher, many subscribers."""

import contextlib
from collections import defaultdict
from collections.abc import Callable

type Handler = Callable[[str, str], None]


class DeliveryError(Exception):
    """Some subscribers couldn't take delivery of a message.

    Everyone else still got their copy -- this just tells the publisher
    exactly which handlers failed, and why.

    Args:
        topic: The topic the message was published to.
        failures: Each failing handler paired with the error it raised.

    """

    def __init__(self, topic: str, failures: list[tuple[Handler, Exception]]) -> None:
        """Remember which handlers failed for this topic."""
        count = len(failures)
        noun = "subscriber" if count == 1 else "subscribers"
        super().__init__(f"{count} {noun} of {topic!r} failed to handle the message")
        self.topic = topic
        self.failures = failures


class PubSub:
    """Publish-subscribe messaging -- like a newsletter.

    One writer sends out an issue, and everyone who subscribed to that
    newsletter gets their own copy in the mail. Here, publishers send
    messages to named topics, and every subscriber to that topic
    receives a copy. Readers can subscribe or unsubscribe any time.
    """

    __slots__ = ("_subscribers",)

    def __init__(self) -> None:
        """Create a new pub/sub system with no topics."""
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)

    @property
    def topics(self) -> list[str]:
        """Return the list of active topics (those with at least one subscriber)."""
        return [topic for topic, handlers in self._subscribers.items() if handlers]

    def subscribe(self, topic: str, handler: Handler) -> None:
        """Register a handler for a topic.

        Args:
            topic: The topic name to subscribe to.
            handler: A callable receiving (topic, message) when a message is published.

        """
        if handler not in self._subscribers[topic]:
            self._subscribers[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
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

        Every subscriber gets their copy, even if one of them trips over it:
        a jammed letterbox at one house doesn't stop the postman visiting the
        rest of the street. Once everyone has been visited, any failures are
        reported together.

        Args:
            topic: The topic to publish to.
            message: The message body.

        Raises:
            DeliveryError: If one or more handlers raised; ``failures`` lists
                each failing handler and its error.

        """
        failures: list[tuple[Handler, Exception]] = []
        for handler in list(self._subscribers.get(topic, [])):
            try:
                handler(topic, message)
            except Exception as exc:  # noqa: BLE001 -- collected and re-raised below
                failures.append((handler, exc))
        if failures:
            raise DeliveryError(topic, failures)
