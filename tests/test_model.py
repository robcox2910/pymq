"""Model check -- a queue must behave exactly like a plain Python list.

A property test checks one rule for many inputs. A *model check* goes
further: Hypothesis invents a random *story* -- put this, take that,
reject it, acknowledge the other, close for the night -- and plays it on two
things at once:

- the real ``MessageQueue`` (or ``PriorityMessageQueue``);
- a **model**: three plain lists (the line, the messages taken, the dead
  letters), which we already trust to be right.

After every step the two must agree. If they ever disagree, Hypothesis
shrinks the story to the shortest list of steps that still goes wrong.

The model is deliberately *not* a copy of the queue's code. A FIFO queue
hands out ``line[0]``; a priority queue hands out ``sorted(line, key=priority)[0]``
(``sorted`` keeps equals in arrival order). No deque, no heap, no counter.

The broker's ``WorkQueue`` has no model check here. It only works inside a
running pynet event loop, and Hypothesis plays a story one step at a time
from ordinary code, outside any loop. Its "nothing lost, nothing doubled"
rule is a property in ``tests/test_properties.py`` instead, played with a
pretend clock.

See ``docs/property-testing.md`` for the walk-through.
"""

import shutil
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from typing import cast

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    MultipleResults,
    RuleBasedStateMachine,
    initialize,
    invariant,
    multiple,
    rule,
)

from pymq.message import Message
from pymq.persistence import load_queues, save_queues
from pymq.queue import BaseMessageQueue, MessageQueue, PriorityMessageQueue
from tests.strategies import (
    PROPERTY_TIMEOUT,
    by_priority,
    message_bodies,
    priorities,
    queue_kinds,
)

# 10 seconds on a normal run, 10 minutes on the thorough one (see tests/strategies.py).
pytestmark = PROPERTY_TIMEOUT

type Saved = dict[str, object]
"""One message written down in the notebook, the same shape ``to_dict`` uses."""

# Small retry limits, so stories are short enough to reach the dead-letter
# shelf. Any limit of 0 or more is allowed (see BaseMessageQueue).
MOST_RETRIES = 3

# Stories start with a few messages already waiting, so more of the 20 steps
# are spent taking, rejecting and dead-lettering than on filling an empty queue.
MOST_FIRST_CUSTOMERS = 3


def small_retry_limits() -> st.SearchStrategy[int]:
    """Pick a retry limit from 0 to 3."""
    return st.integers(min_value=0, max_value=MOST_RETRIES)


def strangers() -> st.SearchStrategy[Message]:
    """Make a message this queue never handed out (a made-up id)."""
    return st.builds(Message, id=st.text(), body=message_bodies(), timestamp=st.text())


def first_customers() -> st.SearchStrategy[list[tuple[str, int]]]:
    """Make up to three messages already waiting when the queue opens."""
    return st.lists(st.tuples(message_bodies(), priorities()), max_size=MOST_FIRST_CUSTOMERS)


class QueueMachine(RuleBasedStateMachine):
    """Play random puts, gets, acks, rejects and restarts on a queue and on plain lists."""

    # Messages handed back by put, and by get. Rules pick from these to
    # acknowledge or reject a message that really is (or was) in the queue.
    messages = Bundle("messages")
    taken = Bundle("taken")

    def __init__(self) -> None:
        """Start with an empty notebook and a folder for the nightly save file."""
        super().__init__()
        self.folder = Path(tempfile.mkdtemp())
        self.queue: BaseMessageQueue = MessageQueue("orders")
        self.kind: type[BaseMessageQueue] = MessageQueue
        self.max_retries = 0
        self.line: list[Saved] = []  # waiting, in the order they arrived
        self.out: list[Saved] = []  # handed out, not yet acknowledged
        self.dead: list[Saved] = []  # the dead-letter shelf, oldest first

    @initialize(
        target=messages,
        kind=queue_kinds(),
        max_retries=small_retry_limits(),
        first=first_customers(),
    )
    def open_queue(
        self, kind: type[BaseMessageQueue], max_retries: int, first: list[tuple[str, int]]
    ) -> MultipleResults[Message]:
        """Open a queue of a random kind and retry limit, with a few messages already waiting."""
        self.queue = kind("orders", max_retries)
        self.kind = kind
        self.max_retries = max_retries
        return multiple(*(self.check_put(body, priority) for body, priority in first))

    def in_delivery_order(self) -> list[Saved]:
        """Return the line in the order the queue should hand it out."""
        if isinstance(self.queue, PriorityMessageQueue):
            return sorted(self.line, key=by_priority)
        return list(self.line)

    @rule(target=messages, body=message_bodies(), priority=priorities())
    def put(self, body: str, priority: int) -> Message:
        """Put a message on the queue, and write it at the back of the line."""
        return self.check_put(body, priority)

    def check_put(self, body: str, priority: int) -> Message:
        """Put a message in both, checking the queue hands back a new message holding it."""
        msg = self.queue.put(body, priority)
        assert (msg.body, msg.priority, msg.retries) == (body, priority, 0)
        known = self.line + self.out + self.dead
        assert all(msg.id != other["id"] for other in known)
        self.line.append(asdict(msg))
        return msg

    @rule(target=taken)
    def get(self) -> Message | MultipleResults[Message]:
        """Take the next message, and remember it for later steps."""
        msg = self.check_get()
        if msg is None:
            nothing: MultipleResults[Message] = multiple()
            return nothing
        return msg

    @rule()
    def get_and_reject(self) -> None:
        """Take the next message and hand it straight back, like a clerk who can't deal with it."""
        msg = self.check_get()
        if msg is not None:
            self.check_reject(msg)

    def check_get(self) -> Message | None:
        """Take the next message: the one the notebook says is next, or None if the line is empty."""
        msg = self.queue.get()
        if not self.line:
            assert msg is None
            return None
        expected = self.in_delivery_order()[0]
        assert msg is not None
        assert asdict(msg) == expected
        self.line.remove(expected)
        self.out.append(expected)
        return msg

    @rule(msg=taken)
    def acknowledge(self, msg: Message) -> None:
        """Acknowledge a message we took: it's gone for good (if it's still out)."""
        self.check_acknowledge(msg)

    @rule(msg=taken)
    def reject(self, msg: Message) -> None:
        """Reject a message we took: back of the line, or the dead letters once retries > max_retries."""
        self.check_reject(msg)

    @rule(msg=st.one_of(messages, strangers()), reject=st.booleans())
    def settle_any_message(self, msg: Message, reject: bool) -> None:
        """Acknowledge or reject any message: taken, still waiting, or made up (those change nothing)."""
        if reject:
            self.check_reject(msg)
        else:
            self.check_acknowledge(msg)

    def check_acknowledge(self, msg: Message) -> None:
        """Acknowledge ``msg`` in both: the notebook crosses it off if it was taken."""
        self.out = [saved for saved in self.out if saved["id"] != msg.id]
        assert self.queue.acknowledge(msg) is None

    def check_reject(self, msg: Message) -> None:
        """Reject ``msg`` in both: if it was taken, one more retry, then the line or the dead letters."""
        was_out = [saved for saved in self.out if saved["id"] == msg.id]
        assert self.queue.reject(msg) is None
        for saved in was_out:
            self.out.remove(saved)
            retries = cast("int", saved["retries"]) + 1
            again = {**saved, "retries": retries}
            (self.dead if retries > self.max_retries else self.line).append(again)

    @rule(
        msg=st.one_of(messages, taken),
        body=message_bodies(),
        priority=priorities(),
        retries=st.integers(),
    )
    def scribble_on_a_message(self, msg: Message, body: str, priority: int, retries: int) -> None:
        """Change a message we were handed: the queue's own copy, and the notebook, stay as they were."""
        msg.body, msg.priority, msg.retries = body, priority, retries

    @rule()
    def get_dead_letter(self) -> None:
        """Take the oldest dead letter, or None if the shelf is empty."""
        msg = self.queue.get_dead_letter()
        if not self.dead:
            assert msg is None
        else:
            assert msg is not None
            assert asdict(msg) == self.dead.pop(0)

    @rule()
    def close_for_the_night(self) -> None:
        """Save the queue and load it back: taken messages go to the front of the line."""
        path = self.folder / "queues.json"
        save_queues([self.queue], path)
        self.queue = load_queues(path)["orders"]
        self.line = self.out + self.in_delivery_order()
        self.out = []

    @invariant()
    def same_messages_in_the_same_order(self) -> None:
        """After every step, the queue holds exactly what the notebook says, in order."""
        assert type(self.queue) is self.kind
        assert self.queue.name == "orders"
        assert self.queue.size == len(self.line)
        assert self.queue.dead_letter_count == len(self.dead)
        assert self.queue.to_dict() == {
            "name": "orders",
            "kind": self.kind.kind,
            "max_retries": self.max_retries,
            "waiting": self.out + self.in_delivery_order(),
            "dead_letters": self.dead,
        }

    def teardown(self) -> None:
        """Throw away the save-file folder."""
        shutil.rmtree(self.folder)


# why: 20 steps find the same bugs; 50 (the default) is most of the run time
QueueMachine.TestCase.settings = settings(stateful_step_count=20)
# pytest runs this like any other test class: one run plays 100 random stories.
# (Hypothesis doesn't say what type ``TestCase`` is, so ``cast`` tells pyright.)
TestQueueModel = cast("type[unittest.TestCase]", QueueMachine.TestCase)
