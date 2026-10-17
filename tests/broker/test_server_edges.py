"""Edge cases for the BrokerServer: failed pushes, bad limits, clean-up and restarts."""

import logging

import pytest
from pynet.errors import RemoteError
from pynet.framing import MAX_FRAME_SIZE
from pynet.messaging import Message, Peer, connect

from pymq.broker import Broker, JsonValue, QueueConfig
from pymq.broker import protocol as p
from pymq.broker.server import BrokerServer
from tests.broker.net import HOST, run_net, wait_until

LONG_ACK_TIMEOUT = 60.0
FRAME_HEADROOM = 200
BIG_BODY = "x" * (MAX_FRAME_SIZE - FRAME_HEADROOM)
NEGATIVE = -1
REQUEST_WAIT = 5.0


async def _connect(port: int, deliveries: list[Message] | None = None) -> Peer:
    """Connect a raw peer that records delivery pushes into ``deliveries``."""

    async def on_deliver(msg: Message) -> JsonValue:
        """Record a delivery push."""
        if deliveries is not None:
            deliveries.append(msg)
        return None

    def setup(peer: Peer) -> None:
        """Register the delivery push handler."""
        peer.on(p.PUSH_DELIVER, on_deliver)

    return await connect(HOST, port, setup=setup, heartbeat_interval=None)


async def _call(peer: Peer, method: str, body: JsonValue = None) -> JsonValue:
    """Send one request and return its reply body."""
    return (await peer.request(method, body, timeout=REQUEST_WAIT)).body


class _CancelFails(Broker):
    """A broker whose first consumer cancel raises."""

    def __init__(self) -> None:
        """Create the broker with no failures yet."""
        super().__init__()
        self.failed = 0

    def cancel(self, consumer_tag: str) -> int:
        """Raise on the first cancel, then behave normally."""
        if not self.failed:
            self.failed += 1
            msg = "cancel broke"
            raise RuntimeError(msg)
        return super().cancel(consumer_tag)


class TestFailedPushes:
    """Check a delivery whose push fails is given back at once."""

    def test_too_large_delivery_is_nacked_not_left_to_time_out(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Dead-letter an unpushable delivery straight away with a push-failed error."""

        async def main() -> None:
            broker = Broker()
            config = QueueConfig(max_attempts=1, ack_timeout=LONG_ACK_TIMEOUT)
            broker.declare_queue("jobs", config)
            async with BrokerServer(broker, heartbeat_interval=None) as server:
                deliveries: list[Message] = []
                peer = await _connect(server.port, deliveries)
                await _call(peer, p.MSG_PUT, {"queue": "jobs", "body": BIG_BODY})
                await _call(peer, p.CONSUME_START, {"queue": "jobs"})
                await wait_until(lambda: broker.stats("jobs").dead == 1)
                (letter,) = broker.dead_letters("jobs")
                assert letter.error is not None
                assert letter.error.startswith("push failed: ")
                assert deliveries == []
                await peer.close()

        with caplog.at_level(logging.ERROR, logger="pymq.broker.outbox"):
            run_net(main)


class TestDeadListLimit:
    """Check the dead.list limit is validated."""

    def test_negative_limit_is_invalid_request(self) -> None:
        """Refuse a negative limit on the wire and in the core."""

        async def main() -> None:
            broker = Broker()
            broker.declare_queue("jobs")
            with pytest.raises(ValueError, match="limit"):
                broker.dead_letters("jobs", NEGATIVE)
            async with BrokerServer(broker, heartbeat_interval=None) as server:
                peer = await _connect(server.port)
                with pytest.raises(RemoteError) as info:
                    await _call(peer, p.DEAD_LIST, {"queue": "jobs", "limit": NEGATIVE})
                assert info.value.code == "invalid_request"
                await peer.close()

        run_net(main)


class TestConnectionCleanUp:
    """Check a connection's clean-up survives a failing step."""

    def test_failing_cancel_still_cancels_the_rest(self, caplog: pytest.LogCaptureFixture) -> None:
        """Cancel every other consumer and forget the connection when one cancel raises."""

        async def main() -> None:
            broker = _CancelFails()
            broker.declare_queue("a")
            broker.declare_queue("b")
            async with BrokerServer(broker, heartbeat_interval=None) as server:
                peer = await _connect(server.port)
                await _call(peer, p.CONSUME_START, {"queue": "a"})
                await _call(peer, p.CONSUME_START, {"queue": "b"})
                await peer.close()
                await wait_until(lambda: server.connection_count == 0)
                assert broker.failed == 1
                assert broker.stats("b").consumers == 0

        with caplog.at_level(logging.ERROR, logger="pymq.broker._handlers"):
            run_net(main)
        assert "cancel broke" in caplog.text


class TestStartTwice:
    """Check a server cannot be started twice."""

    def test_second_start_raises(self) -> None:
        """Refuse a second start instead of leaking the first listener."""

        async def main() -> None:
            server = BrokerServer(heartbeat_interval=None)
            await server.start()
            port = server.port
            with pytest.raises(RuntimeError, match="already started"):
                await server.start()
            assert server.port == port
            await server.close()

        run_net(main)
