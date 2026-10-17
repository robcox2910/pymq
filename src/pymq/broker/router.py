"""The topic router: who gets a copy of an announcement.

Think of a notice board with sorting rules. Subscribers stand at the board and
shout "tell me about ``job.*``"; bindings are standing orders that drop a copy
into a queue's PO box so the news waits there until somebody collects it.
"""

import itertools
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pymq.broker.model import JsonValue
from pymq.broker.topics import topic_matches, validate_pattern, validate_topic


@dataclass(frozen=True)
class TopicEvent:
    """One announcement published on a topic."""

    topic: str
    body: JsonValue
    headers: Mapping[str, str]
    attachments: tuple[bytes, ...]
    published_at: str


type EventSink = Callable[[str, TopicEvent], None]


class TopicRouter:
    """Match published topics against subscriptions and queue bindings."""

    def __init__(self) -> None:
        """Create a router with no subscriptions and no bindings."""
        self._ids = itertools.count(1)
        self._subs: dict[str, tuple[str, EventSink]] = {}
        self._bindings: dict[tuple[str, str], None] = {}

    @property
    def subscription_count(self) -> int:
        """Return how many subscriptions exist."""
        return len(self._subs)

    def subscribe(self, pattern: str, sink: EventSink) -> str:
        """Register ``sink`` for ``pattern`` and return its id (``sub-N``).

        Raises:
            InvalidTopicError: If the pattern is not valid.

        """
        validate_pattern(pattern)
        sub_id = f"sub-{next(self._ids)}"
        self._subs[sub_id] = (pattern, sink)
        return sub_id

    def unsubscribe(self, sub_id: str) -> bool:
        """Remove a subscription. Return False if it was unknown."""
        return self._subs.pop(sub_id, None) is not None

    def bind(self, pattern: str, queue: str) -> None:
        """Bind ``queue`` to ``pattern``. Binding twice changes nothing.

        Raises:
            InvalidTopicError: If the pattern is not valid.

        """
        validate_pattern(pattern)
        self._bindings[(pattern, queue)] = None

    def unbind(self, pattern: str, queue: str) -> bool:
        """Remove one binding. Return False if it did not exist."""
        return self._bindings.pop((pattern, queue), "missing") is None

    def unbind_queue(self, queue: str) -> int:
        """Remove every binding of ``queue`` and return how many there were."""
        doomed = [key for key in self._bindings if key[1] == queue]
        for key in doomed:
            del self._bindings[key]
        return len(doomed)

    def route(self, topic: str) -> tuple[list[tuple[str, EventSink]], list[str]]:
        """Return matching ``(sub_id, sink)`` pairs and bound queues (each once).

        Raises:
            InvalidTopicError: If the topic is not valid.

        """
        validate_topic(topic)
        subs = [(sid, sink) for sid, (pat, sink) in self._subs.items() if topic_matches(pat, topic)]
        queues: dict[str, None] = {}
        for pattern, queue in self._bindings:
            if topic_matches(pattern, topic):
                queues[queue] = None
        return subs, list(queues)

    def bindings(self) -> list[tuple[str, str]]:
        """Return every ``(pattern, queue)`` binding, sorted."""
        return sorted(self._bindings)
