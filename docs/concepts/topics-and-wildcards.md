# Topics and Wildcards

## Postcodes and Sorting Machines

A postcode like `SW1A 1AA` narrows a letter down step by step: area, then
district, then street. A **topic** works the same way. `job.nightly.failed`
reads from the big idea (`job`) to the small one (`failed`), with dots between
the **words**.

At the post office, a **sorting machine** is told which letters to grab. You
can tell it an exact address, or you can give it a rule with blanks:

- `*` is a blank for **exactly one** word.
- `#` is a blank for **any number of words**, even none at all.

A rule like that is called a **pattern**.

## Try It

```python
from pymq.broker import topic_matches, validate_pattern, validate_topic

validate_topic("job.nightly.failed")
validate_pattern("job.*.failed")

print(topic_matches("job.*.failed", "job.nightly.failed"))
print(topic_matches("job.*.failed", "job.a.b.failed"))
print(topic_matches("job.#", "job"))
print(topic_matches("#.failed", "a.b.failed"))
print(topic_matches("job.*", "job"))

try:
    validate_topic("job.*")
except Exception as error:
    print(type(error).__name__)
```

This prints:

```
True
False
True
True
False
InvalidTopicError
```

## `*` versus `#`

| Pattern        | Topic               | Match? | Why                                  |
| -------------- | ------------------- | ------ | ------------------------------------ |
| `job.*.failed` | `job.nightly.failed`| yes    | `*` stood in for `nightly`           |
| `job.*.failed` | `job.a.b.failed`    | no     | `*` is only ONE word                 |
| `job.*`        | `job`               | no     | `*` needs a word to stand in for     |
| `job.#`        | `job`               | yes    | `#` may be zero words                |
| `job.#`        | `job.a.b`           | yes    | `#` can be many words                |
| `#.failed`     | `failed`            | yes    | zero words before `failed`           |
| `job.#.failed` | `job.x.y.failed`    | yes    | `#` covered `x.y`                    |
| `#`            | `anything.at.all`   | yes    | catches everything                   |

## The Rules

- A topic is 1 to 255 characters of words joined by `.`.
- Each word is made of letters, digits, `_` or `-`, and is never empty
  (so `a..b`, `.a` and `a.` are all rejected).
- A topic can never contain `*` or `#`; only patterns can.
- In a pattern, `*` and `#` must be a whole word. `a*` and `#x` are rejected.

## System Topics

Topics whose first word is `$sys` (for example `$sys.dead.jobs`) are
reserved for the broker's own announcements, like a notice board at the post
office. The `$` is allowed only in the first word. Patterns such as `$sys.dead.*`
find them, and so does a catch-all `#`.

## What We Test

- A table of pattern/topic pairs gives the right yes/no answer.
- Empty words, spaces, wildcards inside topics, and 256-character topics are rejected.
- Patterns like `a*` and `#x` are rejected.
- Ten `#`s followed by `z` against 50 words finishes instantly (no exponential backtracking).

## Next Up

Back to [Pub/Sub](pubsub.md) for the newsletter idea these topics build on.
