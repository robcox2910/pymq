"""Tests for the per-connection outbox."""

import logging
from collections.abc import Sequence

import pytest
from pynet.errors import ConnectionClosedError, FrameTooLargeError
from pynet.loop import Event, sleep

from pymq.broker import Envelope, JsonValue, LeasedMessage, TopicEvent
from pymq.broker.outbox import ConnectionOutbox
from pymq.broker.protocol import PUSH_DELIVER, PUSH_EVENT
from tests.broker.helpers import run_async

TWO = 2
THREE = 3
FIVE = 5
SEVEN = 7
SETTLE = 0.01
NOW = "2026-10-17T10:00:00+00:00"

type Push = tuple[str, JsonValue, tuple[bytes, ...]]


class FakePeer:
    """Record pushes; ``gate`` blocks them until set."""

    def __init__(self) -> None:
        """Start with an open gate and no pushes."""
        self.pushes: list[Push] = []
        self.gate = Event()
        self.gate.set()
        self.fail_with: list[BaseException] = []

    async def push(
        self, method: str, body: JsonValue = None, *, attachments: Sequence[bytes] = ()
    ) -> None:
        """Wait for the gate, then record (or raise the next queued failure)."""
        await self.gate.wait()
        if self.fail_with:
            raise self.fail_with.pop(0)
        self.pushes.append((method, body, tuple(attachments)))


def _leased(tag: int, attachments: tuple[bytes, ...] = ()) -> LeasedMessage:
    """Build a leased message with delivery tag ``tag``."""
    env = Envelope(f"m{tag}", "jobs", {"n": tag}, {}, attachments, 0, 1, NOW)
    return LeasedMessage("jobs", "ctag-1", tag, env)


def _event(name: str, attachments: tuple[bytes, ...] = ()) -> TopicEvent:
    """Build an event whose body is ``name``."""
    return TopicEvent("a.b", name, {"h": "v"}, attachments, NOW)


def _bodies(peer: FakePeer) -> list[JsonValue]:
    """Return a short label for each push: delivery tag or event body."""
    out: list[JsonValue] = []
    for method, body, _att in peer.pushes:
        assert isinstance(body, dict)
        if method == PUSH_DELIVER:
            out.append(body["delivery_tag"])
        else:
            event = body["event"]
            assert isinstance(event, dict)
            out.append(event["body"])
    return out


class TestOrdering:
    """Check what goes out first."""

    def test_delivery_jumps_ahead_of_events(self) -> None:
        """Push queued deliveries before queued events."""

        async def main() -> None:
            peer = FakePeer()
            box = ConnectionOutbox(peer)
            box.send_event("sub-1", _event("e1"))
            box.send_event("sub-1", _event("e2"))
            box.send_delivery(_leased(1))
            box.start()
            await sleep(SETTLE)
            assert _bodies(peer) == [1, "e1", "e2"]
            box.close()

        run_async(main)

    def test_events_round_robin_across_subscriptions(self) -> None:
        """Take one event from each subscription in turn."""

        async def main() -> None:
            peer = FakePeer()
            box = ConnectionOutbox(peer)
            for name in ("a1", "a2", "a3"):
                box.send_event("sub-a", _event(name))
            for name in ("b1", "b2"):
                box.send_event("sub-b", _event(name))
            box.start()
            await sleep(SETTLE)
            assert _bodies(peer) == ["a1", "b1", "a2", "b2", "a3"]
            box.close()

        run_async(main)

    def test_push_bodies_and_attachments(self) -> None:
        """Send the agreed body shapes with attachments alongside."""

        async def main() -> None:
            peer = FakePeer()
            box = ConnectionOutbox(peer)
            box.start()
            box.send_delivery(_leased(SEVEN, (b"zip",)))
            box.send_event("sub-1", _event("hello", (b"pic",)))
            await sleep(SETTLE)
            (m1, b1, a1), (m2, b2, a2) = peer.pushes
            assert m1 == PUSH_DELIVER
            assert isinstance(b1, dict)
            assert b1["consumer_tag"] == "ctag-1"
            assert b1["delivery_tag"] == SEVEN
            envelope = b1["envelope"]
            assert isinstance(envelope, dict)
            assert envelope["id"] == "m7"
            assert a1 == (b"zip",)
            assert m2 == PUSH_EVENT
            assert isinstance(b2, dict)
            assert b2["sub_id"] == "sub-1"
            event = b2["event"]
            assert isinstance(event, dict)
            assert event["topic"] == "a.b"
            assert event["headers"] == {"h": "v"}
            assert a2 == (b"pic",)
            box.close()

        run_async(main)


class TestDropping:
    """Check the drop-oldest rule for events and the no-drop rule for deliveries."""

    def test_deliveries_never_dropped(self) -> None:
        """Keep every delivery even past max_pending."""

        async def main() -> None:
            peer = FakePeer()
            box = ConnectionOutbox(peer, max_pending=TWO)
            for tag in range(1, FIVE + 1):
                box.send_delivery(_leased(tag))
            box.start()
            await sleep(SETTLE)
            assert _bodies(peer) == [1, 2, 3, 4, 5]
            box.close()

        run_async(main)

    def test_events_drop_oldest_and_count(self) -> None:
        """Keep the newest max_pending events and count the rest."""

        async def main() -> None:
            peer = FakePeer()
            box = ConnectionOutbox(peer, max_pending=TWO)
            for n in range(1, FIVE + 1):
                box.send_event("sub-1", _event(f"e{n}"))
            assert box.dropped("sub-1") == THREE
            assert box.total_dropped == THREE
            box.start()
            await sleep(SETTLE)
            assert _bodies(peer) == ["e4", "e5"]
            box.close()

        run_async(main)

    def test_subscription_limit_overrides_default(self) -> None:
        """Use the limit given to add_subscription for that subscription only."""

        async def main() -> None:
            peer = FakePeer()
            box = ConnectionOutbox(peer, max_pending=FIVE)
            box.add_subscription("tight", 1)
            for n in range(THREE):
                box.send_event("tight", _event(f"t{n}"))
                box.send_event("loose", _event(f"l{n}"))
            assert box.dropped("tight") == TWO
            assert box.dropped("loose") == 0
            assert box.dropped("unknown") == 0

        run_async(main)

    def test_forget_discards_pending_but_keeps_total(self) -> None:
        """Forget a subscription's queued events; the drop total stays."""

        async def main() -> None:
            peer = FakePeer()
            box = ConnectionOutbox(peer, max_pending=1)
            box.send_event("sub-1", _event("old"))
            box.send_event("sub-1", _event("new"))
            box.send_event("sub-2", _event("other"))
            box.forget("sub-1")
            assert box.dropped("sub-1") == 0
            assert box.total_dropped == 1
            box.start()
            await sleep(SETTLE)
            assert _bodies(peer) == ["other"]
            box.close()

        run_async(main)

    def test_sends_never_block_while_push_is_stuck(self) -> None:
        """Accept sends while a push is blocked; drop events behind it."""

        async def main() -> None:
            peer = FakePeer()
            peer.gate.clear()
            box = ConnectionOutbox(peer, max_pending=TWO)
            box.start()
            box.send_event("sub-1", _event("first"))
            await sleep(SETTLE)  # drain picks "first" and blocks in push
            for n in range(FIVE):
                box.send_event("sub-1", _event(f"e{n}"))
            assert box.dropped("sub-1") == THREE
            peer.gate.set()
            await sleep(SETTLE)
            assert _bodies(peer) == ["first", "e3", "e4"]
            box.close()

        run_async(main)


class TestFailures:
    """Check how push errors and closing are handled."""

    def test_closed_connection_stops_draining(self) -> None:
        """Stop pushing once the connection reports closed."""

        async def main() -> None:
            peer = FakePeer()
            peer.fail_with.append(ConnectionClosedError("gone"))
            box = ConnectionOutbox(peer)
            box.start()
            box.send_delivery(_leased(1))
            await sleep(SETTLE)
            box.send_delivery(_leased(2))
            await sleep(SETTLE)
            assert peer.pushes == []
            box.close()

        run_async(main)

    def test_other_push_errors_are_logged_and_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Log an unexpected push error and carry on with the next item."""

        async def main() -> None:
            peer = FakePeer()
            peer.fail_with.append(RuntimeError("boom"))
            box = ConnectionOutbox(peer)
            box.send_delivery(_leased(1))
            box.send_delivery(_leased(2))
            box.start()
            await sleep(SETTLE)
            assert _bodies(peer) == [2]
            box.close()

        with caplog.at_level(logging.ERROR, logger="pymq.broker.outbox"):
            run_async(main)
        assert "boom" in caplog.text

    def test_failed_delivery_push_is_given_back(self, caplog: pytest.LogCaptureFixture) -> None:
        """Hand a delivery whose push failed to the callback with the error."""
        failed: list[tuple[int, str]] = []

        async def main() -> None:
            peer = FakePeer()
            peer.fail_with.append(FrameTooLargeError(TWO, 1))
            box = ConnectionOutbox(
                peer, on_push_failed=lambda m, err: failed.append((m.delivery_tag, err))
            )
            box.send_delivery(_leased(1))
            box.send_delivery(_leased(TWO))
            box.start()
            await sleep(SETTLE)
            assert _bodies(peer) == [TWO]
            box.close()

        with caplog.at_level(logging.ERROR, logger="pymq.broker.outbox"):
            run_async(main)
        assert len(failed) == 1
        tag, error = failed[0]
        assert tag == 1
        assert error.startswith("push failed: ")

    def test_failing_give_back_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Log a give-back callback that raises and keep draining."""

        def give_back(_m: LeasedMessage, _err: str) -> None:
            text = "nack broke"
            raise RuntimeError(text)

        async def main() -> None:
            peer = FakePeer()
            peer.fail_with.append(RuntimeError("boom"))
            box = ConnectionOutbox(peer, on_push_failed=give_back)
            box.send_delivery(_leased(1))
            box.send_delivery(_leased(TWO))
            box.start()
            await sleep(SETTLE)
            assert _bodies(peer) == [TWO]
            box.close()

        with caplog.at_level(logging.ERROR, logger="pymq.broker.outbox"):
            run_async(main)
        assert "nack broke" in caplog.text

    def test_skipped_event_is_counted_as_dropped(self, caplog: pytest.LogCaptureFixture) -> None:
        """Count an event whose push failed in the drop counters."""

        async def main() -> None:
            peer = FakePeer()
            peer.fail_with.append(RuntimeError("boom"))
            box = ConnectionOutbox(peer)
            box.send_event("sub-1", _event("lost"))
            box.send_event("sub-1", _event("kept"))
            box.start()
            await sleep(SETTLE)
            assert _bodies(peer) == ["kept"]
            assert box.dropped("sub-1") == 1
            assert box.total_dropped == 1
            box.close()

        with caplog.at_level(logging.ERROR, logger="pymq.broker.outbox"):
            run_async(main)

    def test_close_stops_drain_and_ignores_later_sends(self) -> None:
        """Drop everything after close and push nothing more."""

        async def main() -> None:
            peer = FakePeer()
            box = ConnectionOutbox(peer)
            box.start()
            box.start()  # a second start is harmless
            box.close()
            box.send_delivery(_leased(1))
            box.send_event("sub-1", _event("late"))
            await sleep(SETTLE)
            assert peer.pushes == []
            box.close()  # closing twice is harmless

        run_async(main)
