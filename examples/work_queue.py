"""A work queue: Ann crashes on her first job, the lease comes back, and Bob does it.

Ann takes ``job-0`` and then crashes before she can ack it: her connection
is lost. The broker notices the line has gone, takes the lease back
(that used up attempt 1) and puts the job back on the queue. Bob starts
only once the job is back, so he gets it on attempt 2, then does the rest.
"""

from pynet.loop import Event, run, sleep, timeout

from pymq.broker import BrokerClient, BrokerServer, Delivery, QueueConfig

HOST = "127.0.0.1"
JOBS = 4
WAIT = 3.0
POLL = 0.01
RETRY_DELAY = 0.05


async def demo() -> None:
    """Run the producer and workers; Ann's crash hands job-0 to Bob."""
    done: set[str] = set()
    finished = Event()
    ann_has_job = Event()

    async def ann_work(delivery: Delivery) -> None:
        """Take the job and never settle it: Ann is about to crash."""
        print(
            f"ann took {delivery.body} (attempt {delivery.attempt}) and crashed: her connection is lost"
        )
        ann_has_job.set()

    async def bob_work(delivery: Delivery) -> None:
        """Do one job (auto-acked when this returns)."""
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
            await ann.consume("jobs", ann_work, prefetch=1, auto_ack=False)
            await producer.put("jobs", "job-0")
            await ann_has_job.wait()
            await ann.close()  # the crash: Ann's connection drops with job-0 unacked
            while (await producer.stats("jobs")).ready != 1:
                await sleep(POLL)  # wait for the broker to take the lease back
            print("the broker took job-0 back from ann (lease returned, attempt 1 used up)")
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
