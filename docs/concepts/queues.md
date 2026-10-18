# Queues

## Standing in Line

A queue is just a line. The first person to join is the first to be
served. In computing, this is called **FIFO** -- First In, First Out.

```
Producer adds:   [A] → [B] → [C]
Consumer takes:  [A] first, then [B], then [C]
```

## Messages

Each item in the queue is a **message** -- a piece of data with some
metadata:

```python
Message(
    id="msg-001",
    body="Process order #42",
    timestamp="2026-03-22T10:30:00",
    priority=0,
)
```

## Queues in PyMQ

```python
from pymq.queue import MessageQueue

q = MessageQueue("orders")
q.put("Process order #42")
q.put("Process order #43")

msg = q.get()     # "Process order #42" (first in, first out)
q.acknowledge(msg)  # "Got it, delete it"
```

The message `get` hands you is a copy, like a receipt. Scribbling on it
(changing its body, priority or retry count) never changes the message
the queue is keeping; `acknowledge` and `reject` find the real one by its
`id`.

## What We Test

- Messages come out in the order they went in (FIFO).
- Getting from an empty queue returns None.
- Acknowledged messages are removed permanently.
- Unacknowledged messages can be requeued.
- Editing a message you were handed never changes the queue.

## Next Up

We have a simple queue. Now let's broadcast messages to multiple
listeners. Head to [Pub/Sub](pubsub.md).
