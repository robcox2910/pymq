# Dead Letter Queue

## Returned Mail

When the post office can't deliver a letter (wrong address, nobody
home after 3 attempts), they don't throw it away -- they return it
to the sender or put it in a "dead letter" box for review.

A **dead letter queue** (DLQ) works the same way. If a message fails
to be processed after a set number of attempts, it moves to the DLQ
instead of being lost forever. Someone can review the DLQ later to
figure out what went wrong.

## Dead Letter Queue in PyMQ

```python
from pymq.queue import MessageQueue

q = MessageQueue("orders", max_retries=3)

msg = q.get()
# Processing fails... message goes back on the queue
q.reject(msg)  # retry 1

msg = q.get()
q.reject(msg)  # retry 2

msg = q.get()
q.reject(msg)  # retry 3 -- moved to dead letter queue!

dlq_msg = q.get_dead_letter()  # Retrieve from DLQ for inspection
```

## What We Test

- Rejected messages are retried up to max_retries.
- After max retries, messages move to the dead letter queue.
- Dead letter messages can be retrieved for inspection.

## What's Next?

You've learned every concept in message queuing! Queues, pub/sub,
priorities, and dead letter handling. These are the same patterns
used by RabbitMQ, Apache Kafka, Amazon SQS, and every major
messaging system in the world.
