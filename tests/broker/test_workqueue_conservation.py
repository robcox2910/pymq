"""Conservation test for WorkQueue: no message is ever lost or duplicated."""

import random

from pynet.loop import sleep

from pymq.broker import DeadReason, LeasedMessage, QueueConfig
from pymq.broker.queuestore import State
from pymq.broker.workqueue import WorkQueue
from tests.broker.helpers import run_async

STEPS = 400
SEED = 1234
PREFETCH = 3
TWO_CONSUMERS = 2
ACK_TIMEOUT = 5.0
MESSAGE_TTL = 6.0
MAX_ATTEMPTS = 3
P_PUT = 0.35
P_SETTLE = 0.7
P_ACK = 0.5
P_NACK = 0.8
P_REQUEUE = 0.7
P_REMOVE = 0.75
P_REPLAY = 0.8
P_REPLAY_ALL = 0.5
SLEEPS = (0.5, 3.0, 8.0)
DELAYS = (0.0, 0.0, 2.0)


class _Mix:
    """Drive a queue with random operations and remember what happened."""

    def __init__(self, rng: random.Random) -> None:
        """Create the queue with two consumers."""
        config = QueueConfig(
            max_attempts=MAX_ATTEMPTS, ack_timeout=ACK_TIMEOUT, message_ttl=MESSAGE_TTL
        )
        self.rng = rng
        self.q = WorkQueue("q", config)
        self.live: dict[int, LeasedMessage] = {}
        self.acked: set[str] = set()
        self.put_ids: set[str] = set()
        self.removed = 0
        self.replayed = 0
        self.consumers: list[str] = []
        self._next_consumer = 0
        for _ in range(TWO_CONSUMERS):
            self._add_consumer()

    def _add_consumer(self) -> None:
        """Register a fresh consumer."""
        tag = f"c{self._next_consumer}"
        self._next_consumer += 1
        self.q.add_consumer(tag, self._sink, prefetch=PREFETCH)
        self.consumers.append(tag)

    def _sink(self, m: LeasedMessage) -> None:
        """Remember a delivery so it can be settled later."""
        self.live[m.delivery_tag] = m

    def put(self, step: int) -> None:
        """Put a new message, sometimes delayed."""
        mid = f"m{step}"
        self.q.put(step, id=mid, delay=self.rng.choice(DELAYS))
        self.put_ids.add(mid)

    def settle(self) -> None:
        """Ack, nack or forget (let expire) one live delivery."""
        tag = self.rng.choice(sorted(self.live))
        message = self.live.pop(tag)
        choice = self.rng.random()
        if choice < P_ACK:
            if self.q.ack(tag):
                self.acked.add(message.envelope.id)
        elif choice < P_NACK:
            self.q.nack(tag, requeue=self.rng.random() < P_REQUEUE)

    def swap_consumer(self) -> None:
        """Remove a random consumer (its leases come back) and add a new one."""
        gone = self.consumers.pop(self.rng.randrange(len(self.consumers)))
        self.removed += self.q.remove_consumer(gone)
        self._add_consumer()

    def replay(self) -> None:
        """Replay all dead letters, or one chosen at random."""
        dead_ids = sorted(d.envelope.id for d in self.q.dead_letters())
        if self.rng.random() < P_REPLAY_ALL:
            self.replayed += self.q.replay_dead()
        elif dead_ids:
            self.replayed += self.q.replay_dead([self.rng.choice(dead_ids)])

    def places(self) -> dict[str, set[str]]:
        """Return the ids in each place a message can be: acked, dead, waiting, leased."""
        records = self.q._records  # pyright: ignore[reportPrivateUsage]
        waiting = {i for i, r in records.items() if r.state in {State.READY, State.DELAYED}}
        leased = {i for i, r in records.items() if r.state is State.LEASED}
        dead = {d.envelope.id for d in self.q.dead_letters()}
        return {"acked": self.acked, "dead": dead, "waiting": waiting, "leased": leased}


class TestConservation:
    """Check nothing is lost or duplicated under random use."""

    def test_every_put_id_is_in_exactly_one_place(self) -> None:
        """Account for every put id exactly once after random operations.

        The mix covers puts, acks, nacks, lease expiry, TTL expiry,
        consumer removal and dead-letter replay.
        """

        async def main() -> None:
            mix = _Mix(random.Random(SEED))  # noqa: S311
            for step in range(STEPS):
                action = mix.rng.random()
                if action < P_PUT:
                    mix.put(step)
                elif action < P_SETTLE and mix.live:
                    mix.settle()
                elif action < P_REMOVE:
                    mix.swap_consumer()
                elif action < P_REPLAY:
                    mix.replay()
                else:
                    await sleep(mix.rng.choice(SLEEPS))
            places = mix.places()
            assert set[str]().union(*places.values()) == mix.put_ids
            assert sum(len(ids) for ids in places.values()) == len(mix.put_ids)
            stats = mix.q.stats()
            assert len(places["dead"]) == stats.dead
            assert len(places["waiting"]) == stats.ready + stats.delayed
            assert len(places["leased"]) == stats.in_flight
            assert DeadReason.TTL_EXPIRED in {d.reason for d in mix.q.dead_letters()}
            assert mix.removed > 0
            assert mix.replayed > 0

        run_async(main)
