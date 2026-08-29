"""Claude on Bedrock: the only place this project talks to a model.

Two call shapes, and the split is forced by the API rather than chosen:
Bedrock rejects ``thinking`` together with a forced ``tool_choice``
("Thinking may not be enabled when tool_choice forces tool use"). So the
writer thinks and returns prose, and the reviewer returns a structured verdict
without thinking. That is the right way round anyway -- writing has to hold
voice, arc and safety at once, while the review is a judgement against an
explicit list.

Model access here is narrower than the docs suggest, and both facts were
checked against the live account rather than assumed:

* This account is not entitled to ``claude-opus-5`` / ``opus-4-8`` /
  ``sonnet-5``; they return 403 on Bedrock. Opus 4.6 is the most capable model
  it can reach.
* The Bedrock Messages endpoint (the SDK's ``AnthropicBedrockMantle`` client)
  only serves those newer ids, so it is unusable here. ``AnthropicBedrock``
  -- the InvokeModel path -- is what works, and it needs the ``us.`` inference
  profile prefix; a bare ``anthropic.claude-opus-4-6-v1`` is rejected for
  on-demand throughput.

Every failure raises :class:`ModelUnavailable`. Callers never see a raw SDK
exception, because the only correct reaction to any of them is the same: fall
back to safe pre-written copy and flag it for the operator.
"""

from __future__ import annotations

import os
from typing import Any

from backend.core import config, logs

_client = None


class ModelUnavailable(RuntimeError):
    """Generation or review could not be completed. Always recoverable."""


def client():
    global _client
    if _client is None:
        # Imported lazily: `scripts/` and the tests import this module for its
        # types without ever making a call, and the SDK is a heavy import.
        from anthropic import AnthropicBedrock

        # Timeouts and retries are set per call rather than here: writing and
        # reviewing have very different budgets, and both share the webhook's
        # single 30s ceiling.
        _client = AnthropicBedrock(
            aws_region=os.environ.get("AWS_REGION", config.REGION)
        )
    return _client


def reset_client() -> None:
    """Test hook."""
    global _client
    _client = None


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
    }


def write(
    *,
    system: str,
    messages: list[dict],
    max_tokens: int = config.STORY_MAX_TOKENS,
    effort: str = config.STORY_EFFORT,
    purpose: str = "story",
) -> str:
    """Generate prose. Returns the text of the reply, never an empty string.

    Adaptive thinking is on, with the summary suppressed: the reasoning
    improves the writing, and nothing downstream reads the summary, so paying
    output tokens to render it would be waste.
    """
    try:
        with logs.timed("model.write", purpose=purpose, model=config.STORY_MODEL):
            response = (
                client()
                .with_options(
                    timeout=config.WRITE_TIMEOUT_S, max_retries=config.WRITE_RETRIES
                )
                .messages.create(
                    model=config.STORY_MODEL,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                    thinking={"type": "adaptive", "display": "omitted"},
                    output_config={"effort": effort},
                )
            )
    except Exception as exc:
        raise ModelUnavailable(f"{type(exc).__name__}: {exc}") from exc

    text = "\n".join(
        block.text.strip()
        for block in response.content
        if block.type == "text" and block.text.strip()
    ).strip()

    logs.info(
        "model.usage",
        purpose=purpose,
        model=config.STORY_MODEL,
        stop_reason=response.stop_reason,
        chars=len(text),
        **_usage(response),
    )

    if not text:
        # A refusal or a max_tokens cut with nothing but thinking behind it.
        raise ModelUnavailable(f"empty completion (stop_reason={response.stop_reason})")
    return text


def judge(
    *,
    system: str,
    messages: list[dict],
    tool: dict,
    max_tokens: int = config.REVIEW_MAX_TOKENS,
    effort: str = config.REVIEW_EFFORT,
    timeout: float = config.REVIEW_TIMEOUT_S,
    retries: int = config.REVIEW_RETRIES,
    purpose: str = "review",
) -> dict:
    """Get a structured answer by forcing one tool call. Returns its input.

    Forcing the tool is what makes the result parseable without defensive JSON
    handling -- the API guarantees the shape, so there is no branch here for
    "the model replied in prose instead". It is also why this cannot think:
    Bedrock rejects `thinking` alongside a forced `tool_choice`.

    The budget is a parameter because the two callers live under very
    different ceilings -- the safety review runs inside a webhook that must
    answer in 30s, while composing an arc happens at onboarding and can take a
    minute.
    """
    try:
        with logs.timed("model.judge", purpose=purpose, model=config.REVIEW_MODEL):
            response = (
                client()
                .with_options(timeout=timeout, max_retries=retries)
                .messages.create(
                    model=config.REVIEW_MODEL,
                    max_tokens=max_tokens,
                    system=system,
                    messages=messages,
                    tools=[tool],
                    tool_choice={"type": "tool", "name": tool["name"]},
                    output_config={"effort": effort},
                )
            )
    except Exception as exc:
        raise ModelUnavailable(f"{type(exc).__name__}: {exc}") from exc

    logs.info(
        "model.usage",
        purpose=purpose,
        model=config.REVIEW_MODEL,
        stop_reason=response.stop_reason,
        **_usage(response),
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == tool["name"]:
            return dict(block.input)
    raise ModelUnavailable("forced tool call produced no tool_use block")
