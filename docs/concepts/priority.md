# Priority Queue

## The Express Lane

At the grocery store, the express lane serves customers with few items
first, even if they arrived after people with full carts.

A **priority queue** works the same way: each message has a priority
number. Lower numbers = higher urgency = served first.

```
Queue: [order (pri=2), alert (pri=0), report (pri=1)]
Out:   alert (0), then report (1), then order (2)
```

## Priority Queue in PyMQ

```python
from pymq.queue import PriorityMessageQueue

q = PriorityMessageQueue("alerts")
q.put("Weekly report", priority=2)
q.put("SERVER DOWN!", priority=0)
q.put("Daily summary", priority=1)

msg = q.get()  # "SERVER DOWN!" (priority 0 = most urgent)
```

## What We Test

- Higher priority messages come out first (lower number = higher priority).
- Equal priority messages follow FIFO order.
- Default priority is 0 (highest).

## Next Up

What happens when a message can't be processed?
Head to [Dead Letter Queue](dead-letter.md).
