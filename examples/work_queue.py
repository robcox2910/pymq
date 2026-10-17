"""A work queue: one producer, two workers, and a worker that crashes on its first job."""

from pynet.loop import Event, run, timeout

from pymq.broker import BrokerClient, BrokerServer, Delivery, DeliveryHandler, QueueConfig

HOST = "127.0.0.1"
JOBS = 4
WAIT = 2.0
RETRY_DELAY = 0.05


async def demo() -> None:
    """Run the producer and workers; the crashed job is redelivered to the other worker."""
    done: set[str] = set()
    finished = Event()
    crashed: list[str] = []

    def make_worker(name: str, *, flaky: bool) -> DeliveryHandler:
        """Return a worker; a flaky one raises on the first job it sees."""

        async def work(delivery: Delivery) -> None:
            """Do one job, or crash (which nacks it so another worker gets it)."""
            if flaky and not crashed:
                crashed.append(str(delivery.body))
                print(f"{name} crashed on {delivery.body} (attempt {delivery.attempt})")
                msg = "worker crashed"
                raise RuntimeError(msg)
            print(f"{name} finished {delivery.body} (attempt {delivery.attempt})")
            done.add(str(delivery.body))
            if len(done) == JOBS:
                finished.set()

        return work

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
            await ann.consume("jobs", make_worker("ann", flaky=True), prefetch=1)
            await bob.consume("jobs", make_worker("bob", flaky=False), prefetch=1)
            for number in range(JOBS):
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
