# PyMQ Network Broker — Design

**Date:** 2026-10-17
**Repo:** pymq (library), pystack (integration)
**Depends on:** pynet ≥ the event-loop release (`pynet.loop`, `pynet.messaging`)

## Why

PyMQ today is an in-process teaching library: a FIFO queue, a priority
queue, dead letters and a simple pub/sub, all synchronous, all in one
Python process. A message that is never acknowledged stays "in flight"
forever.

The job scheduler (the goal of this whole series) needs a real broker
that its parts talk through:

- **scheduler → agents:** "run job X" work items, delivered to exactly one
  agent, redelivered if that agent dies before confirming.
- **agents → scheduler:** job-state events (started, finished, exit code).
- **scheduler → notifiers and web UI:** job events fanned out by topic
  ("every `job.*.failed`"), buffered for a notifier that is briefly down.

So PyMQ becomes a **networked, async broker running on PyNet's own event
loop**, while the existing synchronous classes stay as they are for
learning ("keep them alongside").

The user handed every design choice to Claude ("you decide what you think
makes most sense"); everything below is Claude's call unless marked as the
user's.

## Goals

1. A broker server (`pymq.broker`) on `pynet.messaging`, plus an async
   client that survives broker restarts.
2. Work queues with at-least-once delivery: leases (visibility timeout),
   ack / nack / touch, retries with exponential backoff, delayed messages,
   priority, max length, message TTL, dead letters that remember *why*.
3. Topics with AMQP-style wildcards (`*` one word, `#` zero or more), live
   subscriptions, and **bindings** that copy matching events into a queue.
4. Fair dispatch to many consumers with a per-consumer prefetch limit.
5. Idempotent publishing: a producer may resend a message after a dropped
   connection without creating a duplicate.
6. Introspection for the scheduler and web UI: per-queue stats, including
   the age of the oldest waiting message (the "job never starts" signal).
7. Kid-friendly docs for every concept, runnable examples, PyStack demo.

## Non-goals

- **Persistence.** The broker is in-memory. If it restarts, queued
  messages are gone; the scheduler keeps its own truth in PyDB (upgraded
  later) and re-dispatches. Docs say this plainly.
- Authentication and TLS (the user chose "plain for now, TLS later").
- Clustering, replication, exactly-once delivery.
- Changing the existing synchronous `pymq.queue` / `pymq.pubsub` API.

## Anti-goals

- A broker that can be stalled by one slow or stuck client.
- Messages that silently vanish: every message ends acked, dead-lettered
  (with a reason), or still in the broker. Topic events dropped for a slow
  live subscriber are *counted*, never silent.
- Timing tests that wait for real seconds.

## Constraints

- Python 3.14, uv, ruff (repo rule set), pyright strict, pytest ≥ 80 %
  coverage, conventional commits, PR into protected `main` — as in the
  repo's CLAUDE.md. No `TYPE_CHECKING`, no `from __future__ import
  annotations`.
- Runs on `pynet.loop` (not asyncio). Tests use `pynet.loop.testing.FakeClock`
  for anything time-based and real localhost sockets for the network layer.
- Add a per-test timeout (10 s, as pynet does) so a hung test fails fast.
- pymq gains a runtime dependency on pynet. It follows the sibling-pinning
  convention in pystack's `docs/superpowers/specs/2026-10-17-pin-sibling-deps-design.md`
  (git source pinned to a pynet tag) if pynet has a tag when the work
  lands; until then a `path = "../pynet"` uv source, with CI cloning
  pynet next to the repo exactly as pystack's CI does today.

## Architecture

Three layers, each usable on its own:

```
BrokerClient ──(pynet.messaging over TCP)──> BrokerServer ──> Broker (core)
                                                               ├─ WorkQueue × n
                                                               └─ TopicRouter
```

- **`Broker` (core)** — pure in-process async object. Owns queues, the
  topic router, timers. No sockets. The scheduler could embed it directly,
  and most behaviour is tested here with a `FakeClock`.
- **`BrokerServer`** — exposes a `Broker` over `pynet.messaging.serve`.
  One `Peer` per client connection; translates requests into core calls
  and core deliveries into pushes. Cleans up when a connection dies.
- **`BrokerClient`** — wraps `pynet.messaging.ReconnectingClient`.
  Remembers what the user declared, consumed, subscribed and bound, and
  replays all of it on every reconnect.

### Package layout (Claude's call)

```
src/pymq/broker/
  __init__.py      re-exports the public API
  errors.py        BrokerError + subclasses
  model.py         QueueConfig, Envelope, DeadLetter, QueueStats, reasons
  topics.py        pattern validation + matching
  workqueue.py     one queue: ready heap, delayed heap, leases, consumers
  router.py        TopicRouter: subscriptions + bindings
  core.py          Broker
  protocol.py      method names, JSON (de)serialisation of model types
  server.py        BrokerServer
  client.py        BrokerClient, Delivery, Subscription handles
  __main__.py      `python -m pymq.broker --host H --port P`
```

Files stay small and single-purpose; no file should pass ~400 lines.

## Behaviour

### Messages

`Envelope` fields: `id` (str, producer-chosen or uuid4), `queue`, `body`
(any JSON value), `attachments` (tuple of bytes), `headers`
(dict[str, str]), `priority` (int, lower = sooner, default 0), `attempt`
(1 on first delivery), `enqueued_at` (wall-clock ISO-8601 UTC, for humans),
plus loop-clock bookkeeping that never leaves the broker.

### Work queues

`QueueConfig` (all per queue, defaults in brackets):

| Field | Meaning |
|---|---|
| `ack_timeout` [30 s] | lease length; an unacked delivery returns when it expires |
| `max_attempts` [5] | deliveries before the message is dead-lettered |
| `retry_delay` [1 s] | first retry wait; doubles each attempt |
| `max_retry_delay` [60 s] | cap on that wait |
| `max_length` [10 000] | ready + delayed limit; `put` beyond it fails with `QueueFullError` |
| `message_ttl` [None] | seconds a message may wait unconsumed before dead-lettering |
| `max_dead_letters` [1 000] | dead-letter list cap; oldest dropped first (and counted) |
| `dedupe_window` [300 s] | how long a message id is remembered for idempotent `put` |

Every numeric field is validated (finite, > 0, whole numbers where
counts); bad config raises `ValueError` at declare time.

`declare_queue(name, config)` is idempotent with the same config; a
different config for an existing name raises `QueueConfigConflictError`.
Queue names: 1–200 chars of letters, digits, `.`, `-`, `_`.

Life of a message:

1. **put** (optionally `delay` seconds, `priority`, `headers`, `id`). With
   a delay it waits in the *delayed* heap, else goes to *ready*. If `id`
   was seen within `dedupe_window`, the put succeeds but nothing is added
   (the result says `duplicate: true`).
2. **deliver** — ready messages go, highest priority then oldest first, to
   consumers round-robin, skipping any consumer whose in-flight count has
   reached its `prefetch`. Delivery starts a lease of `ack_timeout`.
3. The consumer then:
   - **ack** — gone for good.
   - **nack(requeue=True)** — counts as a failed attempt: if attempts are
     used up → dead letter (`max_attempts`); else back to *delayed* for
     `min(retry_delay * 2**(attempt-1), max_retry_delay)`.
   - **nack(requeue=False)** — straight to dead letters (`rejected`).
   - **touch** — renew the lease for another `ack_timeout` (long jobs).
   - nothing — the lease expires and it is treated as `nack(requeue=True)`
     (reason if it dies: `max_attempts`).
4. A consumer whose connection drops, or who cancels, hands all its leased
   messages back as `nack(requeue=True)` immediately (a crash counts as a
   failed attempt, so a poison message that kills workers ends up dead
   rather than looping forever).
5. A message waiting longer than `message_ttl` in ready/delayed is
   dead-lettered with `ttl_expired`.

Acks, nacks and touches name a **delivery tag** (unique per delivery, not
per message). A tag that is stale (lease expired, connection gone, already
settled) is ignored and reported back as `stale: true` — the message has
already been, or will be, redelivered. That is what at-least-once means,
and the docs say so.

Dead letters keep the envelope plus `reason` (`max_attempts`, `rejected`,
`ttl_expired`), `dead_at` and the last error text a consumer gave (nack
accepts an optional `error` string). Admin operations: `dead_letters(queue,
limit)`, `replay_dead(queue, ids | all)` (back to ready, attempt reset to
0), `purge_dead(queue)`. When a message is dead-lettered the broker
publishes an event on topic `$sys.dead.<queue>` (body: the dead letter
summary), so an alerting notifier can bind to it.

`delete_queue(name)` drops the queue and cancels its consumers;
`purge_queue(name)` drops ready and delayed messages only.

### Stats

`QueueStats`: `ready`, `delayed`, `in_flight`, `dead`, `consumers`,
`oldest_ready_age` (seconds, `None` if empty), and running totals
`enqueued`, `delivered`, `acked`, `retried`, `dead_lettered`,
`dead_dropped`, `duplicates`. `list_queues()` returns names; `stats(name)`
one queue; `broker_stats()` adds connection and subscription counts.

### Topics

- Topic names are dot-separated words (`job.nightly-report.failed`).
  Words: letters, digits, `-`, `_`, plus a leading `$` on the first word
  for system topics. Patterns may also use `*` (exactly one word) and `#`
  (zero or more words). Invalid names/patterns raise `InvalidTopicError`.
- `publish(topic, body, headers=…, attachments=…)` → delivered to every
  matching live subscription and copied (as a `put`) into every queue
  bound with a matching pattern. Returns how many subscriptions and
  queues it reached. Publishing to a topic nobody listens to is fine.
- `subscribe(pattern)` — live, fire-and-forget, no acks. Each subscription
  has a bounded outbox (`max_pending`, default 1 000); when full the
  oldest event is dropped and `dropped` counted, so one slow subscriber
  never slows the broker or anyone else.
- `bind(pattern, queue)` / `unbind` — durable-for-the-broker's-lifetime
  routing: events keep piling up in the queue while its consumer is away,
  with all the queue's ack/retry/dead-letter behaviour. A binding whose
  queue is full counts a drop on the binding rather than failing the
  publish.

### Network protocol

Built on `pynet.messaging` requests (client → server) and pushes (server →
client). Method names and body shapes live in `protocol.py`, one place.

Requests: `queue.declare`, `queue.delete`, `queue.purge`, `queue.stats`,
`queue.list`, `broker.stats`, `msg.put`, `msg.ack`, `msg.nack`,
`msg.touch`, `consume.start`, `consume.cancel`, `topic.publish`,
`topic.subscribe`, `topic.unsubscribe`, `topic.bind`, `topic.unbind`,
`dead.list`, `dead.replay`, `dead.purge`.

Pushes: `deliver` (consumer tag, delivery tag, envelope) and `event`
(subscription id, topic, body, headers). Attachments ride as pynet
attachments, never Base64.

Errors cross the wire as pynet `RemoteError` codes (`queue_not_found`,
`queue_full`, `config_conflict`, `invalid_topic`, `invalid_request`) and
the client turns each back into its `BrokerError` subclass.

The server never awaits a slow client inside broker logic: deliveries and
events go through each connection's own outbox task, and prefetch bounds
how many deliveries a consumer can have outstanding.

### Client

```python
async with BrokerClient("127.0.0.1", 5672) as mq:
    await mq.declare_queue("jobs", QueueConfig(ack_timeout=60))
    await mq.put("jobs", {"job": "nightly-report"}, delay=5)

    async def work(d: Delivery) -> None:
        print(d.body)            # returning normally acks; raising nacks

    consumer = await mq.consume("jobs", work, prefetch=4)
    sub = await mq.subscribe("job.*.failed", on_event)
    await mq.bind("job.#", "audit")
```

- `consume(queue, handler, *, prefetch=10, auto_ack=True)`. With
  `auto_ack`, returning acks and raising nacks (with the exception text as
  `error`). With `auto_ack=False` the handler calls `await d.ack()`,
  `d.nack(requeue=…, error=…)` or `d.touch()`. Handlers for one consumer
  may run concurrently up to `prefetch`.
- `put` generates the message id before sending; if the connection drops
  mid-request it waits for the reconnect and resends the *same* id, so
  dedupe makes it safe. `put` gives up with `BrokerUnavailableError` after
  `put_timeout` (default 30 s).
- On every reconnect the client re-declares queues, re-binds,
  re-subscribes and restarts consumers, in that order. Deliveries from the
  old connection that are still being handled finish, but their acks are
  stale (the broker already requeued them) — at-least-once.
- `on_event(cb)` passes through pynet's connection events so callers can
  log "lost broker / reconnected".

### Server process

`python -m pymq.broker --host 127.0.0.1 --port 5672` (and a
`pymq-broker` script) runs a broker until Ctrl-C, logging connections and
dead letters. Default host `127.0.0.1` (Claude's call: no accidental
exposure on all interfaces).

## Docs (kid-friendly, every concept)

New `docs/concepts/` pages, each with an analogy, a runnable snippet that
was actually run, and a "What we test" section:

- `broker.md` — the post office sorting room; local vs networked.
- `leases-and-acks.md` — library book due dates; ack, nack, touch, expiry,
  at-least-once and why handlers should be safe to run twice.
- `retries-and-delays.md` — the snooze button; backoff doubling, delayed
  messages.
- `dead-letters-v2.md` — the lost-property office that writes down why;
  replay and purge; `$sys.dead.*`.
- `topics-and-wildcards.md` — postcodes and sorting machines; `*` vs `#`.
- `bindings.md` — a PO box that holds your mail while you're on holiday.
- `fair-dispatch.md` — buffet plates and prefetch.
- `slow-subscribers.md` — the overflowing letterbox; drop-oldest.
- `idempotency.md` — raffle ticket numbers; safe resends.
- `reconnecting-clients.md` — redialling and replaying your setup.

Plus: `docs/index.md` learning path extended (old pages first, then the
broker path), README section, and `examples/` (`broker_demo.py` all-local,
`work_queue.py`, `topic_alerts.py`), each with a `main()`.

## PyStack integration

Add `mqnet` to pystack's mq plugin: starts a broker on a free localhost
port, a producer and two workers; one worker "crashes" on its first
message so the audience sees the lease return it and the other worker
finish it; a poison message lands in dead letters with its reason; a
topic subscription prints the `$sys.dead.*` event. Tests, README and
`docs/index.md` updated, as with `netecho`.

## Testing approach

- Core (`workqueue`, `router`, `core`) with `FakeClock(autojump=True)`:
  every timer path (lease expiry, backoff, delay, TTL, dedupe window).
- A conservation test: random puts/acks/nacks/expiries; at the end every
  message id is acked, dead, or still in the queue — exactly once.
- Server + client over real localhost sockets: round trips, error-code
  mapping, consumer disconnect returns leases, slow subscriber cannot
  stall a fast one, broker restart → client replays setup and resends a
  put without duplicating it.
- Pattern matching table tests including `#` matching zero words.

## Left to the builder

Internal data structures, exact log wording, outbox mechanics, and how the
plan splits the work into tasks.
