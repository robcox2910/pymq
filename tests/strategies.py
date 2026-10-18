"""Strategies -- recipes for random messages, shared by the property tests.

A strategy tells Hypothesis how to invent one kind of input. Each one here
is a named function, named for what it makes, with a one-line docstring.
``tests/test_properties.py`` and ``tests/test_model.py`` both use them,
and the ``by_priority`` sort key that goes with them.
"""

from typing import cast

from hypothesis import strategies as st

from pymq.queue import BaseMessageQueue, MessageQueue, PriorityMessageQueue


def queue_kinds() -> st.SearchStrategy[type[BaseMessageQueue]]:
    """Pick a kind of queue: first-come-first-served, or most urgent first."""
    return st.sampled_from([MessageQueue, PriorityMessageQueue])


def message_bodies() -> st.SearchStrategy[str]:
    """Make any message body at all: empty, spaces, emoji, quotes, the lot."""
    return st.text()


def priorities() -> st.SearchStrategy[int]:
    """Make any priority: any whole number, negative ones too (lower is more urgent)."""
    return st.integers()


def retry_limits() -> st.SearchStrategy[int]:
    """Make any retry limit a queue accepts: any whole number, 0 or more."""
    # BaseMessageQueue refuses a limit below 0 with a ValueError.
    return st.integers(min_value=0)


def by_priority(message: dict[str, object]) -> int:
    """Return a saved message's priority (as ``to_dict`` writes it), to sort by."""
    return cast("int", message["priority"])
