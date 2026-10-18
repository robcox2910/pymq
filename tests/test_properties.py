"""Property-based tests -- rules that must hold for *every* input.

An ordinary test checks one example someone thought of: "put ``one coffee
please`` in a queue, save it, load it back, and the first message is still
``one coffee please``". A property test states a rule instead, such as
"whatever is in the queue, loading the save file gives back exactly that, in
the same delivery order", and lets Hypothesis invent hundreds of queues to try
to break it. If one breaks the rule, Hypothesis *shrinks* it down to the
smallest input that still fails, and shows you that.

Each test below says its rule in plain English in its docstring. The
strategies at the top are recipes for making random inputs; each one is
named for what it makes.

The step-by-step check of the queues against a plain list lives in
``tests/test_model.py``. See ``docs/property-testing.md`` for the walk-through.
"""

import copy
import json
import tempfile
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import cast

from hypothesis import given
from hypothesis import strategies as st
from pynet.loop import run, sleep
from pynet.loop.testing import FakeClock

from pymq.broker import protocol
from pymq.broker.model import Envelope, JsonValue, LeasedMessage, QueueConfig
from pymq.broker.workqueue import WorkQueue
from pymq.errors import PersistenceError
from pymq.message import Message
from pymq.persistence import load_queues, save_queues
from pymq.queue import BaseMessageQueue, PriorityMessageQueue
from tests.strategies import (
    PROPERTY_TIMEOUT,
    by_priority,
    message_bodies,
    normal_run_seed,
    priorities,
    queue_kinds,
    retry_limits,
)

# 10 seconds on a normal run, 10 minutes on the thorough one (see tests/strategies.py).
pytestmark = PROPERTY_TIMEOUT

# --- Strategies: recipes for random inputs -----------------------------------
# queue_kinds(), message_bodies(), priorities() and retry_limits() live in
# tests/strategies.py, shared with the model check.

# A JSON value is built from at most this many plain pieces. Hypothesis allows
# 100 by default, which makes each input slow to invent; small values find the
# same bugs, and a failure shrinks to a small value anyway.
MOST_PIECES_PER_VALUE = 10

# Steps in one random day at the work queue, and how many messages a clerk
# may hold at once. Short days find the same bugs and keep the test quick.
MOST_STEPS_PER_DAY = 30
PREFETCH = 2

# Things that can happen at a classic queue between opening and closing time.
PUT, GET, ACK, REJECT = "put", "get", "ack", "reject"


def queue_names() -> st.SearchStrategy[str]:
    """Make any queue name at all."""
    return st.text()


def queue_steps() -> st.SearchStrategy[tuple[str, str, int]]:
    """Make one thing that happens at a queue: put a message, take one, ack it or reject it."""
    # PUT is listed twice, so puts happen twice as often and the queue fills up.
    return st.tuples(st.sampled_from([PUT, PUT, GET, ACK, REJECT]), message_bodies(), priorities())


def queue_days() -> st.SearchStrategy[list[tuple[str, str, int]]]:
    """Make a whole day at a queue: a list of puts, gets, acks and rejects."""
    return st.lists(queue_steps())


def any_bytes() -> st.SearchStrategy[bytes]:
    """Make any run of raw bytes, including the empty one and ones that aren't text."""
    return st.binary()


def json_values() -> st.SearchStrategy[JsonValue]:
    """Make any JSON value: text, numbers, true/false, null, and lists and objects of them."""
    plain = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        # NaN ("not a number") is never equal to itself, so ``==`` could never
        # confirm it came back. That's a rule of floats, not of PyMQ.
        st.floats(allow_nan=False),
        st.text(),
    )
    return st.recursive(plain, lists_and_objects_of, max_leaves=MOST_PIECES_PER_VALUE)


def lists_and_objects_of(
    children: st.SearchStrategy[JsonValue],
) -> st.SearchStrategy[JsonValue]:
    """Make a JSON list, or a JSON object with text keys, holding values made by ``children``."""
    return st.one_of(st.lists(children), st.dictionaries(st.text(), children))


def envelopes() -> st.SearchStrategy[Envelope]:
    """Make any envelope the broker might send: any body, headers, attachments and numbers."""
    return st.builds(
        Envelope,
        id=st.text(),
        queue=st.text(),
        body=json_values(),
        headers=st.dictionaries(st.text(), st.text()),
        attachments=st.lists(st.binary()).map(tuple),
        priority=priorities(),
        attempt=st.integers(),
        enqueued_at=st.text(),
    )


@st.composite
def damaged_save_files(draw: st.DrawFn) -> dict[str, object]:
    """Make a real save file with one field swapped or taken out, or one list item copied."""
    queue = queue_after(draw(queue_kinds()), draw(queue_names()), 0, draw(queue_days()))
    document: dict[str, object] = {"queues": [queue.to_dict()]}
    fields, key = draw(st.sampled_from(fields_in(document)))
    # Innermost lists first, so a copied message is tried as often as a copied queue.
    lists = [items for items in reversed(lists_in(document)) if items]
    if draw(st.booleans()):
        fields[key] = draw(json_values())
    elif not lists or draw(st.booleans()):
        del fields[key]
    else:
        # A hand-edit that pastes a message (or a whole queue) in twice.
        items = draw(st.sampled_from(lists))
        items.insert(draw(st.integers(0, len(items))), copy.deepcopy(draw(st.sampled_from(items))))
    return document


def work_queue_steps() -> st.SearchStrategy[tuple[str, int]]:
    """Make one thing that happens at the work queue, with a number to pick who or what."""
    # "put" is listed twice, so puts happen twice as often and clerks stay busy.
    actions = ["put", "put", "ack", "nack", "drop", "wait", "swap clerk", "replay"]
    return st.tuples(st.sampled_from(actions), st.integers(0, 10))


def work_queue_days() -> st.SearchStrategy[list[tuple[str, int]]]:
    """Make a whole day at the work queue: a short list of steps."""
    return st.lists(work_queue_steps(), max_size=MOST_STEPS_PER_DAY)


# --- Helpers -----------------------------------------------------------------


def queue_after(
    kind: type[BaseMessageQueue], name: str, max_retries: int, day: list[tuple[str, str, int]]
) -> BaseMessageQueue:
    """Return a new queue of ``kind`` after playing ``day`` on it.

    ``get`` takes the next message and holds on to it. ``ack`` and
    ``reject`` settle the oldest message held, taking the next one first
    if none is held.
    """
    queue = kind(name, max_retries)
    held: list[Message] = []
    for action, body, priority in day:
        if action == PUT:
            queue.put(body, priority)
            continue
        if action == GET or not held:
            msg = queue.get()
            if msg is not None:
                held.append(msg)
        if action == ACK and held:
            queue.acknowledge(held.pop(0))
        elif action == REJECT and held:
            queue.reject(held.pop(0))
    return queue


def fields_in(document: object) -> list[tuple[dict[str, object], str]]:
    """List every field of every object in a JSON document, as (object, field name) pairs."""
    found: list[tuple[dict[str, object], str]] = []
    if isinstance(document, dict):
        fields = cast("dict[str, object]", document)
        for key, value in fields.items():
            found.append((fields, key))
            found.extend(fields_in(value))
    elif isinstance(document, list):
        for value in cast("list[object]", document):
            found.extend(fields_in(value))
    return found


def lists_in(document: object) -> list[list[object]]:
    """List every list in a JSON document, outermost first."""
    found: list[list[object]] = []
    if isinstance(document, dict):
        for value in cast("dict[str, object]", document).values():
            found.extend(lists_in(value))
    elif isinstance(document, list):
        items = cast("list[object]", document)
        found.append(items)
        for value in items:
            found.extend(lists_in(value))
    return found


def drain(queue: BaseMessageQueue) -> list[dict[str, object]]:
    """Take every waiting message, acknowledging each, and return them in delivery order."""
    taken: list[dict[str, object]] = []
    while (msg := queue.get()) is not None:
        queue.acknowledge(msg)
        taken.append(asdict(msg))
    return taken


def load_or_refuse(path: Path) -> None:
    """Load the queues at ``path``, accepting only a PersistenceError refusal.

    A PersistenceError is caught and ignored: refusing a bad file is fine. Any
    *other* exception escapes and fails the test. A file that *does* load must
    be one the queues can keep: take every message without acknowledging it,
    save again, and every message is still in the new file.
    """
    try:
        queues = load_queues(path)
    except PersistenceError:
        return
    counts = {name: queue.size + queue.dead_letter_count for name, queue in queues.items()}
    for queue in queues.values():
        while queue.get() is not None:
            pass
    save_queues(queues.values(), path)
    again = load_queues(path)
    assert {name: q.size + q.dead_letter_count for name, q in again.items()} == counts


# --- Save and load: whatever was waiting is waiting again ---------------------


@given(kind=queue_kinds(), name=queue_names(), max_retries=retry_limits(), day=queue_days())
def test_save_then_load_gives_back_the_same_queue(
    kind: type[BaseMessageQueue], name: str, max_retries: int, day: list[tuple[str, str, int]]
) -> None:
    """Loading a saved queue gives back every message, in the documented delivery order.

    Messages taken but never acknowledged go back to the front of the line
    (in a priority queue, the front of their own priority band).
    """
    queue = queue_after(kind, name, max_retries, day)
    before = queue.to_dict()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "queues.json"
        save_queues([queue], path)
        loaded = load_queues(path)[name]
    waiting = cast("list[dict[str, object]]", before["waiting"])
    if kind is PriorityMessageQueue:
        # ``sorted`` keeps equals in the order it found them, so taken
        # messages stay first within their own priority.
        waiting = sorted(waiting, key=by_priority)
    expected = {**before, "waiting": waiting}
    assert type(loaded) is kind
    assert loaded.to_dict() == expected
    # ``==`` thinks True == 1, so a priority that came back as ``True``
    # would pass it. ``repr`` writes them differently, so it checks types too.
    assert repr(loaded.to_dict()) == repr(expected)
    assert drain(loaded) == waiting


# --- Loading junk: a bad save file is refused, never a crash ------------------


@given(junk=any_bytes())
def test_any_bytes_on_disk_load_or_raise_persistence_error(junk: bytes) -> None:
    """Whatever bytes are in the file, loading gives queues or a PersistenceError, never another error."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "queues.json"
        path.write_bytes(junk)
        load_or_refuse(path)


@given(document=json_values())
def test_any_json_on_disk_load_or_raise_persistence_error(document: JsonValue) -> None:
    """Whatever JSON is in the file, loading gives queues or a PersistenceError, never another error."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "queues.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        load_or_refuse(path)


# why: the normal run's seed would otherwise come from this test's source, so
# rewording it could stop the run finding the bug the property-testing page
# breaks on purpose ("Try It Yourself"). Pinned; the thorough run stays random.
DAMAGED_FILE_SEED = 1


@normal_run_seed(DAMAGED_FILE_SEED)
@given(document=damaged_save_files())
def test_damaged_save_file_loads_or_raises_persistence_error(document: dict[str, object]) -> None:
    """A real save file with one field swapped or missing, or one item pasted twice, loads or gives a PersistenceError."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "queues.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        load_or_refuse(path)


# --- The wire: an envelope survives the trip between broker and client --------


@given(envelope=envelopes())
def test_envelope_survives_the_wire(envelope: Envelope) -> None:
    """Encoding an envelope, sending it as JSON text and decoding it gives back the same envelope."""
    wire = json.loads(json.dumps(protocol.envelope_to_json(envelope)))
    decoded = protocol.envelope_from_json(wire, envelope.attachments)
    assert decoded == envelope
    assert repr(decoded) == repr(envelope)


# --- The work queue: no message is ever lost or doubled ----------------------


@given(day=work_queue_days())
def test_work_queue_never_loses_or_duplicates_a_message(day: list[tuple[str, int]]) -> None:
    """Whatever happens in a day, at closing time every message put is in exactly one place.

    The places are: acknowledged, dead-lettered, or waiting to be delivered.
    The clock is a pretend one, wound on by hand, so "waiting a while" takes
    no real time.
    """
    clock = FakeClock()

    async def play_the_day() -> None:
        config = QueueConfig(max_attempts=3, ack_timeout=5.0)
        queue = WorkQueue("jobs", config)
        held: dict[int, LeasedMessage] = {}
        put_ids: list[str] = []
        acked: list[str] = []
        clerk = "clerk-0"
        queue.add_consumer(clerk, hold_in(held), prefetch=PREFETCH)
        for step, (action, pick) in enumerate(day):
            tags = sorted(held)
            if action == "put":
                put_ids.append(queue.put(step, id=f"m{step}").id)
            elif action == "ack" and tags:
                tag = tags[pick % len(tags)]
                if queue.ack(tag):
                    acked.append(held[tag].envelope.id)
                del held[tag]
            elif action in {"nack", "drop"} and tags:
                tag = tags[pick % len(tags)]
                queue.nack(tag, requeue=action == "nack")
                del held[tag]
            elif action == "wait":
                await wait(clock, pick)
            elif action == "swap clerk":
                queue.remove_consumer(clerk)
                clerk = f"clerk-{step + 1}"
                queue.add_consumer(clerk, hold_in(held), prefetch=PREFETCH)
            elif action == "replay":
                queue.replay_dead()
        # Closing time: send the clerk home (their messages go back), then
        # wait long enough for every retry to come due.
        queue.remove_consumer(clerk)
        await wait(clock, int(config.max_retry_delay))
        dead = [letter.envelope.id for letter in queue.dead_letters()]
        waiting = queue.ready_ids()
        assert sorted(acked + dead + waiting) == sorted(put_ids)
        stats = queue.stats()
        assert (stats.ready, stats.delayed, stats.in_flight) == (len(waiting), 0, 0)

    run(play_the_day(), clock=clock)


def hold_in(held: dict[int, LeasedMessage]) -> Callable[[LeasedMessage], None]:
    """Return a clerk that keeps every message it's handed in ``held``, by delivery tag."""

    def take(message: LeasedMessage) -> None:
        held[message.delivery_tag] = message

    return take


async def wait(clock: FakeClock, seconds: int) -> None:
    """Wind the pretend clock on one second at a time, letting each timer that comes due go off."""
    for _ in range(seconds):
        clock.advance(1)
        await sleep(0)
