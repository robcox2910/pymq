# PyMQ

**An educational message queue library built from scratch in Python.**

## What Is a Message Queue?

Imagine a busy bakery. The baker puts fresh loaves on the counter. Customers
line up and take loaves one at a time, first come first served. The baker
doesn't need to wait for each customer -- they just keep baking. The counter
holds the bread until someone picks it up.

A **message queue** works the same way for programs:

- **Producers** (bakers) put messages on the queue
- **Consumers** (customers) take messages off the queue
- The queue holds messages until someone processes them
- Producers and consumers don't need to be online at the same time

## Examples

### The Bakery Counter -- Basic Queue

Put messages on the counter and take them off, one at a time:

```python
from pymq.queue import MessageQueue

# Open the bakery counter
q = MessageQueue("orders")

# Baker puts loaves on the counter
q.put("sourdough")
q.put("rye")
q.put("baguette")

# Customers take them in order
msg = q.get()
print(msg.body)  # "sourdough" (first in, first out!)

# Tell the bakery you got it
q.acknowledge(msg)
```

### The Newsletter -- Pub/Sub

One publisher, many subscribers. Everyone gets a copy:

```python
from pymq.pubsub import PubSub

ps = PubSub()

# Subscribers sign up for topics they care about
def weather_fan(topic, message):
    print(f"Weather update: {message}")

def news_fan(topic, message):
    print(f"Breaking: {message}")

ps.subscribe("weather", weather_fan)
ps.subscribe("weather", news_fan)

# Publisher sends one message, both subscribers get it
ps.publish("weather", "Sunny with a chance of Python!")
# Output:
#   Weather update: Sunny with a chance of Python!
#   Breaking: Sunny with a chance of Python!
```

### The Express Lane -- Priority Queue

Urgent messages jump ahead of the line:

```python
from pymq.queue import PriorityMessageQueue

pq = PriorityMessageQueue("emergencies")

# Lower number = higher priority
pq.put("routine checkup", priority=10)
pq.put("FIRE ALARM!", priority=1)
pq.put("lunch order", priority=5)

# Highest priority comes out first
msg = pq.get()
print(msg.body)  # "FIRE ALARM!" (priority 1 beats 5 and 10)
```

### Returned Mail -- Dead Letter Queue

When a message fails too many times, it goes to a special place:

```python
from pymq.queue import MessageQueue

q = MessageQueue("deliveries", max_retries=2)
q.put("fragile package")

# Simulate failures
msg = q.get()
q.reject(msg)     # retry 1
msg = q.get()
q.reject(msg)     # retry 2
msg = q.get()
q.reject(msg)     # too many retries -- moved to dead letter queue

# Check the dead letter queue
failed = q.get_dead_letter()
print(failed.body)  # "fragile package"
```

## The Real Post Office -- Networked Broker

The bakery counter lives inside one program. A real post office has a front
door: many programs can walk in over the network, hand over parcels, and
wait for them to be collected. `pymq.broker` is that post office, built on
PyNet's own event loop.

Start a broker from the terminal (Ctrl-C stops it):

```bash
uv run pymq-broker --host 127.0.0.1 --port 5672 --heartbeat 5
```

Then talk to it with a `BrokerClient`. This snippet starts its own broker on
a free port so you can paste it straight in and run it:

```python
from pynet.loop import Event, run

from pymq.broker import BrokerClient, BrokerServer, Delivery, QueueConfig, TopicEvent


async def main() -> None:
    async with BrokerServer() as server:  # picks a free port; use pymq-broker for a real one
        async with BrokerClient("127.0.0.1", server.port) as mq:
            await mq.declare_queue("jobs", QueueConfig(ack_timeout=60))
            await mq.declare_queue("audit")
            done = Event()

            async def work(d: Delivery) -> None:
                print(d.body)  # returning normally acks; raising nacks
                done.set()

            async def on_event(e: TopicEvent) -> None:
                print("alert:", e.topic)

            await mq.consume("jobs", work, prefetch=4)
            await mq.subscribe("job.*.failed", on_event)
            await mq.bind("job.#", "audit")
            await mq.put("jobs", {"job": "nightly-report"}, delay=0.1)
            await mq.publish("job.nightly.failed", "disk full")
            await done.wait()
            print("audit queue holds:", (await mq.stats("audit")).ready)


run(main())
```

Runnable demos live in `examples/` (`broker_demo.py`, `work_queue.py`,
`topic_alerts.py`). The learning path starts at
[the docs index](docs/index.md).

## Developing alongside a sibling

PyMQ depends on its sibling PyNet as a PyPI package (`fromscratch-pynet` in
`pyproject.toml`), and `uv.lock` records the exact release. A change merged
into PyNet can't affect PyMQ until PyNet publishes a new release and PyMQ
chooses to upgrade.

To try an unreleased change from a PyNet checkout next to this one, install it
over the pinned release:

```bash
uv pip install -e ../pynet
uv run --no-sync pytest
```

Use `uv run --no-sync` while you do this: a plain `uv run` (like `uv sync`)
puts the pinned release back, which is how you undo the override.

## Features

- **FIFO Queue** -- First-in, first-out message storage
- **Priority Queue** -- Urgent messages first, powered by heapq
- **Pub/Sub** -- Publish to topics, subscribers get copies
- **Acknowledgment** -- Confirm processing or reject for retry
- **Dead Letter Queue** -- Failed messages collected for inspection
- **Saving Queues** -- `save_queues` / `load_queues` keep waiting messages across restarts
- **Message Factory** -- Auto-generated UUIDs and ISO timestamps
- **100% Typed** -- Full type annotations with strict Pyright checking

## Quick Start

```bash
# Install with uv
uv add pymq

# Or install from source
git clone https://github.com/robcox2910/pymq.git
cd pymq
uv sync --all-extras

# Run the tests
uv run pytest
```

<!-- docs:start -->
## Documentation

Full docs (with kid-friendly explanations of every idea): https://robcox2910.github.io/pymq/
<!-- docs:end -->

## Releasing

Other projects in the series ([PyStack](https://github.com/robcox2910/pystack)) use a pinned release of this one,
not whatever is on `main`. To publish a change for them:

1. Merge the change to `main`.
2. Tag it: `git tag -a vX.Y.Z -m vX.Y.Z && git push origin vX.Y.Z`.
   A change to the public API bumps the minor version (`v0.1.0` → `v0.2.0`);
   anything else bumps the patch (`v0.1.0` → `v0.1.1`).
3. In each project that uses it, edit the tag in `[tool.uv.sources]`, run
   `uv lock --upgrade-package <name>`, and open a pull request so CI checks
   the pair together.

<!-- series:start -->
## Related Projects

This project is one of a series that builds real systems "from scratch in Python" to teach how they work. See the [series home](https://robcox2910.github.io/) for the full story.

| Project | What it teaches | Repository | Docs |
| --- | --- | --- | --- |
| PyOS | Operating systems | [py-os](https://github.com/robcox2910/py-os) | [Docs](https://robcox2910.github.io/py-os/) |
| Pebble | Compilers and programming languages | [pebble-lang](https://github.com/robcox2910/pebble-lang) | [Docs](https://robcox2910.github.io/pebble-lang/) |
| PyDB | Relational databases | [pydb](https://github.com/robcox2910/pydb) | [Docs](https://robcox2910.github.io/pydb/) |
| PyKV | Key-value stores | [pykv](https://github.com/robcox2910/pykv) | [Docs](https://robcox2910.github.io/pykv/) |
| PyDocDB | Document databases | [pydoc-db](https://github.com/robcox2910/pydoc-db) | [Docs](https://robcox2910.github.io/pydoc-db/) |
| PyGraphDB | Graph databases | [pygraphdb](https://github.com/robcox2910/pygraphdb) | [Docs](https://robcox2910.github.io/pygraphdb/) |
| PyTSDB | Time-series databases | [pytsdb](https://github.com/robcox2910/pytsdb) | [Docs](https://robcox2910.github.io/pytsdb/) |
| PyVecDB | Vector databases | [pyvecdb](https://github.com/robcox2910/pyvecdb) | [Docs](https://robcox2910.github.io/pyvecdb/) |
| PyColDB | Column-family databases | [pycoldb](https://github.com/robcox2910/pycoldb) | [Docs](https://robcox2910.github.io/pycoldb/) |
| PyCrypt | Cryptography | [pycrypt](https://github.com/robcox2910/pycrypt) | [Docs](https://robcox2910.github.io/pycrypt/) |
| PyNet | Computer networking | [pynet](https://github.com/robcox2910/pynet) | [Docs](https://robcox2910.github.io/pynet/) |
| PyWeb | HTTP web servers | [pyweb](https://github.com/robcox2910/pyweb) | [Docs](https://robcox2910.github.io/pyweb/) |
| PyGit | Version control | [pygit](https://github.com/robcox2910/pygit) | [Docs](https://robcox2910.github.io/pygit/) |
| PySearch | Search engines | [pysearch](https://github.com/robcox2910/pysearch) | [Docs](https://robcox2910.github.io/pysearch/) |
| **PyMQ** | **Message queues** | [pymq](https://github.com/robcox2910/pymq) | [Docs](https://robcox2910.github.io/pymq/) |
| PyLLM | Language models (the brain) | [pyllm](https://github.com/robcox2910/pyllm) | [Docs](https://robcox2910.github.io/pyllm/) |
| PyScheduler | Job scheduling | [pyscheduler](https://github.com/robcox2910/pyscheduler) | [Docs](https://robcox2910.github.io/pyscheduler/) |
| PyStack | Full-stack integration | [pystack](https://github.com/robcox2910/pystack) | [Docs](https://robcox2910.github.io/pystack/) |
<!-- series:end -->

## License

MIT -- see [LICENSE](LICENSE) for details.
