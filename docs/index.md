# What Is a Message Queue?

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
"I got it, you can delete it." If they crash before acknowledging,
the message goes back on the queue for someone else.

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

## Let's Start!

Head to [Queues](concepts/queues.md) to learn how messages line up.
