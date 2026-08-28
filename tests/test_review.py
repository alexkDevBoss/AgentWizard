"""The model-based second pass.

The interesting cases are not "does it say yes to a nice message" -- they are
the three ways a verdict can be untrustworthy: the reviewer is unreachable, it
contradicts itself, or it is asked to judge without seeing that the player is
frightened.
"""

from __future__ import annotations

from backend.core import model, review


def test_a_clean_message_passes(story) -> None:
    result = review.review_outbound("Wren here. Which bench?", recent=[])

    assert result.ok
    assert result.rules == ()
    assert not result.unreviewed


def test_a_refusal_names_its_rules(story) -> None:
    story.refuse("impersonation", reason="claims to be the police")

    result = review.review_outbound("This is the police.", recent=[])

    assert not result.ok
    assert result.rules == ("impersonation",)
    assert result.reason == "claims to be the police"


def test_a_contradictory_verdict_is_resolved_against_the_message(story) -> None:
    """`safe: true` alongside a named rule must not be read as approval."""
    story.verdict = {"safe": True, "rules": ["denies_fiction"], "reason": "says real"}

    result = review.review_outbound("This is really happening.", recent=[])

    assert not result.ok
    assert result.rules == ("denies_fiction",)


def test_an_unreachable_reviewer_passes_the_message_but_says_so(story) -> None:
    """Failing closed here would let a throttled account mute the whole story."""
    story.review_error = model.ModelUnavailable("429 Too many requests")

    result = review.review_outbound("Wren here.", recent=[])

    assert result.ok
    assert result.unreviewed


def test_the_reviewer_is_shown_the_recent_conversation(story) -> None:
    """It cannot see a distressed player from the candidate message alone."""
    review.review_outbound(
        "Anyway -- the tapes.",
        recent=["Player: is this real", "Story: of course it is"],
    )

    sent = story.reviews[-1]["messages"][0]["content"]
    assert "is this real" in sent
    assert "Anyway -- the tapes." in sent


def test_only_the_tail_of_a_long_conversation_is_sent(story) -> None:
    review.review_outbound("x", recent=[f"Player: {i}" for i in range(50)])

    sent = story.reviews[-1]["messages"][0]["content"]
    assert "Player: 49" in sent
    assert "Player: 10" not in sent


def test_the_verdict_tool_only_allows_known_rule_ids(story) -> None:
    """A free-text rule id would not match anything the admin panel can show."""
    from backend.core.validation import Rule

    rules = review.VERDICT_TOOL["input_schema"]["properties"]["rules"]
    allowed = rules["items"]["enum"]
    assert set(allowed) == {str(r) for r in Rule}


def _prose(text: str) -> str:
    """Compare wrapped prose by meaning, not by where the lines happen to break."""
    return " ".join(text.split())


def test_the_reviewer_is_told_that_ordinary_fiction_is_not_a_violation() -> None:
    """Over-strictness is a real failure mode: it replaces the story with a fallback."""
    assert "Being over-strict is a real failure" in _prose(review.SYSTEM)


def test_the_reviewer_is_told_to_stop_the_story_for_a_worried_player() -> None:
    assert "The story must yield to a worried player" in _prose(review.SYSTEM)
