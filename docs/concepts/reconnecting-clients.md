# Reconnecting Clients (Ringing Back After the Line Drops)

## Ringing a Friend Back

You are on the phone with a friend, planning a picnic. You have agreed on
the park, you have said you will bring the sandwiches, and they are
reading out their list of games... and the line goes dead.

What do you do? You ring back. And when they answer, you do not start
from "hello, who is this?". You say *"where were we? The park, my
sandwiches, your games"*, and carry on.

Two more things are true about a dropped call:

1. **Anything said in the gap doesn't count.** If you shouted "OK, I'll
   bring the ball!" just as the line died, your friend never heard it.
   You have to say it again on the new call.
2. **Repeating yourself is fine if you use names.** If you are not sure
   your friend heard "I'll bring sandwich order #7", you can say it again.
   Because it has a number, they know it is the same order, not a second
   lot of sandwiches.

## How PyMQ's Client Rings Back

`BrokerClient` uses PyNet's `ReconnectingClient`, which redials by itself
when the connection drops, waiting a little longer after each failed try
(`initial_backoff`, `max_backoff`). On every new connection the client
says "where were we?" by **replaying** what it set up, in this order:

1. declare its queues (with the same configs),
2. bind its topic patterns to queues,
3. subscribe to its topics,
4. start its consumers again.

Things you cancelled or deleted are left out. If one step fails (say the
broker now has that queue with different settings), it is logged and the
rest still happen. Only once the replay is done does the client count
itself as connected again (`wait_connected` waits for this).

While the line is down:

- **Ordinary requests wait** for the line to come back, up to
  `request_timeout`, and then raise `BrokerUnavailableError`. A line that
  drops in the middle of a request raises the same. They are *not* sent
  again automatically, because most requests are not safe to repeat.
- **`put` resends.** The client gives every message an id *before* the
  first send (unless you gave one). If the answer never comes, it waits
  for the line and sends the very same message again, until `put_timeout`
  (30 s by default) has passed in total. The broker remembers recent ids
  (`dedupe_window`), so even if the first try *did* arrive, the queue
  still holds just one copy. The resend is then reported as
  `duplicate=True`.
- **Acks from before the drop don't count.** When a connection closes,
  the broker takes back every parcel it had lent on it, ready to hand it
  out again. A `Delivery` remembers which connection it came on, so
  `ack`, `nack` and `touch` on one from an older connection just return
  `False`, without even asking the broker. Otherwise an old ack might
  sign for the *new* copy that someone else is working on.
- **Server names change.** The new connection hands out new consumer tags
  and subscription ids, so `Consumer.tag` and `Subscription.sub_id` are
  `None` while the line is down and get their new values after the replay.

Here a broker keeps running while its server (the phone line) restarts on
the same port:

```python
from pynet.loop import run, sleep

from pymq.broker import Broker, BrokerClient, BrokerServer, Delivery, QueueConfig


async def main() -> None:
    broker = Broker()  # the post office itself survives; only the phone line drops
    server = BrokerServer(broker)
    await server.start()
    port = server.port
    held: list[Delivery] = []

    async def keep(delivery: Delivery) -> None:
        held.append(delivery)  # hold on to it; we ack by hand below

    client = BrokerClient("127.0.0.1", port, initial_backoff=0.05, max_backoff=0.2)
    async with client:
        await client.declare_queue("chores", QueueConfig(retry_delay=0.01))
        consumer = await client.consume("chores", keep, auto_ack=False)
        await client.put("chores", "feed the cat", id="chore-1")
        while not held:
            await sleep(0.01)
        print("first tag:", consumer.tag)

        await server.close()  # the line drops...
        while client.connected:
            await sleep(0.01)
        server = BrokerServer(broker)
        await server.start(port=port)  # ...and the post office picks up again
        await client.wait_connected(timeout=2)
        while len(held) < 2:
            await sleep(0.01)
        print("new tag:", consumer.tag)

        old, new = held
        print("old ack counted?", await old.ack())
        print("new ack counted?", await new.ack(), "- attempt", new.attempt)
        again = await client.put("chores", "feed the cat", id="chore-1")
        print("same id sent again, duplicate?", again.duplicate)
        print("copies waiting:", (await client.stats("chores")).ready)
    await server.close()
    broker.close()


run(main())
```

Output:

```text
first tag: ctag-1
new tag: ctag-2
old ack counted? False
new ack counted? True - attempt 2
same id sent again, duplicate? True
copies waiting: 0
```

The consumer came back by itself with a new tag. The parcel lent on the
old line was handed out again (attempt 2), so the old delivery's ack was
ignored and the new one's counted. Sending `chore-1` a second time left no
extra copy behind.

## What We Test

- **A broker restart** (a new `Broker` on the same port): the queue is
  declared again by the replay, new messages reach the old consumer, and
  `Consumer.tag` changes.
- Bindings and subscriptions are replayed: an event published after the
  restart reaches both the subscriber and the bound queue.
- Cancelled consumers and subscriptions, and deleted queues, are not
  replayed; a replay step the broker refuses is logged and the rest go on.
- **Acks from an old connection** (Review Focus 2): `touch` and `ack` on a
  delivery from before the drop return `False`, the server sees no settle
  request at all, and the redelivered copy is still leased until it is
  acked itself.
- **A put whose answer is lost** (Review Focus 5): a test server stores
  the first put and then hangs up instead of answering. The client resends
  the same id and the queue holds exactly one message, with one duplicate
  counted.
- `put` gives up with `BrokerUnavailableError` after `put_timeout` when the
  broker never comes back; other requests do the same after
  `request_timeout`, and one made while disconnected is answered once the
  broker returns.
- A consumer the server knows but the client does not (an "orphan") is
  cancelled, so its parcels are not used up by lease timeouts.
- Once `close` has started, late pushes start no new handlers.
