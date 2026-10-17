# The Broker (The Sorting Room)

## The Post Office Sorting Room

Behind the counter of a post office is a **sorting room**. Parcels go into
named trays. Clerks borrow parcels from a tray and hand them back when done.
A notice board carries announcements, and standing orders copy some of those
announcements into trays.

The `Broker` is that sorting room, in three layers:

1. **Queues** (`WorkQueue`): named trays with leases, acks and retries.
2. **Topics** (`TopicRouter`): the notice board, with wildcard patterns.
3. **Bindings**: standing orders that copy announcements into queues.

The broker also announces on `$sys.dead.<queue>` whenever a message dies, so
you can watch for trouble. A queue bound to a catch-all pattern like `#`
receives those announcements too. If such a copy dies in its turn, it is
*not* announced again: otherwise every death would breed another
announcement, forever.

**The broker forgets everything when it restarts.** The scheduler keeps the
real records; the broker is just a fast sorting room.

## Using the Broker Directly

```python
from pynet.loop import run
from pynet.loop.testing import FakeClock

from pymq.broker import Broker, LeasedMessage


async def main() -> None:
    broker = Broker()
    broker.declare_queue("jobs")
    broker.subscribe("$sys.dead.*", lambda sub, e: print(sub, e.topic, e.body["reason"]))
    got: list[LeasedMessage] = []
    tag = broker.consume("jobs", got.append)
    print("consumer:", tag)
    broker.put("jobs", "resize photo", id="job-1")
    print("nack accepted:", broker.nack("jobs", got[0].delivery_tag, requeue=False, error="bad file"))
    print("dead letters:", len(broker.dead_letters("jobs")))
    print("stats:", broker.broker_stats())


run(main(), clock=FakeClock(autojump=True))
```

Output:

```text
consumer: ctag-1
sub-1 $sys.dead.jobs rejected
nack accepted: True
dead letters: 1
stats: BrokerStats(queues=1, subscriptions=1, bindings=0, binding_drops=0)
```

(The event line prints first because the announcement happens during the nack.)

- `declare_queue` is safe to repeat with the same settings; different
  settings raise `QueueConfigConflictError`.
- Consumer tags are `ctag-1`, `ctag-2`, ... and unique across all queues, so
  `cancel(tag)` needs no queue name.
- Any unknown queue raises `QueueNotFoundError`.
- A subscriber that raises is logged, and the others still get the event.

## What We Test

- Redeclaring a queue and conflicting settings.
- Every queue method raises for an unknown queue.
- Publishing reaches two subscribers and a bound queue.
- A raising subscriber does not stop the rest.
- A rejected nack publishes `$sys.dead.jobs` with the reason.
- A queue bound to `#` gets exactly one copy of its own death event, and
  when that copy dies too (nack, TTL or a raising consumer) nothing new is
  announced; two catch-all queues do not ping-pong announcements.
- Deleting a queue forgets its bindings and consumer tags.

## What's Next?

The broker is served over the network by later pieces of PyMQ.
