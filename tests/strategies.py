"""Strategies -- recipes for random messages, shared by the property tests.

A strategy tells Hypothesis how to invent one kind of input. Each one here
is a named function, named for what it makes, with a one-line docstring.
``tests/test_properties.py`` and ``tests/test_model.py`` both use them.
"""

from hypothesis import strategies as st


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
