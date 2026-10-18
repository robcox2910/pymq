# Property Testing (Every Letter Accounted For)

## The Postal Inspector

Picture a post office. Letters come in, wait in a tray, go out with a
postman, and either get delivered or come back to try again. Letters that
can't be delivered after too many tries go on the dead-letter shelf. At
night the whole lot is filed in a cabinet, and in the morning it all comes
back out in the same order. PyMQ is that post office, made of Python.

How would you check the post office works? The usual way: post one
letter, watch it come out the other end, and smile. That's an **example
test**. It's useful, but it only tells you about the one letter you
thought of.

Now imagine a postal inspector. All day they post thousands of letters --
long ones, empty ones, ones addressed in Chinese, urgent ones, ones they
send back three times -- and they check a single rule:

> Whatever you post, every letter is accounted for: delivered, waiting,
> or on the dead-letter shelf. Never lost, never doubled.

That rule is a **property**. It doesn't say what any one letter looks
like. It says something that must be true for *every* letter.

When the inspector catches the post office breaking the rule, they don't
report "something went wrong this afternoon, with a big sack of mail".
They try again with fewer letters, shorter letters, simpler steps,
checking it still goes wrong each time, until it's as simple as it can
be. That boiling-down is called **shrinking**, and it's the best part:
the smallest failing input usually points straight at the bug.

In Python, the inspector is a library called
[Hypothesis](https://hypothesis.readthedocs.io/). PyMQ's property tests
live in `tests/test_properties.py`, next to the ordinary example tests,
and the model check (more on that below) is in `tests/test_model.py`.

## A Property from PyMQ

PyMQ can [save its queues and load them again](concepts/saving.md). That's
the post office closing for the night: whatever was in the tray at closing
time must be back in the tray in the morning, in the same order. Here's
the property:

```python
def queue_kinds() -> st.SearchStrategy[type[BaseMessageQueue]]:
    """Pick a kind of queue: first-come-first-served, or most urgent first."""
    return st.sampled_from([MessageQueue, PriorityMessageQueue])


def queue_steps() -> st.SearchStrategy[tuple[str, str, int]]:
    """Make one thing that happens at a queue: put a message, take one, ack it or reject it."""
    return st.tuples(st.sampled_from([PUT, PUT, GET, ACK, REJECT]), message_bodies(), priorities())


def queue_days() -> st.SearchStrategy[list[tuple[str, str, int]]]:
    """Make a whole day at a queue: a list of puts, gets, acks and rejects."""
    return st.lists(queue_steps())


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
```

Line by line:

- **The strategies** (`queue_kinds()`, `queue_steps()` and `queue_days()`)
  are recipes for random inputs. `queue_days()` makes a whole day at the
  counter: a list of steps, each one "put a message", "take one",
  "acknowledge one" or "reject one". The message bodies and priorities
  come from `message_bodies()` and `priorities()` in `tests/strategies.py`
  (shared with the model check): any text at all, and any whole number,
  negative ones too. `queue_names()` is any text and `retry_limits()` is
  any whole number, 0 or more.
- **`@given(...)`** tells Hypothesis to run the test many times (100 in a
  normal run), each time with a fresh kind of queue, name, retry limit and
  day.
- **The docstring** says the rule in plain English. If you can't say the
  rule in one sentence, it probably isn't a good property yet.
- **`queue_after(...)`** is a small helper that makes the queue and plays
  the day on it, so some messages are waiting, some are out with a
  consumer, and some may be on the dead-letter shelf.
- **`before = queue.to_dict()`** writes down the queue before saving.
  `to_dict()` already lists the messages taken but never acknowledged
  *first*: nobody said they were dealt with, so they go out again.
- **`sorted(waiting, key=by_priority)`** is what a priority queue does
  with that list: most urgent first. Python's `sorted` never swaps two
  equal things, so a taken message stays ahead of the others with its
  priority, exactly as [Saving Queues](concepts/saving.md) promises.
- **The first `assert`s** are the rule: same kind of queue, and the same
  name, retry limit, messages and dead letters. This kind of property is
  called a **round-trip**.
- **The `repr` line** is there because Python thinks `True == 1`. A
  priority that came back as `True` would pass `==`, but `repr()` writes
  `True` and `1` differently, so it checks the types too.
- **`drain(loaded)`** takes every message out of the loaded queue, one
  `get()` at a time. They must come out in exactly the expected order:
  the real test of a queue is what it hands you.

Notice what the test *doesn't* say. It never says "save `one coffee
please` and you get `one coffee please`". It doesn't need to know any
answers. It only needs a rule that's always true.

PyMQ has five more properties:

| Rule | Test |
|---|---|
| Whatever bytes are in the file, loading gives queues or a `PersistenceError`, never another error | `test_any_bytes_on_disk_load_or_raise_persistence_error` |
| Whatever JSON is in the file, loading gives queues or a `PersistenceError`, never another error | `test_any_json_on_disk_load_or_raise_persistence_error` |
| A real save file with one field swapped or missing, or one message or queue pasted in twice, gives a `PersistenceError` or loads; if it loads, taking every message and saving again loses none | `test_damaged_save_file_loads_or_raises_persistence_error` |
| Encoding an envelope, sending it as JSON text and decoding it gives back the same envelope | `test_envelope_survives_the_wire` |
| Whatever happens in a day at the broker's work queue, at closing time every message is in exactly one place | `test_work_queue_never_loses_or_duplicates_a_message` |

The three "junk" properties matter because a program using PyMQ writes
`except PersistenceError:` to handle a broken save file; any other error
would crash it instead. But **a loader that does nothing and refuses
every file passes all three junk properties**, so the round-trip sits
beside them: it fails straight away if loading never gives anything back.

The last one is the inspector's rule from the top of this page, played on
the [broker's](concepts/broker.md) work queue with clerks who take,
acknowledge, hand back and drop messages, and a pretend clock wound on by
hand so leases run out without anyone waiting.

## Checking Against a Model

A property checks one action. But a queue is something you change step by
step: put, take, reject, take again, close for the night. Bugs often hide
in the *order* of steps.

So the inspector has a second trick. They carry a notebook and write down
every letter by hand: one list for the tray, one for letters out with a
postman, one for the dead-letter shelf. After every single step at the
counter, they compare the post office with the notebook. If the two ever
disagree, something's wrong with the post office.

That notebook is a **model**: something simple that we already trust.
It checks the classic queues. (The broker's work queue has no model check:
it only runs inside a live event loop, and Hypothesis plays a story one
step at a time from ordinary code, so it gets the "every message in
exactly one place" property instead.) Here's part of the check in
`tests/test_model.py`:

```python
class QueueMachine(RuleBasedStateMachine):
    messages = Bundle("messages")

    def __init__(self) -> None:
        super().__init__()
        self.queue: BaseMessageQueue = MessageQueue("orders")
        self.kind: type[BaseMessageQueue] = MessageQueue
        self.max_retries = 0
        self.line: list[Saved] = []  # waiting, in the order they arrived
        self.out: list[Saved] = []  # handed out, not yet acknowledged
        self.dead: list[Saved] = []  # the dead-letter shelf, oldest first

    def in_delivery_order(self) -> list[Saved]:
        if isinstance(self.queue, PriorityMessageQueue):
            return sorted(self.line, key=by_priority)
        return list(self.line)

    @rule(target=messages, body=message_bodies(), priority=priorities())
    def put(self, body: str, priority: int) -> Message:
        return self.check_put(body, priority)

    @rule()
    def get_dead_letter(self) -> None:
        msg = self.queue.get_dead_letter()
        if not self.dead:
            assert msg is None
        else:
            assert asdict(msg) == self.dead.pop(0)

    @invariant()
    def same_messages_in_the_same_order(self) -> None:
        assert self.queue.to_dict() == {
            "name": "orders",
            "kind": self.kind.kind,
            "max_retries": self.max_retries,
            "waiting": self.out + self.in_delivery_order(),
            "dead_letters": self.dead,
        }
QueueMachine.TestCase.settings = settings(stateful_step_count=20)
TestQueueModel = cast("type[unittest.TestCase]", QueueMachine.TestCase)
```

Piece by piece:

- **`RuleBasedStateMachine`** is Hypothesis's tool for testing things
  that change step by step. Hypothesis makes up a random **story** of
  steps and plays it.
- **`self.queue` and the three lists** are the post office and the
  notebook. Each story opens a queue of a random kind, with a random retry
  limit (`self.max_retries`, from 0 to 3) and a few messages already
  waiting.
- **`in_delivery_order()`** is the notebook's whole idea of ordering: a
  FIFO queue hands out the front of the line, a priority queue hands out
  `sorted(self.line, key=by_priority)[0]`. No heap, no counter.
- **Each `@rule`** is one kind of step. It does the same thing to the
  queue and to the notebook. The real file has rules to put, take, take
  and hand straight back, acknowledge, reject, settle a made-up message,
  scribble on a message we were handed (the queue must not notice), take a
  dead letter, and close for the night (save and load).
- **`messages = Bundle("messages")`** is a bag of messages from earlier
  in the story. `put` drops its message in the bag (`target=messages`),
  and a later rule pulls one out to acknowledge or reject it. (A second
  bag, `taken`, holds what `get` handed out.) Without the bags, a story
  could only reject messages it made up on the spot, which the queue
  never handed out.
- **`get_dead_letter`** checks what the queue *says*, not just what it
  holds: the oldest dead letter, or `None` when the shelf is empty.
- **The `@invariant()`** runs after every single step and compares
  everything: the size, and the whole `to_dict()` -- name, kind, retry
  limit, every waiting message in order (the ones out with a postman
  first, as `to_dict()` promises), and every dead letter.
- **`TestQueueModel = ...TestCase`** turns the machine into a test that
  pytest can find and run. One run plays 100 random stories, each up to
  20 steps long (the `settings` line; Hypothesis's default is 50, which
  finds the same bugs but takes most of the run time).

The lists are a good model because they're obviously right and they're
*not* PyMQ's code. If we built the model by copying the queue's heap and
counter, any bug would be in both, and they'd agree perfectly about the
wrong answer.

## Try It Yourself

Let's break the loader on purpose and watch the inspector find it.

Open `src/pymq/persistence.py`, find `_messages_from`, and change one
line, as if you'd forgotten that a saved message might not be an object
at all:

```python
        fields = cast("dict[str, object]", item)  # was: fields = cast("dict[str, object]", item) if isinstance(item, dict) else {}
```

Run the property tests:

```bash
uv run pytest tests/test_properties.py tests/test_model.py --no-cov
```

Hypothesis reports this (trimmed to the interesting part):

```
    @normal_run_seed(DAMAGED_FILE_SEED)
>   @given(document=damaged_save_files())

>   if not all(isinstance(fields.get(key), str) for key in _TEXT_FIELDS) or not all(
        _is_int(fields.get(key)) for key in _NUMBER_FIELDS
    ):
E   AttributeError: 'NoneType' object has no attribute 'get'
E   Failing test case: test_damaged_save_file_loads_or_raises_persistence_error(
E       document={'queues': [{'name': '',
E          'kind': 'fifo',
E          'max_retries': 0,
E          'waiting': [None],
E          'dead_letters': []}]},
E   )
```

Look at how small that is. Hypothesis made real save files full of
messages, then damaged one field at a time, and shrank the failure all
the way down to **an empty queue whose waiting line holds one `null`**.
The loader asked that `None` for its `id`, and `None` has no fields to
ask about, so it crashed with an `AttributeError` instead of politely
refusing the file with a `PersistenceError`.

It's `test_damaged_save_file_loads_or_raises_persistence_error` that
catches it. The two other junk properties stay green: random bytes and
random JSON almost never look enough like a save file to get as far as
the message list. The round-trip and the model check stay green too,
because a file PyMQ wrote itself never has a `null` in the line.

The example tests all still pass. Only the property notices. They try a
message with missing fields, but never a message that isn't an object.

Put the line back and run the tests again. They pass.

!!! tip "Older Hypothesis versions"
    Older versions of Hypothesis print `Falsifying example:` instead of
    `Failing test case:`. It means the same thing.

## A Bug We Found

When we first wrote the model check, it failed on PyMQ's real code -- no
deliberate bug needed:

```
E   AssertionError: assert 1 == 0
E    +  where 1 = <pymq.queue.MessageQueue object at 0x...>.size
E   Failing test case: <state machine>
E   state = QueueMachine()
E   (messages_0,) = state.open_queue(kind=pymq.queue.MessageQueue, max_retries=1, first=[(
E        '',  # or any other generated value
E        0,  # or any other generated value
E    )])
E   state.same_messages_in_the_same_order()
E   state.close_for_the_night()
E   state.same_messages_in_the_same_order()
E   state.get_and_reject()
E   state.same_messages_in_the_same_order()
E   taken_0 = state.get()
E   state.same_messages_in_the_same_order()
E   state.settle_any_message(msg=messages_0, reject=True)
E   state.same_messages_in_the_same_order()
E   state.teardown()
```

Read the story like a script. One message, a retry limit of 1. Close for
the night. Take the message and hand it back (retry 1). Take it again,
then reject it using `messages_0`, the copy from *before* the restart.
That's retry 2, more than the limit, so the notebook put it on the
dead-letter shelf. The queue put it back in line.

Here's why. `reject` found the message by its id, then incremented and
requeued the object *you* handed it. After a restart, the loaded queue
holds a fresh copy, so the old copy, still saying "retry 0", went back in
line. Rejected that way after every restart, a message could dodge the
dead-letter shelf forever. The example tests always rejected the exact object `get` had
just returned, where the two copies are one and the same.

The fix makes `reject` use the queue's own copy:

```python
        own = self._in_flight.pop(msg.id, None)  # was: if self._in_flight.pop(msg.id, None) is None:
```

followed by `if own is None: return`, and `own` (not `msg`) gets the
extra retry and goes back in line. The story is now an example test in
`tests/test_queue.py`:

```python
    def test_reject_with_a_handle_from_before_a_restart(self, tmp_path: Path) -> None:
        """Rejecting with a copy from before a restart still counts against the queue's message.

        Found by the model check, which shrank its failure to this story. The
        queue used to put the *caller's* old copy back in line, with its old
        retry count, so the message dodged the dead-letter queue.
        """
        path = tmp_path / "queues.json"
        q = MessageQueue("orders", max_retries=1)
        old = q.put("")
        save_queues([q], path)
        q = load_queues(path)["orders"]
        msg = q.get()
        assert msg is not None
        q.reject(msg)
        assert q.get() is not None
        q.reject(old)
        assert q.size == 0
        assert q.dead_letter_count == 1
```

A reviewer then spotted a cousin of this bug that our tests had missed:
`put` and `get` handed out the queue's own message, so setting
`msg.retries = 0` before each reject also dodged the dead letters. Now
they hand out copies, and the model has a rule that scribbles on them.

The round-trip found another bug: a queue made with `max_retries=-1`
saved fine, but `load_queues` refused the file it had just written. Now
the queue refuses a negative limit when it's made. And by reading the
loader, we found that a number over 4,300 digits or lists nested 100,000
deep crashed it with the wrong error; random inputs never get that big,
so those two are pinned as example tests.

Later, a reviewer pasted one message into a save file twice, the way a
hand-edit might. `load_queues` took it, but the queue keeps a taken
message by its `id`, so taking both copies and saving again quietly lost
one. Now the loader refuses two messages with one `id` in a queue. The
damaged-file property used to check only *which error* came out, so it
could never see a lost message. Now it also pastes a message (or a queue)
in twice, and when a file loads it takes every message, saves again, and
counts them.

## Run It Yourself

Run just the property tests and the model check (100 examples each, the
same every time):

```bash
uv run pytest tests/test_properties.py tests/test_model.py --no-cov
```

The `--no-cov` turns off the coverage check, which expects the *whole*
test suite to run and would fail on two files.

Run them the long way (2,000 random examples each, different every run)
to hunt for rarer bugs. It takes about 3 and a half minutes:

```bash
uv run pytest tests/test_properties.py tests/test_model.py --no-cov --hypothesis-profile=thorough
```

The normal run uses a fixed seed, so it tries the same inputs every time
and a passing build means the same thing on every computer. The thorough
run is random, so each time you run it the inspector brings a brand-new
sack of mail. Both settings live in `conftest.py` at the top of the
project.

## Your Turn

Write one new property for PyMQ. Think about `PriorityMessageQueue`. If
you put in any pile of messages and then take them all out, what must be
true of the priorities you get back? What's the strategy that makes the
pile? What's the `assert`? Run it and watch it pass. Then break `get` on
purpose and watch the inspector find it.
