"""Test saving queues to disk and loading them back, like a real broker."""

import json
import re
from pathlib import Path

import pytest

from pymq.errors import PersistenceError
from pymq.persistence import load_queues, save_queues
from pymq.queue import BaseMessageQueue, MessageQueue, PriorityMessageQueue


def _bodies(queue: BaseMessageQueue) -> list[str]:
    """Drain *queue*, returning the bodies in delivery order."""
    bodies: list[str] = []
    while (msg := queue.get()) is not None:
        queue.acknowledge(msg)
        bodies.append(msg.body)
    return bodies


def _round_trip(tmp_path: Path, *queues: BaseMessageQueue) -> dict[str, BaseMessageQueue]:
    """Save *queues* and load them straight back."""
    path = tmp_path / "queues.json"
    save_queues(queues, path)
    return load_queues(path)


class TestRoundTrip:
    """Whatever was waiting before saving is waiting again after loading."""

    def test_fifo_messages_come_back_in_order(self, tmp_path: Path) -> None:
        """A FIFO queue keeps its line order."""
        q = MessageQueue("orders", max_retries=5)
        for body in ("one", "two", "three"):
            q.put(body)
        loaded = _round_trip(tmp_path, q)["orders"]
        assert isinstance(loaded, MessageQueue)
        assert _bodies(loaded) == ["one", "two", "three"]

    def test_priority_queue_keeps_priority_order(self, tmp_path: Path) -> None:
        """A priority queue still serves the most urgent first, ties in arrival order."""
        q = PriorityMessageQueue("er")
        q.put("cold", priority=5)
        q.put("broken arm", priority=1)
        q.put("cough", priority=5)
        q.put("fever", priority=1)
        loaded = _round_trip(tmp_path, q)["er"]
        assert isinstance(loaded, PriorityMessageQueue)
        assert _bodies(loaded) == ["broken arm", "fever", "cold", "cough"]

    def test_message_details_survive(self, tmp_path: Path) -> None:
        """Ids, timestamps, priorities and retry counts are kept."""
        q = MessageQueue("orders")
        q.put("hello")
        msg = q.get()
        assert msg is not None
        q.reject(msg)
        loaded = _round_trip(tmp_path, q)["orders"].get()
        assert loaded == msg

    def test_settings_survive(self, tmp_path: Path) -> None:
        """The retry limit is kept, so dead-lettering works the same after a restart."""
        q = MessageQueue("orders", max_retries=0)
        q.put("doomed")
        loaded = _round_trip(tmp_path, q)["orders"]
        msg = loaded.get()
        assert msg is not None
        loaded.reject(msg)
        assert loaded.dead_letter_count == 1

    def test_dead_letters_survive(self, tmp_path: Path) -> None:
        """The dead-letter shelf is kept too."""
        q = MessageQueue("orders", max_retries=0)
        q.put("doomed")
        msg = q.get()
        assert msg is not None
        q.reject(msg)
        loaded = _round_trip(tmp_path, q)["orders"]
        dead = loaded.get_dead_letter()
        assert dead is not None
        assert dead.body == "doomed"
        assert loaded.size == 0

    def test_unacknowledged_messages_are_delivered_again(self, tmp_path: Path) -> None:
        """A message taken but never acknowledged goes back to the front of the line."""
        q = MessageQueue("orders")
        q.put("taken")
        q.put("waiting")
        assert q.get() is not None
        loaded = _round_trip(tmp_path, q)["orders"]
        assert _bodies(loaded) == ["taken", "waiting"]

    def test_several_queues_and_empty_ones(self, tmp_path: Path) -> None:
        """Every queue comes back under its own name, even an empty one."""
        a = MessageQueue("a")
        a.put("x")
        loaded = _round_trip(tmp_path, a, MessageQueue("empty"))
        assert sorted(loaded) == ["a", "empty"]
        assert loaded["empty"].size == 0

    def test_saving_twice_replaces_the_file(self, tmp_path: Path) -> None:
        """A second save overwrites the first, it doesn't add to it."""
        path = tmp_path / "queues.json"
        q = MessageQueue("orders")
        q.put("old")
        save_queues([q], path)
        assert q.get() is not None
        save_queues([MessageQueue("fresh")], path)
        assert sorted(load_queues(path)) == ["fresh"]

    def test_save_creates_missing_folders(self, tmp_path: Path) -> None:
        """Saving into a folder that doesn't exist yet creates it."""
        path = tmp_path / "deep" / "down" / "queues.json"
        save_queues([MessageQueue("q")], path)
        assert sorted(load_queues(path)) == ["q"]


class TestBadFiles:
    """A file that isn't a saved set of queues is refused, never loaded as empty."""

    def test_missing_file(self, tmp_path: Path) -> None:
        """Loading a file that isn't there names the file."""
        path = tmp_path / "nope.json"
        with pytest.raises(PersistenceError, match=re.escape(str(path))):
            load_queues(path)

    @pytest.mark.parametrize(
        "content",
        [
            "{not json",
            "[]",
            "{}",
            '{"queues": {}}',
            '{"queues": [1]}',
            '{"queues": [{"name": "q", "kind": "lifo", "max_retries": 3,'
            ' "waiting": [], "dead_letters": []}]}',
            '{"queues": [{"name": "q", "kind": "fifo", "max_retries": "3",'
            ' "waiting": [], "dead_letters": []}]}',
            '{"queues": [{"name": "q", "kind": "fifo", "max_retries": 3,'
            ' "waiting": [{"body": "no id"}], "dead_letters": []}]}',
            '{"queues": [{"name": "q", "kind": "fifo", "max_retries": 3,'
            ' "waiting": [], "dead_letters": []},'
            ' {"name": "q", "kind": "fifo", "max_retries": 3,'
            ' "waiting": [], "dead_letters": []}]}',
        ],
        ids=[
            "not-json",
            "list",
            "no-queues",
            "queues-not-list",
            "queue-not-object",
            "unknown-kind",
            "bad-retries",
            "bad-message",
            "duplicate-name",
        ],
    )
    def test_bad_file_is_refused_and_kept(self, tmp_path: Path, content: str) -> None:
        """Damaged or foreign files raise PersistenceError naming the file, and stay as they were."""
        path = tmp_path / "queues.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(PersistenceError, match=re.escape(str(path))):
            load_queues(path)
        assert path.read_text(encoding="utf-8") == content

    def test_saved_file_is_readable_json(self, tmp_path: Path) -> None:
        """The save file is plain JSON a learner can open and read."""
        path = tmp_path / "queues.json"
        q = MessageQueue("orders")
        q.put("hello")
        save_queues([q], path)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["queues"][0]["name"] == "orders"
        assert data["queues"][0]["waiting"][0]["body"] == "hello"

    def test_unwritable_location_raises_persistence_error(self, tmp_path: Path) -> None:
        """A save that can't be written raises PersistenceError, not a raw OSError."""
        blocker = tmp_path / "file"
        blocker.write_text("", encoding="utf-8")
        with pytest.raises(PersistenceError):
            save_queues([MessageQueue("q")], blocker / "queues.json")
