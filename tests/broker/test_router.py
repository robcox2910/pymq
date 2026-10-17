"""Tests for the topic router."""

import pytest

from pymq.broker import InvalidTopicError, TopicEvent, TopicRouter

TWO = 2


def _sink(_sub_id: str, _event: TopicEvent) -> None:
    """Ignore the event."""


class TestSubscriptions:
    """Check subscribe, unsubscribe and routing to sinks."""

    def test_ids_and_matching(self) -> None:
        """Hand out sequential ids and route by pattern."""
        router = TopicRouter()
        assert router.subscribe("job.*", _sink) == "sub-1"
        assert router.subscribe("other", _sink) == "sub-2"
        subs, queues = router.route("job.done")
        assert [s for s, _ in subs] == ["sub-1"]
        assert queues == []
        assert router.subscription_count == TWO

    def test_unsubscribe(self) -> None:
        """Remove a subscription once."""
        router = TopicRouter()
        sid = router.subscribe("#", _sink)
        assert router.unsubscribe(sid)
        assert not router.unsubscribe(sid)
        assert router.route("a")[0] == []

    def test_invalid_inputs(self) -> None:
        """Reject bad patterns and topics."""
        router = TopicRouter()
        with pytest.raises(InvalidTopicError):
            router.subscribe("a..b", _sink)
        with pytest.raises(InvalidTopicError):
            router.route("#.x")
        with pytest.raises(InvalidTopicError):
            router.bind("", "q")


class TestBindings:
    """Check queue bindings."""

    def test_queue_listed_once(self) -> None:
        """Report a queue once even when two bindings match."""
        router = TopicRouter()
        router.bind("job.*", "q")
        router.bind("#", "q")
        router.bind("#", "q")
        assert router.route("job.x")[1] == ["q"]
        assert router.bindings() == [("#", "q"), ("job.*", "q")]

    def test_unbind(self) -> None:
        """Remove one binding or all of a queue's."""
        router = TopicRouter()
        router.bind("a", "q")
        router.bind("b", "q")
        router.bind("a", "r")
        assert router.unbind("a", "q")
        assert not router.unbind("a", "q")
        assert router.unbind_queue("q") == 1
        assert router.bindings() == [("a", "r")]
