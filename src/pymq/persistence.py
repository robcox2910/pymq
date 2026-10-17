"""Persistence -- keep the queues safe when the program stops.

A real message broker doesn't forget its letters when the power goes off.
Before closing up for the night, the post office files every waiting letter
away, and in the morning it puts them all back on the counter in the same
order. That is what :func:`save_queues` and :func:`load_queues` do.

Letters someone picked up but never confirmed (acknowledged) are treated as
undelivered: they go back to the front of the line (in a priority queue,
the front of their own priority band), so they are handed out again. Real brokers do the same, because nobody said the job was done.

Example:
    >>> from pymq.queue import MessageQueue
    >>> from pymq.persistence import save_queues, load_queues
    >>> orders = MessageQueue("orders")
    >>> _ = orders.put("one coffee please")
    >>> save_queues([orders], "queues.json")
    >>> load_queues("queues.json")["orders"].get().body
    'one coffee please'

"""

import json
from collections.abc import Iterable
from pathlib import Path
from typing import cast

from pymq.atomic import atomic_write_text
from pymq.errors import PersistenceError
from pymq.message import Message
from pymq.queue import BaseMessageQueue, MessageQueue, PriorityMessageQueue

_KINDS: dict[str, type[BaseMessageQueue]] = {
    MessageQueue.kind: MessageQueue,
    PriorityMessageQueue.kind: PriorityMessageQueue,
}
_TEXT_FIELDS = ("id", "body", "timestamp")
_NUMBER_FIELDS = ("priority", "retries")


def save_queues(queues: Iterable[BaseMessageQueue], filepath: str | Path) -> None:
    """Save every queue, with its waiting messages and dead letters, to one JSON file.

    The file is replaced in one step, so a crash mid-save leaves the old
    version whole rather than half of each.

    Args:
        queues: The queues to save.
        filepath: Path to the JSON file to create or overwrite.

    Raises:
        PersistenceError: If writing to disk fails, or two queues share a
            name (the file could never be loaded back).

    """
    path = Path(filepath)
    entries = [queue.to_dict() for queue in queues]
    names = [entry["name"] for entry in entries]
    if duplicates := sorted({str(name) for name in names if names.count(name) > 1}):
        msg = f"Cannot save queues to {path}: more than one queue is called {duplicates[0]!r}"
        raise PersistenceError(msg)
    data = {"queues": entries}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(data, indent=2))
    except OSError as exc:
        msg = f"Failed to save queues to {path}: {exc}"
        raise PersistenceError(msg) from exc


def load_queues(filepath: str | Path) -> dict[str, BaseMessageQueue]:
    """Load the queues saved by :func:`save_queues`.

    Args:
        filepath: Path to the JSON file to read.

    Returns:
        Each saved queue, keyed by its name.

    Raises:
        PersistenceError: If the file can't be read, isn't JSON, or isn't a
            saved set of queues.

    """
    path = Path(filepath)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"Failed to load queues from {path}: {exc}"
        raise PersistenceError(msg) from exc
    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"Invalid JSON in {path}: {exc}"
        raise PersistenceError(msg) from exc

    # Refuse anything that isn't a saved set of queues, rather than loading it
    # as an empty one that the next save would write over.
    if not isinstance(data, dict) or not isinstance(
        saved := cast("dict[str, object]", data).get("queues"), list
    ):
        msg = f"{path} is not a saved set of queues: expected an object with a 'queues' list"
        raise PersistenceError(msg)

    queues: dict[str, BaseMessageQueue] = {}
    for entry in cast("list[object]", saved):
        queue = _queue_from(entry, path)
        if queue.name in queues:
            msg = f"{path} is not a saved set of queues: queue {queue.name!r} appears twice"
            raise PersistenceError(msg)
        queues[queue.name] = queue
    return queues


def _queue_from(entry: object, path: Path) -> BaseMessageQueue:
    """Rebuild one queue from its saved entry, checking its shape."""
    if not isinstance(entry, dict):
        msg = f"{path} is not a saved set of queues: each queue must be an object"
        raise PersistenceError(msg)
    fields = cast("dict[str, object]", entry)
    name, kind, max_retries = fields.get("name"), fields.get("kind"), fields.get("max_retries")
    if not isinstance(name, str):
        msg = f"{path} is not a saved set of queues: a queue has no name"
        raise PersistenceError(msg)
    if not isinstance(kind, str) or kind not in _KINDS:
        msg = f"{path} is not a saved set of queues: queue {name!r} has unknown kind {kind!r}"
        raise PersistenceError(msg)
    if not _is_int(max_retries) or cast("int", max_retries) < 0:
        msg = f"{path} is not a saved set of queues: queue {name!r} has a bad max_retries"
        raise PersistenceError(msg)
    return _KINDS[kind].from_messages(
        name,
        cast("int", max_retries),
        _messages_from(fields.get("waiting"), name, path),
        _messages_from(fields.get("dead_letters"), name, path),
    )


def _messages_from(saved: object, queue: str, path: Path) -> list[Message]:
    """Rebuild a list of saved messages, checking each one's shape."""
    if not isinstance(saved, list):
        msg = f"{path} is not a saved set of queues: queue {queue!r} has a bad message list"
        raise PersistenceError(msg)
    messages: list[Message] = []
    for item in cast("list[object]", saved):
        fields = cast("dict[str, object]", item) if isinstance(item, dict) else {}
        if not all(isinstance(fields.get(key), str) for key in _TEXT_FIELDS) or not all(
            _is_int(fields.get(key)) for key in _NUMBER_FIELDS
        ):
            msg = f"{path} is not a saved set of queues: queue {queue!r} has a bad message"
            raise PersistenceError(msg)
        messages.append(
            Message(
                id=cast("str", fields["id"]),
                body=cast("str", fields["body"]),
                timestamp=cast("str", fields["timestamp"]),
                priority=cast("int", fields["priority"]),
                retries=cast("int", fields["retries"]),
            )
        )
    return messages


def _is_int(value: object) -> bool:
    """Return True for a whole number (``True`` and ``False`` don't count)."""
    return isinstance(value, int) and not isinstance(value, bool)
