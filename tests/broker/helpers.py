"""Helpers shared by broker tests."""

from collections.abc import Callable, Coroutine
from typing import Any

from pynet.loop import run
from pynet.loop.testing import FakeClock


def run_async[T](fn: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run an async test body on a loop with a virtual, auto-jumping clock."""
    return run(fn(), clock=FakeClock(autojump=True))
