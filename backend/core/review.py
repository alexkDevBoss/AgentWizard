"""The model-based second pass over generated content.

This is the layer the regex in :mod:`backend.core.validation` cannot be: it
reads meaning. A regex cannot tell "he's dead" in a forty-year-old recording
from "he's dead" asserted about the player's own life, and it cannot see a
message that never trips a pattern while still leaving a player convinced
something real is happening to them.

It sits **on top of** the regex layer and never replaces it. The order is
deliberate:

1. the deterministic layer runs first, in :func:`validation.validate_outbound`
2. this pass runs second, only on model-generated text

Only generated text is reviewed. An operator typing in the console is a human
who is accountable for what they send, and routing their words through a model
for approval would be both surprising and slower; the deterministic rules
still apply to them in dispatch.

**Failure of this call is not treated as a refusal.** If Bedrock times out or
rate-limits, the message has still passed every deterministic rule, and
failing closed would mean a throttled account silently replaces the story with
"…give me a moment." That trade is stated here rather than buried: an API
error downgrades to a loud log line and a ``needs_review`` flag on the
timeline, and a real verdict of unsafe always blocks.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.core import logs, model
from backend.core.validation import Rule

#: The judgement is returned through a forced tool call, so the shape is
#: guaranteed by the API rather than parsed out of prose.
VERDICT_TOOL = {
    "name": "verdict",
    "description": (
        "Record whether this message may be sent to the player. Call this exactly once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "safe": {
                "type": "boolean",
                "description": "True only if the message breaks none of the rules.",
            },
            "rules": {
                "type": "array",
                "items": {"type": "string", "enum": [str(r) for r in Rule]},
                "description": "Ids of every rule the message breaks. Empty if safe.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "One sentence for the operator's timeline. If safe, say 'ok'."
                ),
            },
        },
        "required": ["safe", "rules", "reason"],
        "additionalProperties": False,
    },
}

_TEMPLATE = f"""You are the safety reviewer for an interactive fiction service.

A player has knowingly signed up for a written story delivered over a
messaging app. They were told at enrolment, and are reminded regularly, that
the story is fiction and that every character is AI. Your job is to read one
message the story is about to send and decide whether it may go out.

You are reviewing fiction. Ordinary story content is fine: invented people,
invented places, sadness, tension, mystery, a character describing something
that happened long ago, a character who is dead in the story's past. None of
that is a violation and you must not flag it. Being over-strict is a real
failure -- it replaces the story with a fallback line and the player sees the
seams.

Refuse a message only when it does one of these:

- `{Rule.REAL_EMERGENCY}` -- presents a real, present emergency in the
  player's own life, or instructs them to take real emergency action (call
  emergency services, seek medical help, treat something as genuinely urgent).
  A crisis inside the story that the player is only reading about is fine.
- `{Rule.IMPERSONATION}` -- claims to be police, government, a bank, a
  hospital, an employer, a delivery service, or any other real institution
  contacting the player, or claims to be a specific real living person.
- `{Rule.CREDENTIAL_OR_MONEY}` -- asks for money, payment, account details,
  passwords, codes, identity documents, or access to anything of the
  player's.
{{unsafe_instruction}}
- `{Rule.DENIES_FICTION}` -- claims the story is real, claims the sender is a
  real human rather than AI, or tells the player that something they are
  worried about is actually happening. This includes a character insisting
  they are not AI when the player has sincerely asked.

One more rule, which has no pattern behind it and is the main reason you are
here:

- If the player's most recent messages show they are frightened, distressed,
  or sincerely unsure whether any of this is real, then a message that stays
  in character and presses on is a violation. Flag it as
  `{Rule.DENIES_FICTION}` and say so in the reason. The story must yield to a
  worried player immediately.

Judge only the candidate message. The conversation is context for that
judgement, not the thing being judged."""


#: The sit-down reading of "unsafe": the player is not going anywhere.
_ARMCHAIR_UNSAFE = f"""\
- `{Rule.UNSAFE_INSTRUCTION}` -- asks the player to do something physically
  risky or intrusive: go somewhere, especially after dark; enter, climb or
  break into anywhere; approach, follow, confront or photograph a real
  person; contact a real third party; drive; or anything that would put them
  in front of a stranger. Asking them to think, remember, describe what is
  already around them, or photograph an ordinary object is fine."""

#: The walking reading. Sending the player out is the point of the format, so
#: the rule cannot be "never move them" -- it has to name what actually makes
#: a destination unsafe. Left as the armchair version, this reviewer would
#: refuse every message a walking arc exists to send.
_WALKING_UNSAFE = f"""\
- `{Rule.UNSAFE_INSTRUCTION}` -- sends the player somewhere unsafe, or makes
  going feel urgent. This is a walking adventure and the player agreed to
  walk, so being asked to go to a named public place in daylight, at their own
  pace, is exactly right and is NOT a violation. What is a violation: sending
  them indoors, onto private property, over or through anything, anywhere
  needing a ticket or a fee, or anywhere after dark. Sending them toward a
  person -- approaching, following, waiting for or watching anybody. Asking
  for a photograph of a person, or of anything that would mean pointing a
  camera at a stranger. Any deadline, countdown, or suggestion that they are
  being followed or must hurry. Telling them to cross a road or commenting on
  traffic as though the sender can see them. Asking again after they have said
  they cannot or would rather not go somewhere."""


def system_prompt(*, walking: bool) -> str:
    """The reviewer's brief, matched to the kind of arc being played."""
    return _TEMPLATE.replace(
        "{unsafe_instruction}", _WALKING_UNSAFE if walking else _ARMCHAIR_UNSAFE
    )


#: The default brief, kept as a name for the tests and for callers that have
#: no arc in hand.
SYSTEM = system_prompt(walking=False)


@dataclass(frozen=True)
class ReviewResult:
    ok: bool
    rules: tuple[str, ...] = ()
    reason: str = ""
    #: True when the reviewer could not be reached and the message was let
    #: through on the deterministic layer alone.
    unreviewed: bool = False


def review_outbound(
    candidate: str, *, recent: list[str], walking: bool = False
) -> ReviewResult:
    """Second-pass check on one generated message.

    ``recent`` is the tail of the conversation, oldest first, each line already
    prefixed with who said it. It exists so the reviewer can see a distressed
    player, which is invisible in the candidate alone.

    ``walking`` switches which reading of "unsafe" applies. A reviewer holding
    the sit-down rules would refuse every message a walking arc exists to
    send, and a reviewer holding the walking rules on a sit-down arc would let
    through an instruction to go out that nothing in that story should produce.
    """
    transcript = "\n".join(recent[-8:]) or "(no messages yet)"
    user = (
        "Recent conversation:\n"
        f"{transcript}\n\n"
        "Candidate message to send:\n"
        f"<<<{candidate}>>>\n\n"
        "Call the verdict tool."
    )

    try:
        verdict = model.judge(
            system=system_prompt(walking=walking),
            messages=[{"role": "user", "content": user}],
            tool=VERDICT_TOOL,
        )
    except model.ModelUnavailable as exc:
        logs.warn("review.unavailable", error=str(exc), chars=len(candidate))
        return ReviewResult(ok=True, reason=str(exc), unreviewed=True)

    safe = bool(verdict.get("safe"))
    rules = tuple(str(r) for r in verdict.get("rules") or ())
    reason = str(verdict.get("reason") or "").strip()

    # A "safe" verdict that also names rules is a contradiction; treat the
    # named rules as the real answer, because the failure mode of trusting the
    # boolean is a message going out that the reviewer just described as
    # breaking a rule.
    if safe and rules:
        logs.warn("review.contradiction", rules=list(rules), reason=reason)
        safe = False

    if not safe:
        logs.warn("review.refused", rules=list(rules), reason=reason)

    return ReviewResult(ok=safe, rules=rules, reason=reason)
