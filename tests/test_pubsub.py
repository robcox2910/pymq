"""Test the PubSub publish-subscribe messaging system."""

from pymq.pubsub import PubSub


class TestPubSubSubscribe:
    """Verify subscription and message delivery."""

    def test_subscribe_delivers_message(self) -> None:
        """A subscriber should receive published messages."""
        ps = PubSub()
        received: list[tuple[str, str]] = []
        ps.subscribe("news", lambda t, m: received.append((t, m)))
        ps.publish("news", "hello")
        assert len(received) == 1
        assert received[0] == ("news", "hello")

    def test_multiple_subscribers_all_receive(self) -> None:
        """All subscribers to a topic should receive the message."""
        ps = PubSub()
        results_a: list[str] = []
        results_b: list[str] = []
        ps.subscribe("alerts", lambda _t, m: results_a.append(m))
        ps.subscribe("alerts", lambda _t, m: results_b.append(m))
        ps.publish("alerts", "fire!")
        assert results_a == ["fire!"]
        assert results_b == ["fire!"]

    def test_wrong_topic_not_delivered(self) -> None:
        """A subscriber should not receive messages from other topics."""
        ps = PubSub()
        received: list[str] = []
        ps.subscribe("sports", lambda _t, m: received.append(m))
        ps.publish("weather", "sunny")
        assert received == []

    def test_multiple_messages_delivered_in_order(self) -> None:
        """Messages should be delivered in publication order."""
        ps = PubSub()
        received: list[str] = []
        ps.subscribe("log", lambda _t, m: received.append(m))
        ps.publish("log", "first")
        ps.publish("log", "second")
        ps.publish("log", "third")
        assert received == ["first", "second", "third"]

    def test_duplicate_subscribe_ignored(self) -> None:
        """Subscribing the same handler twice should only deliver once."""
        ps = PubSub()
        received: list[str] = []

        def handler(_topic: str, msg: str) -> None:
            received.append(msg)

        ps.subscribe("test", handler)
        ps.subscribe("test", handler)
        ps.publish("test", "once")
        assert received == ["once"]


class TestPubSubUnsubscribe:
    """Verify unsubscription behaviour."""

    def test_unsubscribe_stops_delivery(self) -> None:
        """After unsubscribing, the handler should not receive messages."""
        ps = PubSub()
        received: list[str] = []

        def handler(_topic: str, msg: str) -> None:
            received.append(msg)

        ps.subscribe("news", handler)
        ps.publish("news", "before")
        ps.unsubscribe("news", handler)
        ps.publish("news", "after")
        assert received == ["before"]

    def test_unsubscribe_unknown_handler_safe(self) -> None:
        """Unsubscribing a handler that was never subscribed should not raise."""
        ps = PubSub()
        ps.unsubscribe("nonexistent", lambda _t, _m: None)

    def test_unsubscribe_one_keeps_others(self) -> None:
        """Unsubscribing one handler should not affect other subscribers."""
        ps = PubSub()
        results_a: list[str] = []
        results_b: list[str] = []

        def handler_a(_t: str, m: str) -> None:
            results_a.append(m)

        def handler_b(_t: str, m: str) -> None:
            results_b.append(m)

        ps.subscribe("topic", handler_a)
        ps.subscribe("topic", handler_b)
        ps.unsubscribe("topic", handler_a)
        ps.publish("topic", "only-b")
        assert results_a == []
        assert results_b == ["only-b"]


class TestPubSubTopics:
    """Verify the topics property."""

    def test_topics_empty_initially(self) -> None:
        """A new PubSub should have no topics."""
        ps = PubSub()
        assert ps.topics == []

    def test_topics_lists_active_topics(self) -> None:
        """Topics with subscribers should appear in the topics list."""
        ps = PubSub()
        ps.subscribe("weather", lambda _t, _m: None)
        ps.subscribe("sports", lambda _t, _m: None)
        assert sorted(ps.topics) == ["sports", "weather"]

    def test_topics_excludes_unsubscribed(self) -> None:
        """Topics with no remaining subscribers should not appear."""
        ps = PubSub()

        def handler(_t: str, _m: str) -> None:
            pass

        ps.subscribe("temp", handler)
        ps.unsubscribe("temp", handler)
        assert "temp" not in ps.topics

    def test_publish_to_topic_with_no_subscribers(self) -> None:
        """Publishing to a topic with no subscribers should not raise."""
        ps = PubSub()
        ps.publish("empty-topic", "nobody home")
