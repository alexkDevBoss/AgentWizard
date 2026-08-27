"""Command parsing and the concern flag.

STOP failing to register is the worst thing this system can do, so the parser
is tested for both halves of its contract: it must catch every reasonable way a
player writes a control word, and it must not fire on prose that merely
contains one.
"""

from __future__ import annotations

import pytest

from backend.core.safety import Command, mentions_stopping, parse_command


@pytest.mark.parametrize(
    "text",
    [
        "STOP",
        "stop",
        "Stop",
        "sToP",
        "  stop  ",
        "stop.",
        "STOP!",
        "stop!!!",
        "/stop",
        "/STOP",
        "please stop",
        "stop please",
        "stop now",
        "stop this",
        "Please stop.",
        "cancel",
        "quit",
        "unsubscribe",
    ],
)
def test_stop_is_recognised(text: str) -> None:
    assert parse_command(text)[0] is Command.STOP


@pytest.mark.parametrize(
    "text",
    [
        "I had to stop at the lights",
        "the bus stop on the corner",
        "don't stop believing",
        "Can you stop the car in the photo?",
        "I'll stop by tomorrow",
    ],
)
def test_stop_inside_a_sentence_does_not_trigger(text: str) -> None:
    """A keyword in prose flags for a human instead of acting on its own."""
    assert parse_command(text)[0] is Command.NONE
    assert mentions_stopping(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("PAUSE", Command.PAUSE),
        ("pause", Command.PAUSE),
        ("/pause", Command.PAUSE),
        ("hold", Command.PAUSE),
        ("RESUME", Command.RESUME),
        ("resume", Command.RESUME),
        ("/resume", Command.RESUME),
        ("continue", Command.RESUME),
        ("unpause", Command.RESUME),
        ("/real", Command.REAL),
        ("real", Command.REAL),
        ("/start", Command.START),
    ],
)
def test_other_commands(text: str, expected: Command) -> None:
    assert parse_command(text)[0] is expected


def test_start_carries_its_enrolment_code() -> None:
    command, argument = parse_command("/start AB3KD9XY")
    assert command is Command.START
    assert argument == "AB3KD9XY"


def test_group_chat_command_suffix_is_stripped() -> None:
    assert parse_command("/real@MyStoryBot")[0] is Command.REAL


@pytest.mark.parametrize("text", [None, "", "   ", "hello", "yes", "on my way"])
def test_ordinary_messages_are_not_commands(text: str | None) -> None:
    assert parse_command(text)[0] is Command.NONE


@pytest.mark.parametrize(
    "text",
    [
        "is this real?",
        "Are you a real person?",
        "am I in danger",
        "I'm scared",
        "this is freaking me out",
        "leave me alone",
        "I don't want to do this",
    ],
)
def test_distress_is_flagged_for_a_human(text: str) -> None:
    assert mentions_stopping(text)


@pytest.mark.parametrize("text", ["on my way", "found it", "here's the photo"])
def test_ordinary_messages_are_not_flagged(text: str) -> None:
    assert not mentions_stopping(text)
