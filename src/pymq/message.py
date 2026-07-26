"""Messages are the letters that travel through the queue system."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class Message:
    """A single message -- like a letter sealed in an envelope.

    Just as a letter carries its address, a postmark, and the note
    inside, a message carries its ``id`` (address), ``timestamp``
    (postmark), and ``body`` (the note). The queue delivers the whole
    envelope from sender to receiver.

    Args:
        id: Unique identifier for this message.
        body: The message content.
        timestamp: ISO-8601 creation timestamp.
        priority: Priority level (lower number = higher priority).
        retries: Number of times this message has been rejected.

    """

    id: str
    body: str
    timestamp: str
    priority: int = 0
    retries: int = 0


def create_message(body: str, priority: int = 0) -> Message:
    """Create a new message with an auto-generated UUID and ISO timestamp.

    Args:
        body: The message content.
        priority: Priority level (lower number = higher priority).

    Returns:
        A new Message instance.

    """
    return Message(
        id=str(uuid.uuid4()),
        body=body,
        timestamp=datetime.now(tz=UTC).isoformat(),
        priority=priority,
    )
