# PyMQ Network Broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn PyMQ into a networked, async message broker on PyNet's own event loop (work queues with leases/retries/dead letters, wildcard topics, bindings, a reconnecting client), keeping the old synchronous classes, with kid-friendly docs and a PyStack demo.

**Architecture:** Three layers: a pure in-process `Broker` core (queues, router, timers on `pynet.loop`, no sockets), a `BrokerServer` exposing it over `pynet.messaging`, and a `BrokerClient` wrapping `pynet.messaging.ReconnectingClient` that replays its setup after every reconnect.

**Tech Stack:** Python 3.14, `pynet.loop` (own event loop), `pynet.messaging` (Peer, serve, ReconnectingClient), uv, ruff, pyright strict, pytest + pytest-cov + pytest-timeout.

**Spec:** `docs/superpowers/specs/2026-10-17-pymq-network-broker-design.md`

## Global Constraints

- Python `>=3.14`; never `TYPE_CHECKING`, never `from __future__ import annotations`.
- Every public module/class/function/method and every test class/method has a Google-style docstring in imperative mood (repo CLAUDE.md).
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright src tests` (strict) all clean; `uv run pytest` passes with coverage ≥ 80 %; named constants instead of magic numbers in tests (PLR2004).
- Per-test timeout 10 s: add `pytest-timeout>=2.3.1` to dev deps and `"--timeout=10"` to pytest `addopts`.
- Conventional commits; every commit message ends with a `Co-Authored-By:` trailer naming the model that wrote it.
- Runs on `pynet.loop`, never asyncio. Time-based tests use `pynet.loop.testing.FakeClock(autojump=True)` via `pynet.loop.run(main(), clock=clock)`; no test sleeps real seconds.
- Existing `pymq.message`, `pymq.queue`, `pymq.pubsub` and their tests stay unchanged.
- No file under `src/pymq/broker/` passes ~400 lines.
- Every docs snippet is actually run before committing (paste it into `uv run python -` and check the printed output matches what the page claims).
- Kid-friendly docs: each concept page opens with an everyday analogy, then a runnable snippet, then "What We Test".
- Defaults (spec, verbatim): `ack_timeout` 30 s, `max_attempts` 5, `retry_delay` 1 s, `max_retry_delay` 60 s, `max_length` 10 000, `message_ttl` None, `max_dead_letters` 1 000, `dedupe_window` 300 s, consumer `prefetch` 10, subscription `max_pending` 1 000, client `put_timeout` 30 s, server default host `127.0.0.1`.

## Review Focus

1. **A consumer handler that never returns** (hung job): its lease must still expire after `ack_timeout`, the message must go to another consumer, and the late ack must come back `stale` — Task 4 test `test_lease_expiry_redelivers_and_late_ack_is_stale`.
2. **Ack for a delivery from before a reconnect**: must be ignored without raising and without acking the redelivered copy — Task 9 test `test_ack_from_old_connection_is_stale`.
3. **A subscriber that stops reading its socket** while another subscriber and a consumer keep working: the others must not stall, and drops must be counted — Task 7 test `test_stalled_subscriber_does_not_block_others`.
4. **Queue name / topic with odd characters** (`"jobs/../x"`, `""`, `"a..b"`, `"#.x"` as a *topic*): rejected with `InvalidNameError` / `InvalidTopicError` at the client *and* the server, never a crash — Tasks 1, 2, 6.
5. **Broker restart while a `put` is in flight**: the client resends the same id once the broker is back; with the same broker instance surviving (connection drop only) the queue holds exactly one copy — Task 9 test `test_put_resent_after_drop_is_not_duplicated`.

---

## File Structure

```
src/pymq/broker/
  __init__.py      public re-exports
  errors.py        BrokerError hierarchy (Task 1)
  model.py         QueueConfig, Envelope, LeasedMessage, DeadLetter, DeadReason,
                   PutResult, QueueStats, validate_queue_name (Task 1)
  topics.py        validate_topic, validate_pattern, topic_matches (Task 2)
  workqueue.py     WorkQueue: storage, dedupe, TTL (Task 3) + consumers, leases (Task 4)
  router.py        TopicRouter, TopicEvent (Task 5)
  core.py          Broker (Task 5)
  protocol.py      method names, JSON codecs, error-code mapping (Task 6)
  outbox.py        ConnectionOutbox (Task 7)
  server.py        BrokerServer (Task 7)
  client.py        BrokerClient, Delivery, Consumer, Subscription (Tasks 8–9)
  __main__.py      CLI (Task 10)
tests/broker/      one test module per source module
docs/concepts/     new pages (listed per task)
examples/          broker_demo.py, work_queue.py, topic_alerts.py (Task 10)
```

---

### Task 1: Scaffolding, errors and the data model

**Files:**
- Modify: `pyproject.toml`, `.github/workflows/ci.yml`
- Create: `src/pymq/broker/__init__.py`, `src/pymq/broker/errors.py`, `src/pymq/broker/model.py`
- Test: `tests/broker/__init__.py`, `tests/broker/test_model.py`

**Interfaces:**
- Produces:
  - `errors.py`: `BrokerError(Exception)`; subclasses `QueueNotFoundError`, `QueueFullError`, `QueueConfigConflictError`, `InvalidNameError`, `InvalidTopicError`, `InvalidRequestError`, `BrokerUnavailableError`. Each has a class attribute `code: str`: `"queue_not_found"`, `"queue_full"`, `"config_conflict"`, `"invalid_name"`, `"invalid_topic"`, `"invalid_request"`, `"broker_unavailable"`; `BrokerError.code = "broker_error"`.
  - `model.py`:
    - `type JsonValue` — re-export `pynet.messaging.JsonValue`.
    - `@dataclass(frozen=True) QueueConfig(ack_timeout: float = 30.0, max_attempts: int = 5, retry_delay: float = 1.0, max_retry_delay: float = 60.0, max_length: int = 10_000, message_ttl: float | None = None, max_dead_letters: int = 1_000, dedupe_window: float = 300.0)`; `__post_init__` raises `ValueError` unless: floats finite and > 0 (`message_ttl` may be `None`), ints are `int` but not `bool` and ≥ 1, and `max_retry_delay >= retry_delay`. Method `retry_wait(attempt: int) -> float` = `min(retry_delay * 2 ** (attempt - 1), max_retry_delay)`.
    - `@dataclass(frozen=True) Envelope(id: str, queue: str, body: JsonValue, headers: Mapping[str, str], attachments: tuple[bytes, ...], priority: int, attempt: int, enqueued_at: str)`.
    - `@dataclass(frozen=True) LeasedMessage(queue: str, consumer_tag: str, delivery_tag: int, envelope: Envelope)`.
    - `class DeadReason(StrEnum)`: `MAX_ATTEMPTS = "max_attempts"`, `REJECTED = "rejected"`, `TTL_EXPIRED = "ttl_expired"`.
    - `@dataclass(frozen=True) DeadLetter(envelope: Envelope, reason: DeadReason, dead_at: str, error: str | None)`.
    - `@dataclass(frozen=True) PutResult(id: str, duplicate: bool)`.
    - `@dataclass(frozen=True) QueueStats(name: str, ready: int, delayed: int, in_flight: int, dead: int, consumers: int, oldest_ready_age: float | None, enqueued: int, delivered: int, acked: int, retried: int, dead_lettered: int, dead_dropped: int, duplicates: int)`.
    - `validate_queue_name(name: str) -> str` — returns `name` if it matches `^[A-Za-z0-9._-]{1,200}$` and contains no `..`, else raises `InvalidNameError`.
    - `utc_now_iso() -> str` — `datetime.now(tz=UTC).isoformat()`.

- [ ] **Step 1: Dependencies.** In `pyproject.toml` add `"pynet"` to `[project.dependencies]`, a `[tool.uv.sources]` entry `pynet = { path = "../pynet", editable = true }`, `"pytest-timeout>=2.3.1"` to the `dev` extra, and `"--timeout=10"` to pytest `addopts`. Run `uv sync --all-extras`; expected: pynet installed. In `.github/workflows/ci.yml`, add to the lint and test jobs (before "Install dependencies") a step `Clone pynet` running `cd .. && git clone --depth 1 https://github.com/robcox2910/pynet.git pynet` (security job too, since it runs `uv sync`).

- [ ] **Step 2: Write failing tests** in `tests/broker/test_model.py`:

```python
class TestQueueConfig:
    def test_defaults_match_spec(self) -> None:
        c = QueueConfig()
        assert (c.ack_timeout, c.max_attempts, c.retry_delay, c.max_retry_delay) == (30.0, 5, 1.0, 60.0)
        assert (c.max_length, c.message_ttl, c.max_dead_letters, c.dedupe_window) == (10_000, None, 1_000, 300.0)

    @pytest.mark.parametrize("kwargs", [
        {"ack_timeout": 0}, {"ack_timeout": math.nan}, {"ack_timeout": math.inf},
        {"max_attempts": 0}, {"max_attempts": True}, {"max_attempts": 1.5},
        {"retry_delay": -1}, {"retry_delay": 10.0, "max_retry_delay": 5.0},
        {"max_length": 0}, {"message_ttl": 0}, {"max_dead_letters": 0}, {"dedupe_window": 0},
    ])
    def test_bad_values_rejected(self, kwargs) -> None:
        with pytest.raises(ValueError):
            QueueConfig(**kwargs)

    def test_retry_wait_doubles_then_caps(self) -> None:
        c = QueueConfig(retry_delay=1.0, max_retry_delay=5.0)
        assert [c.retry_wait(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 5.0]

class TestQueueNames:
    @pytest.mark.parametrize("name", ["jobs", "job.q-1_x", "a" * 200])
    def test_good_names(self, name) -> None: assert validate_queue_name(name) == name
    @pytest.mark.parametrize("name", ["", "a" * 201, "jobs/../x", "a..b", "sp ace", "ünï"])
    def test_bad_names(self, name) -> None:
        with pytest.raises(InvalidNameError): validate_queue_name(name)

class TestErrors:
    def test_codes(self) -> None:
        assert QueueFullError.code == "queue_full" and issubclass(QueueFullError, BrokerError)
```

(Type the parametrised arguments properly; the above is shorthand.)

- [ ] **Step 3: Run** `uv run pytest tests/broker/test_model.py -v` — expected FAIL (import error).
- [ ] **Step 4: Implement** `errors.py`, `model.py`, and `__init__.py` re-exporting every name listed above.
- [ ] **Step 5: Run** the tests — PASS; then the full gates (ruff, format, pyright, pytest).
- [ ] **Step 6: Commit** `feat(broker): add data model, errors and pynet dependency`.

---

### Task 2: Topics and wildcards

**Files:**
- Create: `src/pymq/broker/topics.py`, `docs/concepts/topics-and-wildcards.md`
- Test: `tests/broker/test_topics.py`

**Interfaces:**
- Produces: `validate_topic(topic: str) -> str`, `validate_pattern(pattern: str) -> str` (both raise `InvalidTopicError`), `topic_matches(pattern: str, topic: str) -> bool` (assumes both already valid). `SYSTEM_PREFIX = "$sys"`.

Rules: words separated by `.`; 1–255 chars total; each word non-empty and matching `[A-Za-z0-9_-]+`, except the first word may start with `$`. A pattern word may also be exactly `*` (one word) or `#` (zero or more words); `*`/`#` mixed with other characters in a word (`a*`) is invalid; topics may not contain `*` or `#`.

- [ ] **Step 1: Failing tests** — a table test of `(pattern, topic, expected)` including at least:

```python
("job.*.failed", "job.nightly.failed", True), ("job.*.failed", "job.a.b.failed", False),
("job.#", "job", True), ("job.#", "job.a.b", True), ("#", "anything.at.all", True),
("#.failed", "failed", True), ("job.#.failed", "job.failed", True),
("job.#.failed", "job.x.y.failed", True), ("job.*", "job", False),
("$sys.dead.*", "$sys.dead.jobs", True), ("#", "$sys.dead.jobs", True),
```

plus invalid inputs: topics `"", "a..b", ".a", "a.", "a.*", "a.#", "a b", "x" * 256`; patterns `"a*", "#x", "a..b"`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** `topic_matches` splits both on `.` and matches word lists with a memoised or iterative algorithm (no exponential backtracking on many `#`s: test `"#.#.#.#.#.#.#.#.#.#.z"` against 50 `a` words finishes instantly).
- [ ] **Step 4: Docs** `docs/concepts/topics-and-wildcards.md` — postcodes and sorting machines analogy; `*` vs `#` with a table; system topics.
- [ ] **Step 5: Run** gates — PASS. **Commit** `feat(broker): add topic names and wildcard matching`.

---

### Task 3: WorkQueue storage — put, priority, delay, dedupe, TTL, stats

**Files:**
- Create: `src/pymq/broker/workqueue.py`, `docs/concepts/idempotency.md`
- Test: `tests/broker/test_workqueue_storage.py`

**Interfaces:**
- Consumes: Task 1 model/errors.
- Produces (`class WorkQueue`, this task's part):
  - `__init__(self, name: str, config: QueueConfig, *, on_dead: Callable[[DeadLetter], None] | None = None) -> None` — uses `get_running_loop()` for `time()` and `call_at`; must be created inside a running loop.
  - `name: str`, `config: QueueConfig` (read-only properties).
  - `put(self, body: JsonValue, *, id: str | None = None, priority: int = 0, headers: Mapping[str, str] | None = None, attachments: Sequence[bytes] = (), delay: float = 0.0) -> PutResult` — `id` defaults to `uuid4().hex`; raises `QueueFullError` if ready+delayed ≥ `max_length` (a duplicate id never raises); `ValueError` for a negative/non-finite delay.
  - `purge(self) -> int` — drop ready+delayed, return count.
  - `stats(self) -> QueueStats`.
  - `dead_letters(self, limit: int | None = None) -> list[DeadLetter]` (oldest first), `replay_dead(self, ids: Collection[str] | None = None) -> int`, `purge_dead(self) -> int`.
  - `close(self) -> None` — cancel every timer; the queue is unusable after (further calls raise `QueueNotFoundError`).
  - Private hook for Task 4: `_dispatch()` is called whenever a message becomes ready (in this task it does nothing).

Behaviour pinned here:
- Ready order: lowest `priority` first, then put order (FIFO tie-break).
- Delayed messages become ready at `put time + delay` via one loop timer for the earliest due message.
- Dedupe: an id seen within `dedupe_window` seconds → `PutResult(id, duplicate=True)`, nothing added, `duplicates += 1`. Ids expire from memory after the window.
- TTL: a message's deadline is when it last entered ready/delayed (put, retry, replay) + `message_ttl`; on expiry → dead letter `TTL_EXPIRED`. A leased message never expires by TTL.
- Dead letters: appended with `dead_at=utc_now_iso()`; beyond `max_dead_letters` the oldest is dropped and `dead_dropped += 1`; `on_dead(letter)` called for every dead letter (exceptions from `on_dead` are caught and logged, never propagate).
- `replay_dead` puts letters back to ready with `attempt` reset so the next delivery is attempt 1; ignores `max_length`; unknown ids are skipped. `None` = all.
- `oldest_ready_age` = now − the time the oldest *ready* message became ready, or `None`.

- [ ] **Step 1: Failing tests** (all inside `run(main(), clock=FakeClock(autojump=True))` via a small helper `run_async(fn)` in `tests/broker/conftest.py` or a helper module), at least:
  - `test_priority_then_fifo` (peek order via a test-only accessor or via stats after Task 4 — prefer exposing `ready_ids() -> list[str]` as a documented introspection method).
  - `test_delay_moves_to_ready_on_time` — `put(delay=5)`; at t=4.9 stats `delayed == 1, ready == 0`; at t=5 `ready == 1`.
  - `test_duplicate_id_within_window_ignored`, `test_duplicate_id_after_window_accepted` (window 300 → advance 301).
  - `test_max_length_raises_queue_full` (`max_length=2`, third put raises; delayed counts toward the limit).
  - `test_ttl_dead_letters_with_reason` (ttl 10, wait 10 → `dead_letters()[0].reason is DeadReason.TTL_EXPIRED`, `on_dead` called once).
  - `test_dead_letter_cap_drops_oldest` (`max_dead_letters=2`, three expire → ids of the last two kept, `dead_dropped == 1`).
  - `test_replay_and_purge_dead`, `test_purge_ready`, `test_oldest_ready_age`, `test_on_dead_exception_is_swallowed`, `test_bad_delay_rejected` (`-1`, `nan`).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** with a ready heap `(priority, seq, message_id)`, a delayed heap `(due, seq, message_id)`, a TTL heap `(deadline, seq, message_id)` with lazy deletion (each stored record has a generation counter; stale heap entries are skipped), and an id→expiry dict + FIFO deque for dedupe pruning. One `TimerHandle` per heap, re-armed for the earliest live entry.
- [ ] **Step 4: Docs** `docs/concepts/idempotency.md` — raffle ticket numbers; why a resend with the same id is safe.
- [ ] **Step 5: Run** gates — PASS. **Commit** `feat(broker): add work queue storage with delays, dedupe and TTL`.

---

### Task 4: WorkQueue consumers — dispatch, leases, ack/nack/touch, retries

**Files:**
- Modify: `src/pymq/broker/workqueue.py`
- Create: `docs/concepts/leases-and-acks.md`, `docs/concepts/retries-and-delays.md`, `docs/concepts/dead-letters-v2.md`, `docs/concepts/fair-dispatch.md`
- Test: `tests/broker/test_workqueue_consumers.py`

**Interfaces:**
- Consumes: Task 3 `WorkQueue`.
- Produces (rest of `WorkQueue`):
  - `type DeliverySink = Callable[[LeasedMessage], None]` (in `workqueue.py`, re-exported) — must not block; exceptions from a sink are caught, logged, and the message is nacked (requeue) immediately.
  - `add_consumer(self, consumer_tag: str, sink: DeliverySink, prefetch: int = 10) -> None` — `ValueError` for prefetch < 1 or a duplicate tag.
  - `remove_consumer(self, consumer_tag: str) -> int` — its leases are nacked (requeue, counts as an attempt); returns how many. Unknown tag → 0.
  - `ack(self, delivery_tag: int) -> bool`, `nack(self, delivery_tag: int, *, requeue: bool = True, error: str | None = None) -> bool`, `touch(self, delivery_tag: int) -> bool` — `False` means stale (unknown, expired, already settled).
  - `consumer_count` property.

Behaviour pinned here:
- Dispatch: while a ready message exists and some consumer has `in_flight < prefetch`, hand the next ready message to the next eligible consumer in round-robin order (starting after the last one served). The delivered `Envelope.attempt` is the delivery count (1, 2, …). Delivery tags are per-queue integers starting at 1, never reused.
- Lease: `ack_timeout` after delivery (or after the latest `touch`), an unsettled delivery is treated as `nack(requeue=True, error="ack timeout")`.
- `nack(requeue=True)`: if `attempt >= max_attempts` → dead letter `MAX_ATTEMPTS` (with `error`); else delayed for `config.retry_wait(attempt)`, `retried += 1`.
- `nack(requeue=False)` → dead letter `REJECTED`.
- Totals: `delivered` per delivery, `acked` per ack.
- `close()` (Task 3) now also drops consumers without requeueing.

- [ ] **Step 1: Failing tests**, at least:
  - `test_round_robin_across_consumers` (two consumers, prefetch 10, four puts → deliveries alternate A, B, A, B).
  - `test_prefetch_limits_in_flight` (prefetch 2, five puts → 2 delivered; one ack → a third).
  - `test_ack_removes_message`; `test_double_ack_is_stale`.
  - `test_nack_backs_off_then_dead_letters` (`retry_delay=1, max_attempts=3`: attempts redelivered at +1 s then +2 s; third nack → dead `MAX_ATTEMPTS`, error kept).
  - `test_nack_without_requeue_dead_letters_as_rejected`.
  - `test_lease_expiry_redelivers_and_late_ack_is_stale` (Review Focus 1: consumer A never acks; after `ack_timeout` B receives attempt 2; A's ack returns `False`; message is not removed).
  - `test_touch_extends_lease` (ack_timeout 10; touch at 8; still leased at 15; expires at 18).
  - `test_remove_consumer_returns_leases_as_attempts`.
  - `test_sink_exception_requeues`.
  - `test_conservation_under_random_operations` — seeded `random.Random(1234)`, 500 steps of put / deliver-and-randomly ack-nack-ignore / advance time; at the end each put id is in exactly one of {acked set, dead letters, ready/delayed/in-flight}.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** Leases in a dict `delivery_tag → lease` plus a lease-deadline heap with one timer (same lazy pattern as Task 3). Keep `workqueue.py` under ~400 lines; if it grows past that, move the heap+timer helper into `src/pymq/broker/timers.py` (`class TimerHeap[T]`) and use it for all four heaps.
- [ ] **Step 4: Docs** — four pages (library book due dates; snooze button with a doubling table; lost-property office that writes down why, replay/purge; buffet plates and prefetch). `leases-and-acks.md` must say plainly: at-least-once means a handler may see the same message twice, so make handlers safe to repeat.
- [ ] **Step 5: Run** gates — PASS. **Commit** `feat(broker): add consumers, leases, acks and retries`.

---

### Task 5: Topic router and the Broker core

**Files:**
- Create: `src/pymq/broker/router.py`, `src/pymq/broker/core.py`, `docs/concepts/bindings.md`, `docs/concepts/broker.md`
- Test: `tests/broker/test_router.py`, `tests/broker/test_core.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  - `router.py`: `@dataclass(frozen=True) TopicEvent(topic: str, body: JsonValue, headers: Mapping[str, str], attachments: tuple[bytes, ...], published_at: str)`; `type EventSink = Callable[[str, TopicEvent], None]` (called with the subscription id). `class TopicRouter`: `subscribe(pattern: str, sink: EventSink) -> str` (ids `"sub-1"`, `"sub-2"`, …), `unsubscribe(sub_id: str) -> bool`, `bind(pattern: str, queue: str) -> None` (idempotent), `unbind(pattern: str, queue: str) -> bool`, `unbind_queue(queue: str) -> int`, `route(topic: str) -> tuple[list[tuple[str, EventSink]], list[str]]`, `subscription_count`, `bindings() -> list[tuple[str, str]]`.
  - `core.py`: `@dataclass(frozen=True) PublishResult(subscribers: int, queues: int)`; `@dataclass(frozen=True) BrokerStats(queues: int, subscriptions: int, bindings: int, binding_drops: int)`. `class Broker` (create inside a running loop):
    - `declare_queue(name: str, config: QueueConfig | None = None) -> QueueConfig` — idempotent for an equal config; different → `QueueConfigConflictError`; validates name.
    - `delete_queue(name) -> None` (also unbinds it and drops its consumers), `purge_queue(name) -> int`, `list_queues() -> list[str]` (sorted), `stats(name) -> QueueStats`, `broker_stats() -> BrokerStats`.
    - `put(queue, body, **same kwargs as WorkQueue.put) -> PutResult`.
    - `consume(queue, sink: DeliverySink, *, prefetch: int = 10) -> str` (consumer tags `"ctag-1"`, … unique broker-wide), `cancel(consumer_tag) -> int` (leases returned).
    - `ack(queue, delivery_tag) -> bool`, `nack(queue, delivery_tag, *, requeue=True, error=None) -> bool`, `touch(queue, delivery_tag) -> bool`.
    - `publish(topic, body, *, headers=None, attachments=()) -> PublishResult`, `subscribe(pattern, sink) -> str`, `unsubscribe(sub_id) -> bool`, `bind(pattern, queue) -> None` (queue must exist), `unbind(pattern, queue) -> bool`.
    - `dead_letters(queue, limit=None)`, `replay_dead(queue, ids=None)`, `purge_dead(queue)`.
    - `close() -> None`.
    - Unknown queue anywhere → `QueueNotFoundError`.

Behaviour pinned here:
- `publish` validates the topic, calls each matching sink (exceptions caught+logged; one failing sink never stops the rest), and `put`s a copy into each bound queue with `headers` plus `{"topic": topic}`; a `QueueFullError` on a bound queue increments `binding_drops` instead of raising.
- Every queue's `on_dead` publishes topic `$sys.dead.<queue>` with body `{"queue", "id", "reason", "error", "attempt", "dead_at"}`.

- [ ] **Step 1: Failing tests**: router table (`route` returns every matching sub and bound queue once even if two bindings for the same queue match); core: declare idempotent / conflict; unknown queue errors for each method; publish fan-out to two subs + a bound queue; full bound queue counted in `binding_drops`; a raising sink doesn't stop the others; `$sys.dead.jobs` event arrives with the reason after a rejected nack; `delete_queue` removes bindings.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.**
- [ ] **Step 4: Docs** — `bindings.md` (a PO box holding your post on holiday); `broker.md` (post office sorting room; the three layers; "the broker forgets everything when it restarts — the scheduler keeps the real records"), with a runnable all-local snippet using `Broker` directly.
- [ ] **Step 5: Gates**, **Commit** `feat(broker): add topic router and broker core`.

---

### Task 6: Wire protocol

**Files:**
- Create: `src/pymq/broker/protocol.py`
- Test: `tests/broker/test_protocol.py`

**Interfaces:**
- Consumes: Task 1 model, Task 5 `TopicEvent`, `PublishResult`, `BrokerStats`.
- Produces:
  - Constants for every request/push name in the spec: `QUEUE_DECLARE = "queue.declare"`, `QUEUE_DELETE`, `QUEUE_PURGE`, `QUEUE_STATS`, `QUEUE_LIST`, `BROKER_STATS = "broker.stats"`, `MSG_PUT = "msg.put"`, `MSG_ACK`, `MSG_NACK`, `MSG_TOUCH`, `CONSUME_START = "consume.start"`, `CONSUME_CANCEL`, `TOPIC_PUBLISH = "topic.publish"`, `TOPIC_SUBSCRIBE`, `TOPIC_UNSUBSCRIBE`, `TOPIC_BIND`, `TOPIC_UNBIND`, `DEAD_LIST = "dead.list"`, `DEAD_REPLAY`, `DEAD_PURGE`, `PUSH_DELIVER = "deliver"`, `PUSH_EVENT = "event"`.
  - `config_to_json(c: QueueConfig) -> dict[str, JsonValue]` / `config_from_json(v: JsonValue) -> QueueConfig`; `envelope_to_json(e: Envelope) -> dict[str, JsonValue]` (no attachments — they travel as pynet attachments) / `envelope_from_json(v: JsonValue, attachments: Sequence[bytes]) -> Envelope`; same pairs for `QueueStats`, `DeadLetter` (attachments dropped from listings — `dead.list` returns envelopes without attachment bytes but with `"attachments": <count>`; replay keeps the real bytes server-side), `TopicEvent`, `BrokerStats`.
  - Field readers `req_str(body, key) -> str`, `req_int(body, key) -> int`, `opt_str`, `opt_float`, `opt_int`, `opt_bool`, `opt_headers(body, key) -> dict[str, str]`, `opt_str_list` — every `*_from_json` and reader raises `InvalidRequestError` (never `KeyError`/`TypeError`) for a missing or wrongly typed field; booleans are not ints.
  - `error_to_remote(err: BrokerError | ValueError) -> RemoteError` (ValueError → `invalid_request`) and `remote_to_error(err: RemoteError) -> BrokerError | RemoteError` (known codes → the matching class with the remote message; unknown codes returned unchanged).

- [ ] **Step 1: Failing tests**: round trip of every type; each reader rejects missing/wrong-type/`bool`-as-int with `InvalidRequestError`; `remote_to_error(RemoteError("queue_full", "x"))` is a `QueueFullError` whose message contains `"x"`; unknown code passes through.
- [ ] **Step 2–3: Run, implement. Step 4: Gates. Commit** `feat(broker): add wire protocol codecs`.

---

### Task 7: Outbox and BrokerServer

**Files:**
- Create: `src/pymq/broker/outbox.py`, `src/pymq/broker/server.py`, `docs/concepts/slow-subscribers.md`
- Test: `tests/broker/test_outbox.py`, `tests/broker/test_server.py`

**Interfaces:**
- Consumes: Tasks 5–6; `pynet.messaging.serve`, `Peer`, `Reply`, `RemoteError`.
- Produces:
  - `class ConnectionOutbox(peer: Peer, *, max_pending: int = 1_000)`: `send_delivery(leased: LeasedMessage) -> None` (never dropped), `send_event(sub_id: str, event: TopicEvent) -> None` (per-subscription deque; when that subscription already holds `max_pending`, drop its oldest and count), `dropped(sub_id) -> int`, `forget(sub_id) -> None`, `start() -> None`, `close() -> None`. One drain task per connection pushes `PUSH_DELIVER` / `PUSH_EVENT` with `await peer.push(...)`, deliveries before events, events round-robin across subscriptions. All sends are non-blocking for the caller.
  - Push bodies: `deliver` → `{"consumer_tag", "delivery_tag", "envelope": envelope_to_json(...)}` with the envelope's attachments as pynet attachments; `event` → `{"sub_id", "event": event_to_json(...)}` with attachments.
  - `class BrokerServer(broker: Broker | None = None, *, max_pending: int = 1_000, heartbeat_interval: float | None = 5.0)`: `async start(host: str = "127.0.0.1", port: int = 0) -> None`, `port: int` property, `broker: Broker` property, `connection_count: int`, `async close() -> None` (closes listener and every peer; does not close a broker passed in). Supports `async with`.
  - Request handlers map 1:1 onto `Broker` methods using `protocol` readers; return JSON bodies: `msg.put` → `{"id", "duplicate"}`; `msg.ack|nack|touch` → `{"stale": bool}`; `consume.start` (`queue`, optional `prefetch`) → `{"consumer_tag"}`; `topic.subscribe` (`pattern`, optional `max_pending`) → `{"sub_id"}`; `topic.publish` → `{"subscribers", "queues"}`; `queue.declare` (`name`, optional `config`) → `{"config"}`; `queue.stats` → stats JSON; `broker.stats` → broker stats JSON + `"connections"`; `dead.list` (`queue`, optional `limit`) → `{"letters": [...]}`; `dead.replay` (`queue`, optional `ids`) / `dead.purge` / `queue.purge` → `{"count"}`; others → `{"ok": true}`. `BrokerError`/`ValueError` → `error_to_remote`.
  - Consumer and subscription ownership: each connection remembers its consumer tags and sub ids; acks/nacks/touches are only honoured for consumer tags owned by that connection (else `stale: true`). When the peer closes: cancel its consumers (leases requeued as attempts), unsubscribe its subscriptions, close its outbox.

- [ ] **Step 1: Failing tests** (`test_outbox.py` with a fake peer whose `push` can be blocked by an `Event`; `test_server.py` over real localhost sockets using `pynet.messaging.connect` as a raw client):
  - outbox: deliveries never dropped; events drop-oldest with count; delivery jumps ahead of queued events.
  - server: declare/put/consume round trip yields a `deliver` push with the body and attachments; ack → `stale False`, second ack → `stale True`; unknown queue → `RemoteError` code `queue_not_found`; malformed body (`{"queue": 5}`) → `invalid_request`; invalid queue name → `invalid_name` (Review Focus 4); client disconnect requeues its leased message to a second connection with `attempt == 2`; ack from a connection that does not own the consumer → stale.
  - `test_stalled_subscriber_does_not_block_others` (Review Focus 3): connection S subscribes `#` and stops reading (pause its reader — e.g. a raw stream connection that never reads after subscribing, with `max_pending=5`); connection F subscribes `#` and a consumer on a queue; publish 2 000 events with ~1 KB bodies plus put 10 messages; F receives all events and all deliveries within the test timeout; `broker.stats`/outbox reports drops for S.
- [ ] **Step 2–3: Run, implement.** Keep `server.py` under ~400 lines (split handler registration into a `_handlers.py` if needed).
- [ ] **Step 4: Docs** `slow-subscribers.md` — the overflowing letterbox: the postie keeps the newest letters, notes how many old ones were thrown away, and never stops the round for one full box.
- [ ] **Step 5: Gates. Commit** `feat(broker): add broker server and per-connection outbox`.

---

### Task 8: BrokerClient (single connection behaviour)

**Files:**
- Create: `src/pymq/broker/client.py`
- Test: `tests/broker/test_client.py`

**Interfaces:**
- Consumes: Tasks 6–7, `pynet.messaging.ReconnectingClient`.
- Produces:
  - `type DeliveryHandler = Callable[[Delivery], Awaitable[None]]`, `type EventHandler = Callable[[TopicEvent], Awaitable[None]]`.
  - `class Delivery`: properties `queue`, `id`, `body`, `headers`, `attachments`, `priority`, `attempt`, `envelope`; `settled: bool`; `async ack() -> bool`, `async nack(*, requeue: bool = True, error: str | None = None) -> bool`, `async touch() -> bool` — return `False` when stale; a second settle on the same `Delivery` returns `False` without a network call.
  - `class Consumer`: `tag: str | None` (current server tag), `queue`, `async cancel() -> None`.
  - `class Subscription`: `pattern`, `async cancel() -> None`.
  - `class BrokerClient(host: str, port: int, *, put_timeout: float = 30.0, request_timeout: float = 10.0, **reconnect_kwargs)`: `async start()`, `async close()`, `async wait_connected(timeout: float | None = None)`, `async with` support (start + wait_connected on enter, close on exit); `on_event(cb)` passthrough; `async declare_queue(name, config=None) -> QueueConfig`, `delete_queue`, `purge_queue -> int`, `list_queues -> list[str]`, `stats(name) -> QueueStats`, `broker_stats() -> dict[str, JsonValue]`; `async put(queue, body, *, id=None, priority=0, headers=None, attachments=(), delay=0.0) -> PutResult`; `async consume(queue, handler, *, prefetch=10, auto_ack=True) -> Consumer`; `async publish(topic, body, *, headers=None, attachments=()) -> PublishResult`; `async subscribe(pattern, handler, *, max_pending=1_000) -> Subscription`; `async bind(pattern, queue)`, `unbind`; `async dead_letters(queue, limit=None) -> list[DeadLetter]`, `replay_dead(queue, ids=None) -> int`, `purge_dead(queue) -> int`.
  - Errors: every `RemoteError` is converted with `remote_to_error`; names and topics are validated client-side first (Review Focus 4).

Behaviour pinned here:
- `auto_ack=True`: handler returns → ack; handler raises `Exception` → nack(requeue=True, error=`f"{type(exc).__name__}: {exc}"`); if the handler already settled the delivery, nothing more is sent. `auto_ack=False`: nothing automatic.
- Each delivery's handler runs in its own task (so up to `prefetch` run at once); exceptions are logged, never crash the client.
- Event handlers for one subscription run one at a time in arrival order.

- [ ] **Step 1: Failing tests** against a real `BrokerServer` on port 0: round trip with auto-ack (stats `acked == 1`); handler raising → `attempt == 2` redelivery and the dead letter after `max_attempts` carries `"ValueError: boom"`; manual ack mode; touch keeps a message leased past `ack_timeout` (use small timeouts like `ack_timeout=0.2` — real clock is unavoidable over sockets, keep every wait under 2 s); subscribe + publish delivers `TopicEvent` with headers and attachments; bind + consume receives published events with `headers["topic"]`; `QueueFullError`, `QueueNotFoundError` surface as those classes; invalid name raises before any request (assert server saw no request — e.g. via `broker_stats` counts unchanged); `dead_letters`/`replay_dead`/`purge_dead` round trip; `concurrent handlers up to prefetch` (prefetch 3, handlers block on an `Event`; exactly 3 running).
- [ ] **Step 2–3: Run, implement. Step 4: Gates. Commit** `feat(broker): add async broker client`.

---

### Task 9: Client reconnection, replay and resend

**Files:**
- Modify: `src/pymq/broker/client.py`
- Create: `docs/concepts/reconnecting-clients.md`
- Test: `tests/broker/test_client_reconnect.py`

**Interfaces:**
- Consumes: Task 8.
- Produces: no new public names; behaviour only.

Behaviour pinned here:
- The client records declarations (name → config), bindings, subscriptions (pattern, handler, max_pending) and consumers (queue, handler, prefetch, auto_ack). On every `CONNECTED`/`RECONNECTED` it replays, in order: declare queues → bind → subscribe → consume. Cancelled consumers/subscriptions and deleted queues are removed from the record. A replay step that fails is logged and the rest continue.
- Server tags change on reconnect: `Consumer.tag` updates; deliveries carry their connection generation; `ack`/`nack`/`touch` on a delivery from an older generation return `False` immediately without a request (Review Focus 2).
- `put`: id generated before the first send; on `ConnectionClosedError`/`BrokerUnavailable`/timeout from a dropped connection it waits for reconnection and resends the same id, until `put_timeout` total elapses → `BrokerUnavailableError`. Other errors (`QueueFullError` …) are raised straight away.
- Other requests made while disconnected wait for the connection up to `request_timeout`, then raise `BrokerUnavailableError` (no automatic resend — they are not idempotent in general).

- [ ] **Step 1: Failing tests** (real sockets; restart the server on the *same port* with the *same* `Broker` instance to simulate a dropped connection, or a *new* `Broker` to simulate a broker restart):
  - `test_consumer_resumes_after_server_restart` (new Broker: queue re-declared by replay, new puts are delivered, `Consumer.tag` changed).
  - `test_subscription_and_binding_replayed`.
  - `test_ack_from_old_connection_is_stale` (Review Focus 2).
  - `test_put_resent_after_drop_is_not_duplicated` (Review Focus 5: same Broker; force the first attempt's connection to drop after the server processed the put but before the reply — e.g. a server-side hook that closes the peer inside the `msg.put` handler after calling `broker.put` — then assert `stats.ready == 1` and `duplicates == 1`).
  - `test_put_gives_up_after_put_timeout` (server never comes back; `put_timeout=0.5` → `BrokerUnavailableError`).
  - `test_request_while_disconnected_raises_unavailable`.
- [ ] **Step 2–3: Run, implement.**
- [ ] **Step 4: Docs** `reconnecting-clients.md` — redialling a friend and reminding them what you were talking about; why acks from before the drop don't count; why `put` can safely resend.
- [ ] **Step 5: Gates. Commit** `feat(broker): replay client setup and resend puts after reconnect`.

---

### Task 10: CLI, examples, docs index and README

**Files:**
- Create: `src/pymq/broker/__main__.py`, `examples/broker_demo.py`, `examples/work_queue.py`, `examples/topic_alerts.py`
- Modify: `pyproject.toml` (`[project.scripts] pymq-broker = "pymq.broker.__main__:main"`), `docs/index.md`, `README.md`
- Test: `tests/broker/test_cli.py`, `tests/test_examples.py`

**Interfaces:**
- Produces: `main(argv: Sequence[str] | None = None) -> int` in `__main__.py` — `--host` (default `127.0.0.1`), `--port` (default `5672`), `--heartbeat` (default 5.0); runs `BrokerServer` until `KeyboardInterrupt`, logging `listening on HOST:PORT`, connections, and each `$sys.dead.*` event; returns 0. `serve_forever(host, port, *, heartbeat, ready: Callable[[int], None] | None = None) -> Coroutine` helper so tests can start it and learn the port.
- Each example exposes `main() -> None` that runs to completion in under 3 s on localhost port 0 and prints what it shows.

- [ ] **Step 1: Failing tests**: `main(["--port", "x"])` exits with argparse error code 2; `serve_forever` on port 0 calls `ready(port)` and accepts a `BrokerClient` that can put/consume; `tests/test_examples.py` imports each example and runs `main()` (capsys output contains a key line per example).
- [ ] **Step 2–3: Run, implement.** Examples: `broker_demo.py` (local `Broker`, no network: put, consume, nack, dead letter), `work_queue.py` (server + producer + two workers, one crashes on its first message → redelivered to the other), `topic_alerts.py` (bind `job.*.failed` to an `alerts` queue, subscribe `#` live, publish a few job events).
- [ ] **Step 4: Docs.** `docs/index.md`: keep the existing "Big Ideas", add "Part 2: A Real Post Office" learning path linking, in order: broker, topics-and-wildcards, leases-and-acks, retries-and-delays, dead-letters-v2, fair-dispatch, idempotency, bindings, slow-subscribers, reconnecting-clients; replace the "Heads up: no visibility timeout" box with a note that the networked broker now has leases (link). `README.md`: a "The Real Post Office — Networked Broker" section with the client snippet from the spec and how to run `pymq-broker`; a "Releasing" note per the pin-sibling-deps spec (0.x semver: public API change bumps minor).
- [ ] **Step 5: Gates. Commit** `feat(broker): add pymq-broker CLI, examples and learning path`.

---

### Task 11: PyStack `mqnet` demo

**Files (in `/Users/robcox/development/python/pystack`, on a new branch in a worktree — never switch the user's checkout):**
- Modify: `src/pystack/plugins/mq_plugin.py`, `tests/test_integration_plugins.py` (or the mq plugin's existing test module), `README.md`, `docs/index.md`; `pyproject.toml`/`uv.lock` only if pymq's new pynet dependency requires it.

**Interfaces:**
- Consumes: the plugin's existing command-registration pattern (follow `netecho` in `net_plugin.py`), `pymq.broker.BrokerServer`, `BrokerClient`, `QueueConfig`.
- Produces: shell command `mqnet` — starts a broker on a free localhost port, declares `jobs` (`max_attempts=2`, `ack_timeout=0.5`, `retry_delay=0.1`) and subscribes `$sys.dead.*`; puts `job-1`, `job-2` and a poison `job-boom`; two workers, worker A "crashes" (nacks with `error="worker A crashed"`) on its first message; prints each step, ending with the dead letter for `job-boom` with reason `max_attempts`, and the `$sys.dead.jobs` event. Finishes in under 3 s.

- [ ] **Step 1: Failing test** that runs the command and asserts the output includes `job-1 done`, `job-2 done`, `dead: job-boom (max_attempts)` and `$sys.dead.jobs`.
- [ ] **Step 2–3: Run, implement. Step 4:** README command table + `docs/index.md` entry. **Step 5:** pystack gates (pytest, pyright, ruff). **Commit** `feat(mq): add mqnet demo for PyMQ's networked broker`.

Note: pystack CI clones sibling `main`s; this PR must merge after the pymq PR.
