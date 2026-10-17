"""The client's phone line: is it up, is everything set up again, and how to call.

A ``ReconnectingClient`` redials by itself when the line drops. ``Session``
sits on top of it and adds three things:

- **Ready means set up.** After every (re)connect it runs the client's
  replay (declare, bind, subscribe, consume) and only then counts the line
  as *ready*. Ordinary requests wait for ready, so they never race the
  replay.
- **Waiting, not failing.** A request made while the line is down waits
  for it to come back, up to its timeout, and then raises
  ``BrokerUnavailableError``. A line that drops mid-request raises the same.
- **Resending puts.** ``put`` is safe to repeat (the broker drops a second
  copy with the same id), so ``put`` keeps resending until it gets an
  answer or its own timeout runs out.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence

from pynet.errors import NetConnectionError, NotConnectedError, RemoteError
from pynet.loop import Event, Task, get_running_loop, sleep, spawn, wait_for
from pynet.messaging import ConnectionEvent, ReconnectingClient

from pymq.broker import protocol as p
from pymq.broker.errors import BrokerUnavailableError, InvalidRequestError
from pymq.broker.model import JsonValue
from pymq.broker.protocol import Json

logger = logging.getLogger(__name__)

RESEND_PAUSE = 0.01  # seconds between a failed put attempt and the next try
CLOSED_MESSAGE = "the broker client is closed"


class Session:
    """Track whether the line is up and set up, and send requests over it.

    Args:
        rc: The reconnecting client that owns the line.
        replay: Sets everything up again on a new connection.
        on_drop: Called when the line drops (forget per-connection names).

    """

    def __init__(
        self,
        rc: ReconnectingClient,
        *,
        replay: Callable[[], Awaitable[None]],
        on_drop: Callable[[], None],
    ) -> None:
        """Watch ``rc``'s connection events."""
        self._rc = rc
        self._replay = replay
        self._on_drop = on_drop
        self._ready = False
        self._changed = Event()  # set while ready, and once closed
        self._closed = False
        self._replay_task: Task[None] | None = None
        rc.on_event(self._on_event)

    @property
    def connected(self) -> bool:
        """Return True while there is a live connection (set up or not)."""
        return self._rc.connected

    @property
    def ready(self) -> bool:
        """Return True while connected and set up again."""
        return self._ready and self._rc.connected

    def close(self) -> None:
        """Stop replaying and wake every waiter with ``BrokerUnavailableError``."""
        self._closed = True
        self._ready = False
        self._cancel_replay()
        self._changed.set()

    async def wait_ready(self, timeout: float | None) -> None:
        """Wait until ready.

        Raises:
            TimeoutError: If not ready within ``timeout`` seconds.
            BrokerUnavailableError: If the client is (or gets) closed.

        """
        await wait_for(self._until_ready(), timeout)

    async def wait_ready_or_unavailable(self, timeout: float) -> None:
        """Wait until ready, raising ``BrokerUnavailableError`` after ``timeout``."""
        try:
            await self.wait_ready(timeout)
        except TimeoutError as err:
            msg = f"no connection to the broker within {timeout}s"
            raise BrokerUnavailableError(msg) from err

    async def request(
        self,
        method: str,
        body: JsonValue,
        *,
        attachments: Sequence[bytes] = (),
        timeout: float,
    ) -> Json:
        """Wait for the line (within ``timeout``), then send a request.

        Raises:
            BrokerUnavailableError: If the line is not back in time, or drops
                before the answer comes.
            TimeoutError: If the broker is connected but too slow to answer.

        """
        loop = get_running_loop()
        deadline = loop.time() + timeout
        await self.wait_ready_or_unavailable(timeout)
        return await self.request_now(
            method, body, attachments=attachments, timeout=max(deadline - loop.time(), 0.0)
        )

    async def request_now(
        self,
        method: str,
        body: JsonValue,
        *,
        attachments: Sequence[bytes] = (),
        timeout: float,
    ) -> Json:
        """Send a request over the line as it is right now (no waiting).

        Raises:
            BrokerUnavailableError: If there is no line, or it drops mid-request.
            TimeoutError: If no answer comes within ``timeout``.

        """
        try:
            reply = await self._rc.request(method, body, attachments=attachments, timeout=timeout)
        except (NotConnectedError, NetConnectionError) as err:
            msg = f"{method}: the connection to the broker is down ({err})"
            raise BrokerUnavailableError(msg) from err
        except RemoteError as err:
            converted = p.remote_to_error(err)
            if converted is err:
                raise
            raise converted from err
        return _obj(reply.body)

    async def resend_until_answered(
        self,
        method: str,
        body: JsonValue,
        *,
        attachments: Sequence[bytes] = (),
        timeout: float,
    ) -> Json:
        """Send a request that is safe to repeat, resending after each drop.

        Raises:
            BrokerUnavailableError: If no answer came within ``timeout`` in total.

        """
        loop = get_running_loop()
        deadline = loop.time() + timeout
        attempt = 0
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0 or self._closed:
                break
            attempt += 1
            try:
                await self.wait_ready(remaining)
                return await self.request_now(
                    method,
                    body,
                    attachments=attachments,
                    timeout=max(deadline - loop.time(), 0.0),
                )
            except TimeoutError:
                break
            except BrokerUnavailableError as err:
                logger.debug("%s attempt %d failed (%s); will resend", method, attempt, err)
                await sleep(max(min(RESEND_PAUSE, deadline - loop.time()), 0.0))
        msg = f"{method}: no answer from the broker within {timeout}s ({attempt} attempts)"
        raise BrokerUnavailableError(msg)

    # -- connection events --

    def _on_event(self, event: ConnectionEvent) -> None:
        """Replay on every (re)connect; forget per-connection state on a drop."""
        if self._closed:
            return
        self._cancel_replay()
        if event is ConnectionEvent.DISCONNECTED:
            self._ready = False
            self._changed.clear()
            self._on_drop()
        else:
            self._replay_task = spawn(self._run_replay(), name="pymq-replay")

    async def _run_replay(self) -> None:
        """Run the replay, then mark the line ready."""
        try:
            await self._replay()
        except Exception:
            logger.exception("replay after reconnect failed")
        self._ready = True
        self._changed.set()

    def _cancel_replay(self) -> None:
        """Cancel a replay still running for an earlier connection."""
        task, self._replay_task = self._replay_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _until_ready(self) -> None:
        """Return once ready; raise ``BrokerUnavailableError`` if closed."""
        while not self.ready:
            if self._closed:
                raise BrokerUnavailableError(CLOSED_MESSAGE)
            if self._changed.is_set():  # ready flag set but the line just went
                await sleep(RESEND_PAUSE)
                continue
            await self._changed.wait()


def _obj(body: JsonValue) -> Json:
    """Return ``body`` if it is a JSON object, else raise InvalidRequestError."""
    if not isinstance(body, dict):
        msg = "reply body must be an object"
        raise InvalidRequestError(msg)
    return body
