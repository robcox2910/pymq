"""Strategies -- recipes for random messages, shared by the property tests.

A strategy tells Hypothesis how to invent one kind of input. Each one here
is a named function, named for what it makes, with a one-line docstring.
``tests/test_properties.py`` and ``tests/test_model.py`` both use them,
and the ``by_priority`` sort key that goes with them, plus the time limit
both files use.
"""

from typing import cast

import pytest
from hypothesis import settings
from hypothesis import strategies as st

from pymq.queue import BaseMessageQueue, MessageQueue, PriorityMessageQueue

# --- Time limits ----------------------------------------------------------------
# pytest stops any test after 10 seconds (see addopts in pyproject.toml), so a
# hang fails fast. A normal run of 100 examples must finish well inside that.
# The thorough run (the profile in conftest.py) tries 2,000, so the property
# tests and the model check get 10 minutes instead. Each test module sets
# ``pytestmark = PROPERTY_TIMEOUT``.
THOROUGH_PROFILE = "thorough"
NORMAL_RUN_TIMEOUT_SECONDS = 10
THOROUGH_RUN_TIMEOUT_SECONDS = 600
_is_thorough_run = settings.get_current_profile_name() == THOROUGH_PROFILE
PROPERTY_TIMEOUT = pytest.mark.timeout(
    THOROUGH_RUN_TIMEOUT_SECONDS if _is_thorough_run else NORMAL_RUN_TIMEOUT_SECONDS
)


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
