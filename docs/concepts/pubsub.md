# Pub/Sub

## The Newsletter

A regular queue is like a bakery counter -- one customer takes each
loaf. But what if you want EVERYONE to get a copy? That's a
**newsletter**: one publisher, many subscribers.

**Pub/Sub** (Publish/Subscribe) works the same way:

```
Publisher sends "Breaking News!"
    │
    ├── Subscriber A gets "Breaking News!"
    ├── Subscriber B gets "Breaking News!"
    └── Subscriber C gets "Breaking News!"
```

## Topics -- TV Channels

Messages are organized into **topics** (like TV channels). You
subscribe to the channels you care about:

```python
from pymq.pubsub import PubSub

bus = PubSub()
bus.subscribe("sports", my_handler)
bus.subscribe("weather", another_handler)

bus.publish("sports", "Team wins championship!")
# Only my_handler is called -- another_handler ignores sports
```

## What We Test

- Publishing delivers to all subscribers of that topic.
- Subscribers to other topics don't receive the message.
- Multiple subscribers to the same topic all get the message.
- Unsubscribing stops delivery.

## Next Up

Some messages are more urgent than others.
Head to [Priority Queue](priority.md).
