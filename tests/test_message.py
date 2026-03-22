"""Test the Message dataclass and create_message factory."""

from pymq.message import Message, create_message

DEFAULT_PRIORITY = 0
DEFAULT_RETRIES = 0
CUSTOM_PRIORITY = 5
UUID_LENGTH = 36


class TestMessageDataclass:
    """Verify the Message dataclass stores fields correctly."""

    def test_message_stores_all_fields(self) -> None:
        """Create a message and verify all fields are accessible."""
        msg = Message(id="abc", body="hello", timestamp="2024-01-01T00:00:00", priority=1)
        assert msg.id == "abc"
        assert msg.body == "hello"
        assert msg.timestamp == "2024-01-01T00:00:00"
        assert msg.priority == 1

    def test_message_defaults_priority_to_zero(self) -> None:
        """Priority should default to zero when not specified."""
        msg = Message(id="x", body="test", timestamp="ts")
        assert msg.priority == DEFAULT_PRIORITY

    def test_message_defaults_retries_to_zero(self) -> None:
        """Retries should default to zero when not specified."""
        msg = Message(id="x", body="test", timestamp="ts")
        assert msg.retries == DEFAULT_RETRIES


class TestCreateMessage:
    """Verify the create_message factory function."""

    def test_create_message_sets_id(self) -> None:
        """The factory should generate a UUID for the message id."""
        msg = create_message("hello")
        assert len(msg.id) == UUID_LENGTH
        assert "-" in msg.id

    def test_create_message_sets_timestamp(self) -> None:
        """The factory should set an ISO-8601 timestamp."""
        msg = create_message("hello")
        assert "T" in msg.timestamp
        assert len(msg.timestamp) > 0

    def test_create_message_stores_body(self) -> None:
        """The factory should store the provided body."""
        msg = create_message("my message")
        assert msg.body == "my message"

    def test_create_message_default_priority(self) -> None:
        """The factory should default priority to zero."""
        msg = create_message("hello")
        assert msg.priority == DEFAULT_PRIORITY

    def test_create_message_custom_priority(self) -> None:
        """The factory should accept a custom priority."""
        msg = create_message("urgent", priority=CUSTOM_PRIORITY)
        assert msg.priority == CUSTOM_PRIORITY

    def test_create_message_unique_ids(self) -> None:
        """Each call should generate a unique id."""
        msg1 = create_message("a")
        msg2 = create_message("b")
        assert msg1.id != msg2.id

    def test_create_message_retries_default(self) -> None:
        """New messages should have zero retries."""
        msg = create_message("hello")
        assert msg.retries == DEFAULT_RETRIES
