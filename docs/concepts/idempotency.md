# Idempotency (Safe to Send Twice)

## Raffle Ticket Numbers

At a school raffle every ticket has a number. If you hand your ticket to
the helper and they are not sure they heard you, you can hand it over
again -- they just look at the number, see it is already in the bucket,
and smile. One ticket, one entry, no matter how many times you shout it.

An operation that is safe to repeat is called **idempotent**. Networks
drop messages, so a sender often has to ask "did that arrive?" and send
again. If every message carries its own **id** (the ticket number), the
queue can spot the repeat and ignore it.

## Idempotency in PyMQ

```python
from pynet.loop import run

from pymq.broker import QueueConfig, WorkQueue


async def main() -> None:
    queue = WorkQueue("raffle", QueueConfig())

    first = queue.put("Sam's ticket", id="ticket-42")
    again = queue.put("Sam's ticket", id="ticket-42")

    print(first.duplicate, again.duplicate)
    print(queue.stats().ready, queue.stats().duplicates)


run(main())
```

Output:

```text
False True
1 1
```

The queue remembers each id for `dedupe_window` seconds (300 by default).
After that the id is forgotten, so a very late resend would be accepted as
new. An id that is still waiting in the queue is always treated as a repeat.

## What We Test

- A repeated id inside the window is ignored and counted as a duplicate.
- The same id is accepted again once the window has passed.
- A duplicate never raises "queue full", even when the queue is full.
- An id that is still queued is never stored twice.

## What's Next?

Messages can also wait: delays, expiry and the dead letter box are all
part of the same queue storage.
