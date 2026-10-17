"""Topic names and wildcard patterns.

Think of a topic like a postcode: ``job.nightly.failed`` narrows things down
one word at a time. A pattern is a sorting-machine rule: ``*`` accepts exactly
one word in that spot, and ``#`` accepts any number of words (even none).
"""

import re

from pymq.broker.errors import InvalidTopicError

SYSTEM_PREFIX = "$sys"
"""Reserved first word for topics the broker itself publishes."""

MAX_TOPIC_LENGTH = 255

_WORD_RE = re.compile(r"[A-Za-z0-9_-]+")
_FIRST_WORD_RE = re.compile(r"\$?[A-Za-z0-9_-]+")
_ONE = "*"
_MANY = "#"


def _check_words(text: str, kind: str, *, allow_wildcards: bool) -> str:
    """Validate the words of text, raising InvalidTopicError on any problem."""
    if not 1 <= len(text) <= MAX_TOPIC_LENGTH:
        msg = f"invalid {kind}: length must be 1-{MAX_TOPIC_LENGTH}"
        raise InvalidTopicError(msg)
    for index, word in enumerate(text.split(".")):
        if allow_wildcards and word in {_ONE, _MANY}:
            continue
        word_re = _FIRST_WORD_RE if index == 0 else _WORD_RE
        if not word_re.fullmatch(word):
            msg = f"invalid {kind}: {text!r}"
            raise InvalidTopicError(msg)
    return text


def validate_topic(topic: str) -> str:
    """Return topic if it is a valid topic name, else raise InvalidTopicError."""
    return _check_words(topic, "topic", allow_wildcards=False)


def validate_pattern(pattern: str) -> str:
    """Return pattern if it is a valid topic pattern, else raise InvalidTopicError."""
    return _check_words(pattern, "pattern", allow_wildcards=True)


def topic_matches(pattern: str, topic: str) -> bool:
    """Return True if topic matches pattern (both assumed already valid).

    ``*`` matches exactly one word; ``#`` matches zero or more words.
    Runs in time proportional to pattern words times topic words.
    """
    words = topic.split(".")
    # `states` holds how many topic words each partial match has consumed.
    states = {0}
    for part in pattern.split("."):
        next_states: set[int] = set()
        for consumed in states:
            if part == _MANY:
                next_states.update(range(consumed, len(words) + 1))
            elif consumed < len(words) and (part == _ONE or part == words[consumed]):
                next_states.add(consumed + 1)
        if not next_states:
            return False
        states = next_states
    return len(words) in states
