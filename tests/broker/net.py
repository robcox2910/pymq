"""Helpers for broker tests that talk over real localhost sockets."""

from collections.abc import Callable, Coroutine
from typing import Any

from pynet.loop import run, sleep, timeout

HOST = "127.0.0.1"
POLL = 0.005
WAIT = 2.0


def run_net(fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run a network test body on the real clock."""
    run(fn())


async def wait_until(check: Callable[[], bool], limit: float = WAIT) -> None:
    """Poll ``check`` until it is true, failing after ``limit`` seconds."""
    async with timeout(limit):
        while not check():
            await sleep(POLL)
