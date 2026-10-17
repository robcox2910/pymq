# Fair Dispatch (Buffet Plates)

## Buffet Plates

At a buffet the cook sets out plates one at a time. Each helper may carry a
few plates at once, but a helper whose hands are full is skipped until they
put one down. The cook goes round the helpers in turn, so nobody is left
empty-handed while someone else carries too much.

PyMQ does the same. Each consumer has a **prefetch**: the most unanswered
messages it may hold. The queue deals messages **round-robin**, starting
after the consumer it served last, and skips anyone who is full.

## Fair Dispatch in PyMQ

```python
from pynet.loop import run
from pynet.loop.testing import FakeClock

from pymq.broker import LeasedMessage, QueueConfig, WorkQueue


async def main() -> None:
    queue = WorkQueue("buffet", QueueConfig())
    fast: list[LeasedMessage] = []
    slow: list[LeasedMessage] = []
    queue.add_consumer("fast", fast.append, prefetch=3)
    queue.add_consumer("slow", slow.append, prefetch=1)
    for n in range(6):
        queue.put(f"plate {n}")
    print("fast:", len(fast), "slow:", len(slow), "ready:", queue.stats().ready)
    queue.ack(slow[0].delivery_tag)
    print("after one ack -> slow:", len(slow), "ready:", queue.stats().ready)


run(main(), clock=FakeClock(autojump=True))
```

Output:

```text
fast: 3 slow: 1 ready: 2
after one ack -> slow: 2 ready: 1
```

The default `prefetch` is 10. A prefetch below 1 is an error, and so is
using the same consumer tag twice. Plates that nobody can take yet simply
wait as `ready` and go out as soon as someone acks.

## What We Test

- Two consumers with room receive messages A, B, A, B.
- With prefetch 2 and five messages only two go out; an ack releases a third.
- Removing a consumer keeps the turn order fair for those that remain.
- Higher-priority messages (lower numbers) are handed out first.

## What's Next?

Next the broker puts all this behind a network connection, so consumers can
live in other programs.
