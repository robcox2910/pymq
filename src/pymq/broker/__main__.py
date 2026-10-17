"""The ``pymq-broker`` command: open the post office from the terminal.

Example:
    pymq-broker --host 127.0.0.1 --port 5672

"""

import argparse
import logging
from collections.abc import Callable, Sequence

from pynet.loop import Event, run

from pymq.broker.core import Broker
from pymq.broker.router import TopicEvent
from pymq.broker.server import DEFAULT_HEARTBEAT_INTERVAL, DEFAULT_HOST, BrokerServer
from pymq.broker.topics import SYSTEM_PREFIX

logger = logging.getLogger("pymq.broker")

DEFAULT_PORT = 5672


def _log_dead(_sub_id: str, event: TopicEvent) -> None:
    """Log one dead-letter announcement."""
    logger.warning("dead letter on %s: %s", event.topic, event.body)


async def serve_forever(
    host: str,
    port: int,
    *,
    heartbeat: float,
    ready: Callable[[int], None] | None = None,
) -> None:
    """Run a broker server until the task is cancelled.

    Args:
        host: The address to listen on.
        port: The port to listen on (0 picks a free one).
        heartbeat: Seconds between pings to each client.
        ready: Called with the real port once the server is listening.

    """
    broker = Broker()
    broker.subscribe(f"{SYSTEM_PREFIX}.dead.*", _log_dead)
    server = BrokerServer(broker, heartbeat_interval=heartbeat)
    try:
        await server.start(host, port)
        logger.info("listening on %s:%d", host, server.port)
        if ready is not None:
            ready(server.port)
        await Event().wait()
    finally:
        await server.close()


def build_parser() -> argparse.ArgumentParser:
    """Return the command line parser."""
    parser = argparse.ArgumentParser(prog="pymq-broker", description="Run a PyMQ broker.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="address to listen on")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to listen on")
    parser.add_argument(
        "--heartbeat",
        type=float,
        default=DEFAULT_HEARTBEAT_INTERVAL,
        help="seconds between pings to each client",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the broker until Ctrl-C and return the exit code."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        run(serve_forever(args.host, args.port, heartbeat=args.heartbeat))
    except KeyboardInterrupt:
        logger.info("shutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
