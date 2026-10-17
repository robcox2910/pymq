"""Errors raised by the broker.

Think of these as the different "sorry" notes a post office hands back:
each one has a short code so both sides of the wire can agree on what went wrong.
"""


class BrokerError(Exception):
    """Base class for every broker error."""

    code: str = "broker_error"


class QueueNotFoundError(BrokerError):
    """Raise when a queue does not exist."""

    code = "queue_not_found"


class QueueFullError(BrokerError):
    """Raise when a queue has reached its maximum length."""

    code = "queue_full"


class QueueConfigConflictError(BrokerError):
    """Raise when a queue is redeclared with different settings."""

    code = "config_conflict"


class InvalidNameError(BrokerError):
    """Raise when a queue name is not allowed."""

    code = "invalid_name"


class InvalidTopicError(BrokerError):
    """Raise when a topic or topic pattern is not allowed."""

    code = "invalid_topic"


class InvalidRequestError(BrokerError):
    """Raise when a request is malformed."""

    code = "invalid_request"


class BrokerUnavailableError(BrokerError):
    """Raise when the broker cannot be reached."""

    code = "broker_unavailable"
