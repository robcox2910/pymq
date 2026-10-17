"""Errors for the classic in-process queues and their save files."""


class PyMQError(Exception):
    """Base class for PyMQ's in-process queue errors."""


class PersistenceError(PyMQError):
    """Raise when saving queues to disk, or loading them back, fails.

    Like a post office whose filing cabinet has jammed: the letters can't be
    filed away, or the cabinet holds something that isn't letters at all.
    """
