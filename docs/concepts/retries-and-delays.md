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

Without the cap the fifth wait would be 16 s; `max_retry_delay` holds any
wait at 10 s. Even an absurd attempt number (5000) is just the cap.

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

## What We Test

- Retries come after 1 s, then 2 s, and the third nack dead-letters the
  message as `max_attempts`, keeping the error text.
- A failing consumer callback is logged and its message is nacked.
- `retry_wait(5000)` returns the cap instead of overflowing.
- Removing a consumer returns its messages as used-up attempts.

## What's Next?

Messages that run out of attempts are not thrown away: see the dead letter
box.
