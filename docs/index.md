---
title: "Build your own message queue in Python"
---

# PyMQ: Build your own message queue in Python

## The Bakery Counter

Imagine a busy bakery. The baker puts fresh loaves on the counter.
Customers line up and take loaves one at a time, first come first
served. The baker doesn't need to wait for each customer -- they just
keep baking. The counter holds the bread until someone picks it up.

A **message queue** works the same way for programs:

- **Producers** (bakers) put messages on the queue
- **Consumers** (customers) take messages off the queue
- The queue holds messages until someone processes them
- Producers and consumers don't need to be online at the same time

## Why Message Queues Matter

Message queues are everywhere:

- **Email** -- messages wait in your inbox until you read them
- **Online orders** -- your order sits in a queue until the warehouse processes it
- **Chat apps** -- messages are queued when you're offline, delivered when you reconnect
- **Video processing** -- uploaded videos queue up for encoding

## The Big Ideas

### 1. Queues -- The Bakery Counter

Messages line up in order. The first message added is the first one
taken out (**FIFO** -- First In, First Out). Like standing in line.

### 2. Pub/Sub -- The Newsletter

Instead of one customer taking one loaf, a **publisher** sends a
message to a **topic**, and ALL **subscribers** to that topic get
a copy. Like a newsletter -- one writer, many readers.

### 3. Priority -- The Express Lane

Some messages are more urgent than others. A **priority queue** lets
important messages jump ahead, like an express checkout lane.

### 4. Acknowledgment -- Signing for a Package

When a consumer takes a message, they **acknowledge** it to say
"I got it, you can delete it." If processing fails, the consumer can
**reject** the message to send it back on the queue for another try.

> **Good news:** the networked broker (see Part 2 below) has **leases**.
> If a consumer crashes or hangs without acknowledging, the lease runs out
> and the message goes to another consumer on its own. Read
> [Leases and Acks](concepts/leases-and-acks.md) to see how.

## Our Building Blocks

| Concept | Analogy | What It Does |
|---------|---------|-------------|
| **Queue** | Bakery counter | FIFO message storage |
| **Producer** | The baker | Add messages to a queue |
| **Consumer** | The customer | Take messages from a queue |
| **Pub/Sub** | Newsletter | Publish to topics, subscribers get copies |
| **Topic** | TV channel | Named stream of messages |
| **Priority Queue** | Express lane | Urgent messages first |
| **Dead Letter Queue** | Returned mail | Failed messages go here |
| **Acknowledgment** | Signing for a package | Confirm you processed it |

## Part 2: A Real Post Office

The bakery counter lives inside one program. A real post office has a
building, a front door and many customers walking in from different
streets. Part 2 grows PyMQ into a **networked broker** that runs on
PyNet's own event loop. Read these in order:

1. [The Broker](concepts/broker.md) -- the sorting room behind the counter
2. [Topics and Wildcards](concepts/topics-and-wildcards.md) -- the notice board
3. [Leases and Acks](concepts/leases-and-acks.md) -- borrowing a parcel for a while
4. [Retries and Delays](concepts/retries-and-delays.md) -- trying again, a little later
5. [Dead Letters v2](concepts/dead-letters-v2.md) -- the returned-mail shelf
6. [Fair Dispatch](concepts/fair-dispatch.md) -- no clerk gets all the parcels
7. [Idempotency](concepts/idempotency.md) -- sending twice, delivered once
8. [Bindings](concepts/bindings.md) -- standing orders from the notice board
9. [Slow Subscribers](concepts/slow-subscribers.md) -- the full letterbox
10. [Reconnecting Clients](concepts/reconnecting-clients.md) -- walking back in after the door closed

Want to see it run? Try the scripts in the `examples/` folder, or start a
broker with `pymq-broker`.

## Install

```bash
pip install fromscratch-pymq
```

Or get the whole series at once with `pip install fromscratch-pystack`. To read the code or run the tests, clone it from [GitHub](https://github.com/robcox2910/pymq).

## Let's Start!

Head to [Queues](concepts/queues.md) to learn how messages line up.
