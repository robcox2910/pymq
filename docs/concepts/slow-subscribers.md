# Slow Subscribers (The Overflowing Letterbox)

## The Overflowing Letterbox

Imagine a postie walking down a street. Most houses empty their letterbox
every day, but one family has gone away and their box is stuffed full. A
silly postie would stand at that door, waiting for someone to come home,
and nobody else on the street would get their post.

A good postie does three things instead:

1. **Keeps the newest letters.** When the box is full, the oldest letter is
   pulled out to make room for today's one. Yesterday's news matters less.
2. **Writes down how many were thrown away**, so the family can see they
   missed some when they get back.
3. **Never stops the round** for one full box. Every other house still gets
   its post on time.

## Outboxes in PyMQ

On the networked broker every connection has its own **outbox** and its
own postie (a background drain task). The broker drops deliveries and
events into the outbox and walks away at once; it never waits for a slow
reader. Only that connection's postie waits on its socket.

- **Events** (from `subscribe`) are kept per subscription, up to
  `max_pending` (default 1 000). When a subscription's pile is full the
  oldest event is dropped and counted.
- **Deliveries** (from `consume`) are parcels that must be signed for, so
  they are **never** dropped. They do not pile up forever because
  `prefetch` limits how many a consumer can hold, and they go out *before*
  any waiting events.
- Several subscriptions on one connection take turns (round-robin), so a
  busy topic cannot starve a quiet one.

Here is an outbox with a letterbox that holds three letters. Ten letters
and one parcel arrive before the postie starts the round:

```python
from collections.abc import Sequence

from pynet.loop import run, sleep
from pynet.loop.testing import FakeClock

from pymq.broker import Envelope, JsonValue, LeasedMessage, TopicEvent
from pymq.broker.outbox import ConnectionOutbox


class Letterbox:
    """A pretend connection that prints every letter pushed through it."""

    async def push(
        self, method: str, body: JsonValue = None, *, attachments: Sequence[bytes] = ()
    ) -> None:
        assert isinstance(body, dict)
        if method == "deliver":
            print("parcel:", body["delivery_tag"])
        else:
            event = body["event"]
            assert isinstance(event, dict)
            print("letter:", event["body"])


async def main() -> None:
    outbox = ConnectionOutbox(Letterbox())
    outbox.add_subscription("sub-1", max_pending=3)  # the letterbox holds 3
    for n in range(1, 11):
        outbox.send_event("sub-1", TopicEvent("news", f"news #{n}", {}, (), "today"))
    envelope = Envelope("m1", "jobs", "homework", {}, (), 0, 1, "today")
    outbox.send_delivery(LeasedMessage("jobs", "ctag-1", 1, envelope))
    print("thrown away:", outbox.dropped("sub-1"))
    outbox.start()  # the postie starts the round
    await sleep(0.1)
    outbox.close()


run(main(), clock=FakeClock(autojump=True))
```

Output:

```text
thrown away: 7
parcel: 1
letter: news #8
letter: news #9
letter: news #10
```

The parcel jumped the queue, the three newest letters survived, and the
seven oldest were counted as dropped. (`FakeClock` makes the example run
instantly.)

On a real server you choose the size per subscription with
`topic.subscribe`'s `max_pending`, and the `broker.stats` request reports
`event_drops`: every event dropped by any connection so far, including
connections that have since closed.

## What We Test

- Deliveries are never dropped, even past `max_pending`.
- Events drop the oldest first and the count goes up by one per drop.
- A queued delivery goes out before queued events.
- Subscriptions on one connection take turns.
- A push that fails because the connection closed stops the postie; any
  other failure is logged and the postie carries on.
- **A subscriber that stops reading its socket** (Review Focus 3): one
  client subscribes to `#` with `max_pending=5` and never reads again,
  while another client subscribes too and consumes a queue. After 2 000
  events of about 1 KB and 10 queued messages, the working client gets
  every event and every message, and `broker.stats` shows drops for the
  stalled one.

## What's Next?

When a connection drops altogether, the client has to dial back in and set
everything up again: see reconnecting clients.
