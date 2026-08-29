"""Per-environment configuration for the Adventure Agent stacks.

Two environments only, per the build spec: ``dev`` and ``prod``. Nothing here
is a secret -- real credentials live in Secrets Manager and are never
committed.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

PROJECT = "adventure-agent"

# Pinned by the spec: Bedrock Nova Sonic is only available in us-east-1.
REGION = "us-east-1"


@dataclass(frozen=True)
class EnvConfig:
    """Everything that differs between dev and prod."""

    name: str
    stack_name: str

    # Cost guardrail. The budget is account-scoped, so only one environment
    # creates it (see `creates_budget`).
    monthly_budget_usd: int
    creates_budget: bool

    #: Outbound story messages per player per day. Six is the product rule;
    #: dev raises it so a five-stop walk can be tested end to end in one
    #: sitting without waiting for midnight.
    max_messages_per_day: int

    # Data durability. dev is disposable; prod is not.
    retain_data: bool
    point_in_time_recovery: bool
    termination_protection: bool

    @property
    def prefix(self) -> str:
        """Resource-name prefix, e.g. ``adventure-agent-dev``."""
        return f"{PROJECT}-{self.name}"

    @property
    def secret_name(self) -> str:
        return f"{PROJECT}/{self.name}"


ENVIRONMENTS: dict[str, EnvConfig] = {
    "dev": EnvConfig(
        name="dev",
        stack_name="AdventureAgentDev",
        monthly_budget_usd=50,
        creates_budget=True,
        max_messages_per_day=200,
        retain_data=False,
        point_in_time_recovery=False,
        termination_protection=False,
    ),
    "prod": EnvConfig(
        name="prod",
        stack_name="AdventureAgentProd",
        monthly_budget_usd=50,
        # The budget covers the whole account, so it is created once, by dev.
        creates_budget=False,
        max_messages_per_day=6,
        retain_data=True,
        point_in_time_recovery=True,
        termination_protection=True,
    ),
}


def get_env_config(name: str) -> EnvConfig:
    try:
        return ENVIRONMENTS[name]
    except KeyError:
        valid = ", ".join(sorted(ENVIRONMENTS))
        raise SystemExit(f"unknown env {name!r}; expected one of: {valid}") from None


def budget_alert_email() -> str:
    """Where budget alarms are sent.

    Deliberately not hard-coded: read from ``ADVENTURE_BUDGET_EMAIL`` in the
    environment or in the git-ignored ``.env.local``.
    """
    email = os.environ.get("ADVENTURE_BUDGET_EMAIL", "").strip()
    if not email:
        raise SystemExit(
            "ADVENTURE_BUDGET_EMAIL is not set.\n"
            "Copy .env.local.example to .env.local and fill it in "
            "(.env.local is git-ignored)."
        )
    return email


def operator_name() -> str:
    """Who the player is told is running this, in `/real` and STOP replies.

    Never hard-coded: naming a real person or organisation in the repo is
    exactly what the spec forbids. Falls back to a generic phrase with a loud
    synth-time warning, because `/real` naming a placeholder in front of a real
    player would be a safety failure.
    """
    name = os.environ.get("ADVENTURE_OPERATOR_NAME", "").strip()
    if not name:
        print(
            "WARNING: ADVENTURE_OPERATOR_NAME is unset -- /real will say "
            '"the operator". Set it in .env.local before enrolling anyone.',
            file=sys.stderr,
        )
        return "the operator"
    return name


def operator_contact() -> str:
    """How a player reaches a human. Optional, but strongly recommended."""
    return os.environ.get("ADVENTURE_OPERATOR_CONTACT", "").strip()
