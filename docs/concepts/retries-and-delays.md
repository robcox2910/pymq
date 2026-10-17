# Retries and Delays (The Snooze Button)

## Snooze Alarm

Your alarm rings and you press snooze. A good alarm does not ring again
straight away: first one minute, then two, then four, so you are not
bothered over and over. And after enough snoozes it gives up.

When a handler fails (`nack`), the queue waits before offering the message
again, and each wait is **double** the one before, up to a limit. This is
called **exponential backoff**, and it stops a broken job from hammering
the system.

## The Doubling Table

With `retry_delay=1`, `max_retry_delay=10` and `max_attempts=5`:

| Attempt that failed | Wait before next try |
| ------------------- | -------------------- |
| 1                   | 1 s                  |
| 2                   | 2 s                  |
| 3                   | 4 s                  |
| 4                   | 8 s                  |
| 5                   | none: dead letter    |

The table stops after four waits because the fifth failure is the last
attempt. The doubling rule itself (`retry_wait`) keeps going, though:
after attempt 5 it would say 16 s, but `max_retry_delay` holds every wait
at 10 s or less, which is why the code below prints `10.0` last. Even an
absurd attempt number (5000) just gives the cap.

## Retries in PyMQ

```python
from pynet.loop import run, sleep
from pynet.loop.testing import FakeClock

from pymq.broker import LeasedMessage, QueueConfig, WorkQueue


async def main() -> None:
    config = QueueConfig(retry_delay=1.0, max_retry_delay=10.0, max_attempts=5)
    print("waits:", [config.retry_wait(n) for n in range(1, 6)])
    queue = WorkQueue("jobs", config)
    seen: list[LeasedMessage] = []
    queue.add_consumer("worker", seen.append)
    queue.put("flaky job")
    for _ in range(3):
        queue.nack(seen[-1].delivery_tag, error="oops")
        await sleep(20)
        print("attempt", seen[-1].envelope.attempt, "delivered")


run(main(), clock=FakeClock(autojump=True))
```

Output:

```text
waits: [1.0, 2.0, 4.0, 8.0, 10.0]
attempt 2 delivered
attempt 3 delivered
attempt 4 delivered
```

A lease that runs out counts as a `nack` with the error `"ack timeout"`, and
a consumer that is removed has its leases nacked too. Each of these uses up
one attempt. Once `attempt >= max_attempts` the message becomes a dead
letter instead of waiting again.

## Retries Restart the Use-By Date

A queue's `message_ttl` is a use-by date: a message that waits longer than
that is dead-lettered as `ttl_expired`. The clock starts **every time the
message goes into the queue**: when it is put, when it comes back for a
retry (a nack, a lease running out, a consumer being removed), and when a
dead letter is replayed. Time spent waiting out a retry delay counts;
time spent leased to a consumer does not. So a message that keeps coming
back can stay alive much longer than `message_ttl` in total:

```python
from pynet.loop import run, sleep
from pynet.loop.testing import FakeClock

from pymq.broker import LeasedMessage, QueueConfig, WorkQueue


async def main() -> None:
    queue = WorkQueue("jobs", QueueConfig(message_ttl=5.0, retry_delay=1.0))
    queue.put("milk")  # the 5 s clock starts now
    await sleep(4)
    seen: list[LeasedMessage] = []
    queue.add_consumer("worker", seen.append)
    queue.remove_consumer("worker")  # handed back at 4 s: the clock starts again
    await sleep(4)
    print("at 8 s, waiting:", queue.stats().ready, "dead:", queue.stats().dead)
    await sleep(2)
    print("at 10 s, dead:", queue.stats().dead, queue.dead_letters()[0].reason)


run(main(), clock=FakeClock(autojump=True))
```

Output:

```text
at 8 s, waiting: 1 dead: 0
at 10 s, dead: 1 ttl_expired
```

The milk was put at 0 s with a 5 s use-by date, but at 8 s it is still
there: it came back at 4 s, and that restarted its clock. It finally
expires 5 s after it came back.

## What We Test

- Retries come after 1 s, then 2 s, and the third nack dead-letters the
  message as `max_attempts`, keeping the error text.
- A failing consumer callback is logged and its message is nacked.
- `retry_wait(5000)` returns the cap instead of overflowing.
- Removing a consumer returns its messages as used-up attempts.
- A message's TTL clock starts again when it comes back for a retry and
  when a dead letter is replayed: it is still waiting after its first
  use-by date and expires a full `message_ttl` after it came back.

## What's Next?

Messages that run out of attempts are not thrown away: see the dead letter
box.
