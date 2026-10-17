# Dead Letters, Take Two (The Lost Property Office)

## Lost Property

When a school finds a coat nobody claims, it does not bin it. It goes to the
lost property office with a note: where it was found and why it is there.
Later someone can look through the box, hand things back, or clear it out.

A **dead letter** is a message the queue has given up delivering. It is kept
with a **reason** and, when there is one, the **error** the last consumer
reported.

| Reason         | Meaning                                         |
| -------------- | ----------------------------------------------- |
| `max_attempts` | It failed (or timed out) on every allowed try.  |
| `rejected`     | A consumer said `nack(requeue=False)`.          |
| `ttl_expired`  | It waited longer than `message_ttl`.            |

## Dead Letters in PyMQ

```python
from pynet.loop import run
from pynet.loop.testing import FakeClock

from pymq.broker import LeasedMessage, QueueConfig, WorkQueue


async def main() -> None:
    queue = WorkQueue("jobs", QueueConfig())
    got: list[LeasedMessage] = []
    queue.add_consumer("worker", got.append)
    queue.put("bad job", id="job-1")
    queue.nack(got[0].delivery_tag, requeue=False, error="cannot parse")

    letter = queue.dead_letters()[0]
    print(letter.reason.value, "|", letter.error, "| attempts:", letter.envelope.attempt)
    print("replayed:", queue.replay_dead())
    print("redelivered attempt:", got[1].envelope.attempt)
    queue.nack(got[1].delivery_tag, requeue=False)
    print("purged:", queue.purge_dead(), "left:", queue.stats().dead)


run(main(), clock=FakeClock(autojump=True))
```

Output:

```text
rejected | cannot parse | attempts: 1
replayed: 1
redelivered attempt: 1
purged: 1 left: 0
```

- `dead_letters()` lists the box, oldest first.
- `replay_dead()` puts letters back in the queue with a **fresh attempt
  count**, so they get all their tries again. Pass ids to replay only some.
- `purge_dead()` empties the box. The box also holds at most
  `max_dead_letters` letters; the oldest are dropped first (and counted).

## What We Test

- A rejected message is dead with reason `rejected` and keeps its error.
- Replaying a dead letter delivers it again as attempt 1.
- Replaying while consumers are attached (even failing ones) never loses or
  double-counts a letter.
- Exhausted retries give `max_attempts`; waiting too long gives
  `ttl_expired`.

## What's Next?

How many messages does each consumer get at once? See fair dispatch.
