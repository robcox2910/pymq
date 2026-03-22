"""Messages are the letters that travel through the queue system."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class Message:
    """Represent a single message in the queue.

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
