"""The broker server: the post office opens its doors to the network.

``BrokerServer`` listens on a TCP port with PyNet's messaging layer. Every
connection gets its own counter clerk (``Connection``) and its own outbox,
so a customer who stops reading their post only fills their own letterbox.

Example:
    async with BrokerServer() as server:
        print("listening on", server.port)

"""

import logging
from types import TracebackType
from typing import Self

from pynet.loop import sleep
from pynet.messaging import Peer, serve
from pynet.streams import Server

from pymq.broker._handlers import Connection
from pymq.broker.core import Broker
from pymq.broker.outbox import DEFAULT_MAX_PENDING
from pymq.broker.protocol import Json, broker_stats_to_json

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_HEARTBEAT_INTERVAL = 5.0


class BrokerServer:
    """Serve a ``Broker`` over the network.

    Args:
        broker: The broker to serve; a fresh one is made (and owned) if omitted.
        max_pending: The default event limit per subscription.
        heartbeat_interval: Seconds between pings to each client, or ``None``.

    """

    def __init__(
        self,
        broker: Broker | None = None,
        *,
        max_pending: int = DEFAULT_MAX_PENDING,
        heartbeat_interval: float | None = DEFAULT_HEARTBEAT_INTERVAL,
    ) -> None:
        """Create a server; nothing listens until ``start``."""
        self._owns_broker = broker is None
        self._broker = Broker() if broker is None else broker
        self._max_pending = max_pending
        self._heartbeat_interval = heartbeat_interval
        self._server: Server | None = None
        self._connections: set[Connection] = set()
        self._closed_drops = 0
        self._closed = False

    @property
    def broker(self) -> Broker:
        """Return the broker being served."""
        return self._broker

    @property
    def port(self) -> int:
        """Return the listening port.

        Raises:
            RuntimeError: If the server has not been started.

        """
        if self._server is None:
            msg = "BrokerServer is not started"
            raise RuntimeError(msg)
        return self._server.port

    @property
    def connection_count(self) -> int:
        """Return how many clients are connected."""
        return len(self._connections)

    async def start(self, host: str = DEFAULT_HOST, port: int = 0) -> None:
        """Start listening on ``host:port`` (0 picks a free port).

        Raises:
            RuntimeError: If the server was already started.

        """
        if self._server is not None:
            msg = "BrokerServer is already started"
            raise RuntimeError(msg)
        self._server = await serve(
            host, port, self._setup, heartbeat_interval=self._heartbeat_interval
        )
        logger.info("broker listening on %s:%d", host, self._server.port)

    async def close(self) -> None:
        """Stop listening and close every connection; close the broker only if owned."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for conn in list(self._connections):
            await conn.peer.close()
        await sleep(0)  # let aborted sends report their failure before we return
        if self._owns_broker and not self._closed:
            self._broker.close()
        self._closed = True

    async def __aenter__(self) -> Self:
        """Start (if not started yet) and return the server."""
        if self._server is None:
            await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the server."""
        await self.close()

    def _setup(self, peer: Peer) -> None:
        """Give a new peer its own connection handlers and outbox."""
        conn = Connection(peer, self._broker, self._max_pending, self._broker_stats, self._forget)
        self._connections.add(conn)
        logger.info("client connected from %s", peer.peername)

    def _forget(self, conn: Connection) -> None:
        """Drop a closed connection, keeping its event drop count."""
        if conn in self._connections:
            self._connections.discard(conn)
            self._closed_drops += conn.outbox.total_dropped

    def _broker_stats(self) -> Json:
        """Build the ``broker.stats`` reply: broker counts, connections and event drops."""
        out = broker_stats_to_json(self._broker.broker_stats())
        out["connections"] = len(self._connections)
        out["event_drops"] = self._closed_drops + sum(
            c.outbox.total_dropped for c in self._connections
        )
        return out
