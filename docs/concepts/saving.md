# Saving Queues (Closing Up for the Night)

## The Filing Cabinet

When a post office closes for the night, it doesn't throw away the
letters still waiting on the counter. It files them in a cabinet, and in
the morning it puts them back on the counter in exactly the same order.

A real message broker does the same. If the program stops, the waiting
messages are still there when it starts again. That is called
**persistence**.

## Saving and Loading in PyMQ

```python
from pymq.persistence import load_queues, save_queues
from pymq.queue import MessageQueue

orders = MessageQueue("orders")
orders.put("one coffee please")
orders.put("two muffins")

save_queues([orders], "queues.json")  # closing up for the night

queues = load_queues("queues.json")   # the next morning
queues["orders"].get().body           # 'one coffee please'
```

One file holds every queue: its name, its kind (`fifo` or `priority`),
its retry limit, the waiting messages and the dead letters. It is plain
JSON, so you can open it and read it.

## Letters Nobody Confirmed

What about a message someone picked up with `get()` but never
acknowledged? Nobody said the job was done, so it might not have been.
PyMQ puts those messages back at the **front** of the line when it saves,
so they are handed out again after a restart. (In a priority queue they go
to the front of their own priority band: a more urgent message still goes
first.) Real brokers such as
RabbitMQ do the same thing.

## Never a Half-Written File

Saving writes the new file next to the old one, makes sure it has really
reached the disk, then swaps it in with one step. If the computer crashes
halfway through, you still have the whole old file, never half of each.

If a file is damaged, or isn't a save file at all, `load_queues` raises a
`PersistenceError` that names the file. It never pretends the file was
empty, because the next save would then write over your messages. A file
with two queues of one name, or two messages in one queue with one id, is
refused too: the queue tells messages apart by their id, so one of the
pair would be lost.

## What We Test

- FIFO and priority queues come back in the same order.
- Message ids, timestamps, priorities and retry counts survive.
- Dead letters and retry limits survive.
- Unacknowledged messages are delivered again.
- Damaged or foreign files are refused and left untouched.
