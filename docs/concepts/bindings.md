# Bindings (A PO Box While You Are on Holiday)

## The PO Box

When you go on holiday you do not want to miss your post. So you rent a **PO
box** and tell the post office: "any card that says `post.*` goes in my box."
The cards pile up safely, and when you get home you collect them one by one.

A **subscription** is like standing at the notice board: you only hear the
news if you are there at the moment it is pinned up. A **binding** is the
standing order for the PO box: the broker copies matching announcements into a
queue, where they wait until a consumer collects them.

## Bindings in PyMQ

```python
from pynet.loop import run
from pynet.loop.testing import FakeClock

from pymq.broker import Broker, LeasedMessage


async def main() -> None:
    broker = Broker()
    broker.declare_queue("holiday-box")
    broker.bind("post.*", "holiday-box")
    print("published to queues:", broker.publish("post.card", "Wish you were here").queues)
    print("waiting:", broker.stats("holiday-box").ready)
    got: list[LeasedMessage] = []
    broker.consume("holiday-box", got.append)
    print("collected:", got[0].envelope.body, got[0].envelope.headers["topic"])


run(main(), clock=FakeClock(autojump=True))
```

Output:

```text
published to queues: 1
waiting: 1
collected: Wish you were here post.card
```

- `bind(pattern, queue)` needs the queue to exist. Binding twice does nothing extra.
- Every copy gets the header `topic` set to the topic it was published on.
- If a bound queue is full, the copy is dropped and counted as a
  `binding_drop` in `broker_stats()`. Publishing never fails because of it.
- Deleting a queue removes its bindings.

## What We Test

- `route` lists a bound queue once even if two bindings match.
- Binding is idempotent, and `unbind` / `unbind_queue` remove bindings.
- Bound queues receive a copy with the `topic` header.
- A full bound queue is counted in `binding_drops`.
- Deleting a queue removes its bindings.

## What's Next?

See the broker page for how queues, topics and bindings fit together.
