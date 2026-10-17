"""Topic alerts: a binding copies failures into a queue while a live subscriber sees everything."""

from pynet.loop import Event, run, timeout

from pymq.broker import BrokerClient, BrokerServer, TopicEvent

HOST = "127.0.0.1"
WAIT = 2.0
EVENTS = [
    ("job.backup.started", "backup began"),
    ("job.backup.failed", "disk full"),
    ("job.report.done", "report ready"),
    ("job.email.failed", "smtp down"),
]
FAILED_COUNT = 2


async def demo() -> None:
    """Publish job events and show what the live feed and the alerts queue each received."""
    seen: list[str] = []
    all_seen = Event()

    async def on_event(event: TopicEvent) -> None:
        """Print every event as it happens."""
        print(f"live feed: {event.topic} -> {event.body}")
        seen.append(event.topic)
        if len(seen) == len(EVENTS):
            all_seen.set()

    server = BrokerServer()
    await server.start(HOST, 0)
    try:
        async with timeout(WAIT), BrokerClient(HOST, server.port) as mq:
            await mq.declare_queue("alerts")
            await mq.bind("job.*.failed", "alerts")
            await mq.subscribe("#", on_event)
            for topic, body in EVENTS:
                await mq.publish(topic, body)
            await all_seen.wait()
            stats = await mq.stats("alerts")
            print(f"alerts queue holds: {stats.ready}")
    finally:
        await server.close()


def main() -> None:
    """Run the demo."""
    run(demo())


if __name__ == "__main__":
    main()
