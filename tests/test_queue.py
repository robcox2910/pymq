"""Test the MessageQueue class for FIFO ordering, ack, reject, and DLQ."""

from pymq.queue import MessageQueue

MAX_RETRIES_DEFAULT = 3


class TestMessageQueueBasics:
    """Verify basic FIFO queue operations."""

    def test_put_and_get_returns_message(self) -> None:
        """Put a message and get it back."""
        q = MessageQueue("test")
        q.put("hello")
        msg = q.get()
        assert msg is not None
        assert msg.body == "hello"

    def test_fifo_order(self) -> None:
        """Messages should come out in the order they were added."""
        q = MessageQueue("test")
        q.put("first")
        q.put("second")
        q.put("third")
        assert q.get() is not None and q.get() is not None  # noqa: PT018
        # Re-do to check properly
        q2 = MessageQueue("test2")
        q2.put("first")
        q2.put("second")
        q2.put("third")
        m1 = q2.get()
        m2 = q2.get()
        m3 = q2.get()
        assert m1 is not None
        assert m2 is not None
        assert m3 is not None
        assert m1.body == "first"
        assert m2.body == "second"
        assert m3.body == "third"

    def test_get_empty_returns_none(self) -> None:
        """Getting from an empty queue should return None."""
        q = MessageQueue("test")
        assert q.get() is None

    def test_size_property(self) -> None:
        """The size property should reflect the number of waiting messages."""
        q = MessageQueue("test")
        assert q.size == 0
        q.put("a")
        q.put("b")
        assert q.size == 2  # noqa: PLR2004
        q.get()
        assert q.size == 1

    def test_name_property(self) -> None:
        """The name property should return the queue name."""
        q = MessageQueue("orders")
        assert q.name == "orders"


class TestMessageQueueAcknowledge:
    """Verify message acknowledgment."""

    def test_acknowledge_removes_in_flight(self) -> None:
        """Acknowledging a message should permanently remove it."""
        q = MessageQueue("test")
        q.put("hello")
        msg = q.get()
        assert msg is not None
        q.acknowledge(msg)
        assert q.size == 0

    def test_acknowledge_unknown_message(self) -> None:
        """Acknowledging an unknown message should not raise."""
        q = MessageQueue("test")
        q.put("hello")
        msg = q.get()
        assert msg is not None
        q.acknowledge(msg)
        q.acknowledge(msg)  # second ack should be safe


class TestMessageQueueReject:
    """Verify message rejection and dead-lettering."""

    def test_reject_requeues_message(self) -> None:
        """Rejecting a message should put it back on the queue."""
        q = MessageQueue("test")
        q.put("retry-me")
        msg = q.get()
        assert msg is not None
        q.reject(msg)
        assert q.size == 1
        requeued = q.get()
        assert requeued is not None
        assert requeued.body == "retry-me"

    def test_reject_increments_retries(self) -> None:
        """Each rejection should increment the retry counter."""
        q = MessageQueue("test")
        q.put("retry-me")
        msg = q.get()
        assert msg is not None
        assert msg.retries == 0
        q.reject(msg)
        assert msg.retries == 1

    def test_max_retries_moves_to_dlq(self) -> None:
        """Exceeding max retries should move the message to the dead-letter queue."""
        q = MessageQueue("test", max_retries=2)
        q.put("fragile")
        for _ in range(MAX_RETRIES_DEFAULT):
            msg = q.get()
            assert msg is not None
            q.reject(msg)
        assert q.size == 0
        assert q.dead_letter_count == 1

    def test_get_dead_letter(self) -> None:
        """Retrieve a message from the dead-letter queue."""
        q = MessageQueue("test", max_retries=1)
        q.put("doomed")
        msg = q.get()
        assert msg is not None
        q.reject(msg)
        msg = q.get()
        assert msg is not None
        q.reject(msg)
        dlq_msg = q.get_dead_letter()
        assert dlq_msg is not None
        assert dlq_msg.body == "doomed"

    def test_dead_letter_count_property(self) -> None:
        """The dead_letter_count property should reflect DLQ size."""
        q = MessageQueue("test", max_retries=0)
        q.put("fail")
        msg = q.get()
        assert msg is not None
        q.reject(msg)
        assert q.dead_letter_count == 1

    def test_get_dead_letter_empty(self) -> None:
        """Getting from an empty DLQ should return None."""
        q = MessageQueue("test")
        assert q.get_dead_letter() is None

    def test_reject_then_acknowledge(self) -> None:
        """Reject a message, get it again, and acknowledge it."""
        q = MessageQueue("test")
        q.put("eventually-ok")
        msg = q.get()
        assert msg is not None
        q.reject(msg)
        msg2 = q.get()
        assert msg2 is not None
        q.acknowledge(msg2)
        assert q.size == 0

    def test_multiple_messages_reject_one(self) -> None:
        """Rejecting one message should not affect others."""
        q = MessageQueue("test")
        q.put("a")
        q.put("b")
        msg_a = q.get()
        assert msg_a is not None
        q.reject(msg_a)
        msg_b = q.get()
        assert msg_b is not None
        assert msg_b.body == "b"
