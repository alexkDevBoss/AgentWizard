"""Outbound content validation.

Every in-character message passes through here before dispatch. A failure is
not an exception -- it is a refusal: the message is not sent, the violation is
logged and flagged for the operator, and a safe pre-written line goes out
instead.

The rules are the spec's content rules, one pattern group each. They are
deliberately written against *assertions* rather than bare nouns: a regex
cannot tell a fictional death from a claimed real one, but it can catch the
shapes that make a claim feel real -- emergency instructions, authority
claims, credential requests, and unsafe directives.

Phase 3 adds a model-based second pass on top of this for the things regex
cannot see. This layer stays regardless, because it cannot itself be talked
out of its rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

MAX_MESSAGE_CHARS = 1200


class Rule(StrEnum):
    REAL_EMERGENCY = "real_emergency"
    IMPERSONATION = "impersonation"
    CREDENTIAL_OR_MONEY = "credential_or_money"
    UNSAFE_INSTRUCTION = "unsafe_instruction"
    DENIES_FICTION = "denies_fiction"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class Violation:
    rule: Rule
    detail: str
    excerpt: str


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    violations: tuple[Violation, ...] = ()

    @property
    def rule_ids(self) -> list[str]:
        return [str(v.rule) for v in self.violations]


# Each entry: (rule, human description, pattern)
_RULES: tuple[tuple[Rule, str, str], ...] = (
    # --- never claim a real emergency, injury, death, crime, or medical event
    (
        Rule.REAL_EMERGENCY,
        "instructs the player to contact emergency services",
        r"\b(call|dial|ring)\s+(9-?1-?1|1-?1-?2|9-?9-?9|101|the\s+(police|ambulance|paramedics|fire\s+brigade))\b",
    ),
    (
        Rule.REAL_EMERGENCY,
        "asserts a live medical emergency",
        r"\b(i'?m|he'?s|she'?s|they'?re)\s+(bleeding|dying|having a (heart attack|stroke|seizure))\b",
    ),
    (
        Rule.REAL_EMERGENCY,
        "asserts a real death or killing",
        r"\b(is|was|has been|been)\s+(murdered|killed|shot dead)\b|\bhe'?s dead\b|\bshe'?s dead\b|\bthey'?re dead\b",
    ),
    (
        Rule.REAL_EMERGENCY,
        "frames the situation as a genuine emergency",
        r"\bthis is (a real|an actual|not a drill)\b|\breal emergency\b|\bcall for help now\b",
    ),
    # --- never impersonate authority, institutions, or real people
    (
        Rule.IMPERSONATION,
        "claims to be law enforcement or government",
        r"\b(this is|i'?m|i am|we are|speaking (from|for))\s+(the\s+)?(police|detective|officer|fbi|cia|mi5|interpol|border (force|patrol)|immigration|customs|the (government|authorities)|homeland security)\b",
    ),
    (
        Rule.IMPERSONATION,
        "claims to be a bank, hospital, or other institution",
        r"\b(this is|i'?m|i am|we are|calling|writing)\s+(from\s+)?(your\s+)?(bank|hospital|clinic|insurer|tax office|hmrc|irs|social security)\b",
    ),
    (
        Rule.IMPERSONATION,
        "presents itself as an official notice",
        r"\b(official (notice|warning|summons)|you are under (arrest|investigation)|warrant (has been )?issued)\b",
    ),
    # --- never request money, credentials, ID, or account access
    (
        Rule.CREDENTIAL_OR_MONEY,
        "requests credentials or account access",
        r"\b(send|give|share|enter|confirm|verify|provide|tell me)\b[^.?!]{0,40}\b(password|passcode|pin|otp|one-?time code|2fa|login|credentials?|account (details|number)|sort code|routing number)\b",
    ),
    (
        Rule.CREDENTIAL_OR_MONEY,
        "requests identity documents",
        r"\b(send|upload|photograph|scan|show me)\b[^.?!]{0,40}\b(passport|driving licence|driver'?s license|id card|identity card|national insurance|social security)\b",
    ),
    (
        Rule.CREDENTIAL_OR_MONEY,
        "requests money or payment",
        r"\b(send|transfer|wire|pay|deposit|venmo|paypal|zelle|bitcoin|crypto|gift card)\b[^.?!]{0,30}(\$|£|€|\d)|\b(send|transfer|wire) (me|us) (some )?(money|cash|funds)\b",
    ),
    # --- never direct the player somewhere unsafe
    (
        Rule.UNSAFE_INSTRUCTION,
        "directs the player onto private property",
        r"\b(break in(to)?|climb (the|over)|force (the|open)|sneak (in|into|past)|get inside)\b|\b(enter|go into)\b[^.?!]{0,20}\b(private|abandoned|derelict|restricted|no.entry)\b",
    ),
    (
        Rule.UNSAFE_INSTRUCTION,
        "sends the player out at night",
        r"\b(go|head|come|drive|walk|meet me)\b[^.?!]{0,40}\b(at night|after dark|at midnight|at \d{1,2}\s?(am|pm)\b)",
    ),
    (
        Rule.UNSAFE_INSTRUCTION,
        "directs the player to approach a stranger",
        r"\b(approach|follow|confront|talk to|speak to|go up to)\b[^.?!]{0,30}\b(the (man|woman|person|stranger|driver)|someone|a stranger|anyone)\b",
    ),
    (
        Rule.UNSAFE_INSTRUCTION,
        "asks the player to photograph another person",
        r"\b(photo(graph)?|picture|snap|shoot|film|record)\b[^.?!]{0,30}\b(of )?(the|that|a)\s+(man|woman|person|stranger|kid|child|driver|guard|them|him|her)\b",
    ),
    # --- never claim the fiction is real
    (
        Rule.DENIES_FICTION,
        "denies that the story is fiction",
        r"\b(this|it) is( not| isn'?t) (a game|fiction|fictional|pretend|made up|a story)\b|\bi'?m not (an ai|a bot|fictional|made up)\b|\bi'?m a real (person|human)\b|\bthis is real\b",
    ),
)

_COMPILED = tuple(
    (rule, detail, re.compile(pattern, re.IGNORECASE))
    for rule, detail, pattern in _RULES
)


def validate_outbound(text: str) -> ValidationResult:
    """Check one outbound in-character message against every content rule.

    Returns every violation found, not just the first, so the admin panel can
    show the operator the whole picture in one pass.
    """
    violations: list[Violation] = []

    stripped = (text or "").strip()
    if not stripped:
        violations.append(Violation(Rule.MALFORMED, "message is empty", ""))
    elif len(stripped) > MAX_MESSAGE_CHARS:
        violations.append(
            Violation(
                Rule.MALFORMED,
                f"message is {len(stripped)} chars, over the {MAX_MESSAGE_CHARS} cap",
                stripped[:80],
            )
        )

    for rule, detail, pattern in _COMPILED:
        match = pattern.search(stripped)
        if match:
            violations.append(Violation(rule, detail, match.group(0)[:120]))

    return ValidationResult(ok=not violations, violations=tuple(violations))
