"""Test the MessageQueue class for FIFO ordering, ack, reject, and DLQ."""

from dataclasses import replace
from pathlib import Path

import pytest

from pymq.persistence import load_queues, save_queues
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

    def test_reject_after_acknowledge_is_noop(self) -> None:
        """Rejecting an already-acknowledged message must not resurrect it."""
        q = MessageQueue("test")
        q.put("done")
        msg = q.get()
        assert msg is not None
        q.acknowledge(msg)
        q.reject(msg)  # should do nothing -- message already handled
        assert q.size == 0
        assert q.dead_letter_count == 0
        assert msg.retries == 0

    def test_reject_never_in_flight_is_noop(self) -> None:
        """Rejecting a message that was never in-flight must not requeue it."""
        q = MessageQueue("test")
        msg = q.put("never-taken")
        q.reject(msg)  # never get()-ed, so nothing to reject
        assert q.size == 1  # only the original, no duplicate
        assert q.dead_letter_count == 0
        assert msg.retries == 0

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


class TestRetryLimit:
    """The retry limit is a whole number, 0 or more."""

    def test_negative_retry_limit_is_refused(self) -> None:
        """A limit below zero is refused up front: a save file holding it could never be loaded.

        Found by the save-and-load property, which shrank its failure to this:
        an empty queue called "" with ``max_retries=-1`` saved fine, then
        ``load_queues`` refused the file it had just written.
        """
        with pytest.raises(ValueError, match="max_retries"):
            MessageQueue("", max_retries=-1)

    def test_true_is_not_a_retry_limit(self) -> None:
        """``True`` is refused too: it would be saved as ``true``, which isn't a number in JSON."""
        with pytest.raises(ValueError, match="max_retries"):
            MessageQueue("q", max_retries=True)

    def test_zero_is_a_retry_limit(self) -> None:
        """A limit of 0 is fine: the first rejection dead-letters the message."""
        assert MessageQueue("q", max_retries=0).to_dict()["max_retries"] == 0


class TestRejectUsesTheQueuesOwnCopy:
    """Reject goes by the message's id; the queue keeps its own copy, not the caller's."""

    def test_reject_with_a_handle_from_before_a_restart(self, tmp_path: Path) -> None:
        """Rejecting with a copy from before a restart still counts against the queue's message.

        Found by the model check, which shrank its failure to this story. The
        queue used to put the *caller's* old copy back in line, with its old
        retry count, so the message dodged the dead-letter queue.
        """
        path = tmp_path / "queues.json"
        q = MessageQueue("orders", max_retries=1)
        old = q.put("")
        save_queues([q], path)
        q = load_queues(path)["orders"]
        msg = q.get()
        assert msg is not None
        q.reject(msg)
        assert q.get() is not None
        q.reject(old)
        assert q.size == 0
        assert q.dead_letter_count == 1

    def test_reject_never_takes_the_callers_body(self) -> None:
        """Rejecting with an edited copy puts the queue's own message back, unchanged."""
        q = MessageQueue("orders")
        q.put("real")
        msg = q.get()
        assert msg is not None
        q.reject(replace(msg, body="forged"))
        again = q.get()
        assert again is not None
        assert (again.body, again.retries) == ("real", 1)
