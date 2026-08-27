"""Outbound content rules.

Two obligations, tested separately: every forbidden shape must be caught, and
ordinary story prose must survive. A gate that blocks normal narrative would
push every beat onto the fallback line and quietly destroy the product.
"""

from __future__ import annotations

import pytest

from backend.core.validation import MAX_MESSAGE_CHARS, Rule, validate_outbound


def rules_for(text: str) -> set[str]:
    return set(validate_outbound(text).rule_ids)


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("If you see him, call 911 immediately.", Rule.REAL_EMERGENCY),
        ("Call the police. Now.", Rule.REAL_EMERGENCY),
        ("I'm bleeding, I don't have long.", Rule.REAL_EMERGENCY),
        ("He's dead. They got to him first.", Rule.REAL_EMERGENCY),
        ("This is not a drill.", Rule.REAL_EMERGENCY),
        ("This is Detective Hargreaves, city police.", Rule.IMPERSONATION),
        ("I'm from your bank's fraud team.", Rule.IMPERSONATION),
        ("A warrant has been issued in your name.", Rule.IMPERSONATION),
        ("Confirm your password so I know it's you.", Rule.CREDENTIAL_OR_MONEY),
        ("Send me the one-time code they just texted you.", Rule.CREDENTIAL_OR_MONEY),
        ("Photograph your passport and send it over.", Rule.CREDENTIAL_OR_MONEY),
        ("Transfer $200 to the account below.", Rule.CREDENTIAL_OR_MONEY),
        ("Climb over the fence at the back.", Rule.UNSAFE_INSTRUCTION),
        ("Go into the abandoned depot.", Rule.UNSAFE_INSTRUCTION),
        ("Meet me at the docks at night.", Rule.UNSAFE_INSTRUCTION),
        ("Follow the man in the grey coat.", Rule.UNSAFE_INSTRUCTION),
        ("Take a photo of that woman by the gate.", Rule.UNSAFE_INSTRUCTION),
        ("This isn't a game. I'm a real person.", Rule.DENIES_FICTION),
        ("I'm not an AI, I promise.", Rule.DENIES_FICTION),
    ],
)
def test_forbidden_content_is_refused(text: str, rule: Rule) -> None:
    result = validate_outbound(text)
    assert not result.ok, f"should have been refused: {text!r}"
    assert str(rule) in result.rule_ids


@pytest.mark.parametrize(
    "text",
    [
        "The envelope was where she said it would be. Bring it to the bench by the fountain.",
        "You're early. Good. Wait until the church bell goes, then walk to the old port.",
        "I need a picture of the blue door. Just the door — nobody in frame.",
        "Whatever you do, don't lose the key. It's the only one.",
        "Nine years I kept that file. I never thought I'd hand it to a stranger.",
        "Meet me at the cafe on Wednesday morning, the one with the green awning.",
        "She stopped writing in 1998. That's when the trail goes cold.",
        "Take the long way round. There's no hurry today.",
    ],
)
def test_ordinary_story_prose_passes(text: str) -> None:
    result = validate_outbound(text)
    assert result.ok, f"false positive on {text!r}: {result.rule_ids}"


def test_empty_messages_are_refused() -> None:
    assert Rule.MALFORMED in rules_for("   ")


def test_overlong_messages_are_refused() -> None:
    assert Rule.MALFORMED in rules_for("x" * (MAX_MESSAGE_CHARS + 1))


def test_every_violation_is_reported_not_just_the_first() -> None:
    text = "This is the police. Send us your password and $500."
    result = validate_outbound(text)
    assert not result.ok
    assert Rule.IMPERSONATION in result.rule_ids
    assert Rule.CREDENTIAL_OR_MONEY in result.rule_ids
