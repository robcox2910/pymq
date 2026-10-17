"""The sorting room on its own: put, consume, nack and a dead letter (no network)."""

from pynet.loop import run
from pynet.loop.testing import FakeClock

from pymq.broker import Broker, LeasedMessage, QueueConfig


async def demo() -> None:
    """Send one parcel that nobody can deliver and watch it become a dead letter."""
    broker = Broker()
    broker.declare_queue("parcels", QueueConfig(max_attempts=1))
    broker.subscribe("$sys.dead.*", lambda _sub, e: print("announcement:", e.topic))
    got: list[LeasedMessage] = []
    broker.consume("parcels", got.append)
    broker.put("parcels", "fragile vase", id="parcel-1")
    print("received:", got[0].envelope.body)
    broker.nack("parcels", got[0].delivery_tag, requeue=False, error="address not found")
    print("dead letters:", len(broker.dead_letters("parcels")))
    broker.close()


def main() -> None:
    """Run the demo on a virtual clock so it finishes instantly."""
    run(demo(), clock=FakeClock(autojump=True))


if __name__ == "__main__":
    main()
