"""Tests for the ``pymq-broker`` command line."""

import logging

import pytest
from pynet.loop import Event, run, sleep, spawn, timeout

from pymq.broker import BrokerClient, Delivery
from pymq.broker.__main__ import main, serve_forever

HOST = "127.0.0.1"
WAIT = 3.0
ARGPARSE_ERROR = 2
POLL = 0.01


class TestCli:
    """Test the broker command line."""

    def test_bad_port_exits_with_code_two(self) -> None:
        """Reject a non-numeric port the way argparse does."""
        with pytest.raises(SystemExit) as info:
            main(["--port", "x"])
        assert info.value.code == ARGPARSE_ERROR

    def test_serve_forever_reports_port_and_serves_clients(self) -> None:
        """Start on port 0, report the port and let a client put and consume."""

        async def body() -> None:
            ports: list[int] = []
            ready = Event()

            def on_ready(port: int) -> None:
                ports.append(port)
                ready.set()

            task = spawn(serve_forever(HOST, 0, heartbeat=5.0, ready=on_ready))
            async with timeout(WAIT):
                await ready.wait()
                got: list[object] = []
                done = Event()

                async def handler(delivery: Delivery) -> None:
                    got.append(delivery.body)
                    done.set()

                async with BrokerClient(HOST, ports[0]) as mq:
                    await mq.declare_queue("jobs")
                    await mq.consume("jobs", handler)
                    await mq.put("jobs", "hello")
                    await done.wait()
            task.cancel()
            assert got == ["hello"]

        run(body())

    def test_connects_and_disconnects_are_logged_at_info(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Log both a client connecting and disconnecting at the CLI's INFO level."""

        async def body() -> None:
            ports: list[int] = []
            ready = Event()

            def on_ready(port: int) -> None:
                ports.append(port)
                ready.set()

            task = spawn(serve_forever(HOST, 0, heartbeat=5.0, ready=on_ready))
            async with timeout(WAIT):
                await ready.wait()
                async with BrokerClient(HOST, ports[0]) as mq:
                    await mq.list_queues()
                while "client disconnected" not in caplog.text:
                    await sleep(POLL)
            task.cancel()

        with caplog.at_level(logging.INFO, logger="pymq.broker"):
            run(body())
        assert "client connected" in caplog.text
        assert "client disconnected" in caplog.text
