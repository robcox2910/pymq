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


def _partition(q: WorkQueue, acked: set[str]) -> dict[str, set[str]]:
    """Return the ids in each place a message can be: acked, dead, waiting, leased."""
    records = q._records  # pyright: ignore[reportPrivateUsage]
    waiting = {i for i, r in records.items() if r.state in {State.READY, State.DELAYED}}
    leased = {i for i, r in records.items() if r.state is State.LEASED}
    dead = {d.envelope.id for d in q.dead_letters()}
    return {"acked": acked, "dead": dead, "waiting": waiting, "leased": leased}


class TestConservation:
    """Check nothing is lost or duplicated under random use."""

    def test_every_put_id_is_in_exactly_one_place(self) -> None:
        """Account for every put id exactly once after random operations.

        The mix covers puts, acks, nacks, lease expiry, TTL expiry,
        consumer removal and dead-letter replay.
        """

        async def main() -> None:
            rng = random.Random(SEED)  # noqa: S311
            config = QueueConfig(
                max_attempts=MAX_ATTEMPTS, ack_timeout=ACK_TIMEOUT, message_ttl=MESSAGE_TTL
            )
            q = WorkQueue("q", config)
            live: dict[int, LeasedMessage] = {}
            acked: set[str] = set()
            put: set[str] = set()
            removed = replayed = 0
            consumers = ["c0", "c1"]
            next_consumer = len(consumers)

            def sink(m: LeasedMessage) -> None:
                live[m.delivery_tag] = m

            for tag in consumers:
                q.add_consumer(tag, sink, prefetch=PREFETCH)
            for step in range(STEPS):
                action = rng.random()
                if action < P_PUT:
                    mid = f"m{step}"
                    q.put(step, id=mid, delay=rng.choice(DELAYS))
                    put.add(mid)
                elif action < P_SETTLE and live:
                    tag = rng.choice(sorted(live))
                    message = live.pop(tag)
                    choice = rng.random()
                    if choice < P_ACK:
                        if q.ack(tag):
                            acked.add(message.envelope.id)
                    elif choice < P_NACK:
                        q.nack(tag, requeue=rng.random() < P_REQUEUE)
                elif action < P_REMOVE:
                    gone = consumers.pop(rng.randrange(len(consumers)))
                    removed += q.remove_consumer(gone)
                    fresh = f"c{next_consumer}"
                    next_consumer += 1
                    q.add_consumer(fresh, sink, prefetch=PREFETCH)
                    consumers.append(fresh)
                elif action < P_REPLAY:
                    dead_ids = sorted(d.envelope.id for d in q.dead_letters())
                    if rng.random() < P_REPLAY_ALL:
                        replayed += q.replay_dead()
                    elif dead_ids:
                        replayed += q.replay_dead([rng.choice(dead_ids)])
                else:
                    await sleep(rng.choice(SLEEPS))
            places = _partition(q, acked)
            assert set().union(*places.values()) == put
            assert sum(len(ids) for ids in places.values()) == len(put)
            stats = q.stats()
            assert len(places["dead"]) == stats.dead
            assert len(places["waiting"]) == stats.ready + stats.delayed
            assert len(places["leased"]) == stats.in_flight
            reasons = {d.reason for d in q.dead_letters()}
            assert DeadReason.TTL_EXPIRED in reasons
            assert removed > 0
            assert replayed > 0

        run_async(main)
