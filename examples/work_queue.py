"""A work queue: Ann crashes on the first job, so Bob gets it on its second attempt."""

from pynet.loop import Event, run, timeout

from pymq.broker import BrokerClient, BrokerServer, Delivery, QueueConfig

HOST = "127.0.0.1"
JOBS = 4
WAIT = 3.0
RETRY_DELAY = 0.3  # long enough for Ann to leave before the job comes back


async def demo() -> None:
    """Run the producer and workers; the crashed job can only go to Bob."""
    done: set[str] = set()
    finished = Event()
    ann_crashed = Event()

    async def ann_work(delivery: Delivery) -> None:
        """Crash on the job: give it back with a note instead of finishing it."""
        print(f"ann crashed on {delivery.body} (attempt {delivery.attempt})")
        await delivery.nack(error="ann crashed")
        ann_crashed.set()

    async def bob_work(delivery: Delivery) -> None:
        """Do one job."""
        print(f"bob finished {delivery.body} (attempt {delivery.attempt})")
        done.add(str(delivery.body))
        if len(done) == JOBS:
            finished.set()

    server = BrokerServer()
    await server.start(HOST, 0)
    try:
        async with (
            timeout(WAIT),
            BrokerClient(HOST, server.port) as producer,
            BrokerClient(HOST, server.port) as ann,
            BrokerClient(HOST, server.port) as bob,
        ):
            await producer.declare_queue("jobs", QueueConfig(retry_delay=RETRY_DELAY))
            ann_consumer = await ann.consume("jobs", ann_work, prefetch=1, auto_ack=False)
            await producer.put("jobs", "job-0")
            await ann_crashed.wait()
            await ann_consumer.cancel()  # Ann has gone home; only Bob is left
            await bob.consume("jobs", bob_work, prefetch=1)
            for number in range(1, JOBS):
                await producer.put("jobs", f"job-{number}")
            await finished.wait()
    finally:
        await server.close()
    print("all jobs done")


def main() -> None:
    """Run the demo."""
    run(demo())


if __name__ == "__main__":
    main()
