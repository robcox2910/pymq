"""Test the PriorityMessageQueue for priority-based ordering."""

from pymq.queue import PriorityMessageQueue

HIGH_PRIORITY = 1
MEDIUM_PRIORITY = 5
LOW_PRIORITY = 10
DEFAULT_PRIORITY = 0


class TestPriorityQueueOrdering:
    """Verify priority-based message ordering."""

    def test_higher_priority_dequeued_first(self) -> None:
        """Lower priority number should be dequeued before higher."""
        q = PriorityMessageQueue("test")
        q.put("low", priority=LOW_PRIORITY)
        q.put("high", priority=HIGH_PRIORITY)
        q.put("medium", priority=MEDIUM_PRIORITY)
        msg = q.get()
        assert msg is not None
        assert msg.body == "high"

    def test_full_priority_ordering(self) -> None:
        """All messages should come out in priority order."""
        q = PriorityMessageQueue("test")
        q.put("low", priority=LOW_PRIORITY)
        q.put("high", priority=HIGH_PRIORITY)
        q.put("medium", priority=MEDIUM_PRIORITY)
        m1 = q.get()
        m2 = q.get()
        m3 = q.get()
        assert m1 is not None
        assert m2 is not None
        assert m3 is not None
        assert m1.body == "high"
        assert m2.body == "medium"
        assert m3.body == "low"

    def test_equal_priority_fifo(self) -> None:
        """Messages with equal priority should come out in FIFO order."""
        q = PriorityMessageQueue("test")
        q.put("first", priority=MEDIUM_PRIORITY)
        q.put("second", priority=MEDIUM_PRIORITY)
        q.put("third", priority=MEDIUM_PRIORITY)
        m1 = q.get()
        m2 = q.get()
        m3 = q.get()
        assert m1 is not None
        assert m2 is not None
        assert m3 is not None
        assert m1.body == "first"
        assert m2.body == "second"
        assert m3.body == "third"

    def test_default_priority_is_zero(self) -> None:
        """Messages without explicit priority should default to zero."""
        q = PriorityMessageQueue("test")
        msg = q.put("default")
        assert msg.priority == DEFAULT_PRIORITY


class TestPriorityQueueBasics:
    """Verify basic operations on the priority queue."""

    def test_get_empty_returns_none(self) -> None:
        """Getting from an empty priority queue should return None."""
        q = PriorityMessageQueue("test")
        assert q.get() is None

    def test_size_property(self) -> None:
        """The size property should reflect the number of waiting messages."""
        q = PriorityMessageQueue("test")
        assert q.size == 0
        q.put("a")
        q.put("b")
        assert q.size == 2  # noqa: PLR2004
        q.get()
        assert q.size == 1

    def test_name_property(self) -> None:
        """The name property should return the queue name."""
        q = PriorityMessageQueue("priority-orders")
        assert q.name == "priority-orders"

    def test_acknowledge_removes_message(self) -> None:
        """Acknowledging should permanently remove the message."""
        q = PriorityMessageQueue("test")
        q.put("ack-me")
        msg = q.get()
        assert msg is not None
        q.acknowledge(msg)
        assert q.size == 0

    def test_reject_requeues_with_priority(self) -> None:
        """Rejected messages should maintain their priority when requeued."""
        q = PriorityMessageQueue("test")
        q.put("reject-me", priority=HIGH_PRIORITY)
        msg = q.get()
        assert msg is not None
        q.reject(msg)
        assert q.size == 1
        requeued = q.get()
        assert requeued is not None
        assert requeued.priority == HIGH_PRIORITY

    def test_max_retries_moves_to_dlq(self) -> None:
        """Exceeding max retries should dead-letter the message."""
        q = PriorityMessageQueue("test", max_retries=1)
        q.put("fragile")
        msg = q.get()
        assert msg is not None
        q.reject(msg)
        msg = q.get()
        assert msg is not None
        q.reject(msg)
        assert q.size == 0
        assert q.dead_letter_count == 1

    def test_get_dead_letter(self) -> None:
        """Retrieve a message from the dead-letter queue."""
        q = PriorityMessageQueue("test", max_retries=0)
        q.put("doomed")
        msg = q.get()
        assert msg is not None
        q.reject(msg)
        dlq_msg = q.get_dead_letter()
        assert dlq_msg is not None
        assert dlq_msg.body == "doomed"

    def test_dead_letter_empty(self) -> None:
        """Getting from an empty DLQ should return None."""
        q = PriorityMessageQueue("test")
        assert q.get_dead_letter() is None
