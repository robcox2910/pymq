# Leases and Acks (Borrowing a Message)

## Library Due Dates

When you borrow a library book the librarian stamps a **due date** inside
the cover. Bring it back on time and the librarian ticks you off. Forget it,
and when the date passes the library says "that book is available again"
and lends it to someone else. If you turn up later with the old copy slip
the librarian says, "Sorry, that loan is over."

A queue lends messages the same way. A **lease** is the loan, `ack_timeout`
is how long it lasts, and the **ack** is handing the book back.

## At-Least-Once Means Maybe Twice

This is the most important thing on this page: PyMQ promises that every
message is delivered **at least once**. If a handler is slow, crashes or
forgets to answer, the lease runs out and the message goes to somebody
else. That means a handler may see the same message twice, so **make
handlers safe to repeat** (see the idempotency page for how).

## Leases in PyMQ

```python
from pynet.loop import run, sleep
from pynet.loop.testing import FakeClock

from pymq.broker import LeasedMessage, QueueConfig, WorkQueue


async def main() -> None:
    queue = WorkQueue("library", QueueConfig(ack_timeout=10.0))
    sam: list[LeasedMessage] = []
    kim: list[LeasedMessage] = []
    queue.add_consumer("sam", sam.append)
    queue.add_consumer("kim", kim.append)
    queue.put("Dragon Book", id="book-1")

    print("sam holds:", [m.envelope.body for m in sam], "attempt", sam[0].envelope.attempt)
    await sleep(12)  # Sam forgets to hand the book back
    print("kim holds:", [m.envelope.body for m in kim], "attempt", kim[0].envelope.attempt)
    print("sam's late ack accepted?", queue.ack(sam[0].delivery_tag))
    print("kim's ack accepted?", queue.ack(kim[0].delivery_tag))


run(main(), clock=FakeClock(autojump=True))
```

Output:

```text
sam holds: ['Dragon Book'] attempt 1
kim holds: ['Dragon Book'] attempt 2
sam's late ack accepted? False
kim's ack accepted? True
```

(`FakeClock` makes the 12 seconds pass instantly, so the example runs fast.)

- `ack(tag)` finishes a delivery. `nack(tag)` gives it back to try again.
- `touch(tag)` is the "renew" button: it restarts the lease clock so a
  long job is not taken away.
- All three return `False` when the delivery is **stale**: unknown, already
  settled, or expired. A stale answer never changes anything.
- Each delivery gets a fresh `delivery_tag` (1, 2, 3, ... never reused),
  and the message's `attempt` counts deliveries.

## What We Test

- An ack removes the message; a second ack is stale.
- A consumer that never answers loses its lease after `ack_timeout`, another
  consumer gets the message (attempt 2), and the late ack is refused.
- `touch` pushes the deadline out by a full `ack_timeout`.
- A put of an id that is currently leased counts as a duplicate.
- Closing the queue drops consumers and leases without requeueing.

## What's Next?

When a handler says "not this time", the message waits before its next try:
see retries and delays.
