"""Define exception hierarchy for PyMQ."""


class PyMQError(Exception):
    """Base exception for all PyMQ errors."""


class QueueEmptyError(PyMQError):
    """Signal that a queue has no available messages."""


class MessageExpiredError(PyMQError):
    """Signal that a message has exceeded its retry limit."""
