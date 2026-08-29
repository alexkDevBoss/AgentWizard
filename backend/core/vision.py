"""Looking at a photograph the player sent.

Two jobs, and the second is the one that matters. The first is a gate: a photo
stop should not be satisfied by a picture of somebody's desk. The second is
truthfulness -- without a real description the writer invents one, and a
character that says "there is a marking near the bottom edge, is there not?"
about a photograph it never saw is lying to the player in a way the player can
immediately catch.

**EXIF is stripped before the bytes go anywhere.** The spec's rule is that
coordinates never enter the system, and a photograph taken on a phone carries
them in its metadata. Telegram re-encodes images sent as photos and usually
drops it, but "usually" is not a guarantee to build a privacy rule on.

The verdict is deliberately lenient about *what* the photo shows. A model
cannot know what a particular local sculpture looks like, so asking it to
confirm one would refuse honest photographs constantly. It is asked something
it can actually answer: is this plausibly a photograph of the kind of thing
they were sent to photograph, taken outdoors, rather than something unrelated.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from backend.core import config, logs, model

#: Telegram will not send us anything near this, but the Bedrock request has a
#: size limit and a truncated upload should fail here rather than there.
MAX_IMAGE_BYTES = 4_500_000

LOOK_TOOL = {
    "name": "looked",
    "description": "Record what is actually in the photograph. Call once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "plausible": {
                "type": "boolean",
                "description": (
                    "True if this is plausibly a photograph taken outdoors of a "
                    "sculpture, monument, building, sign, artwork or view -- the "
                    "kind of thing someone on a walking trail would be asked to "
                    "photograph. False for a screenshot, a photo of a screen, an "
                    "indoor room, a pet, a selfie, or a picture of nothing in "
                    "particular. Do NOT require it to be the specific named "
                    "place: you cannot know what that looks like."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "What is actually visible, in two or three plain sentences. "
                    "Concrete and factual: materials, shapes, colours, what the "
                    "figures are doing, any legible text. This is the only thing "
                    "the storyteller will know about the image, so do not "
                    "speculate and do not flatter it. If it is too dark or "
                    "blurred to tell, say that."
                ),
            },
            "people_visible": {
                "type": "boolean",
                "description": (
                    "True if any recognisable person appears in the frame, "
                    "including the photographer's reflection."
                ),
            },
        },
        "required": ["plausible", "description", "people_visible"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class Look:
    plausible: bool
    description: str
    people_visible: bool = False
    #: True when the image could not be looked at. Treated as "let it through"
    #: for the same reason the safety reviewer is: a failing call must not
    #: strand a player who did what they were asked.
    unavailable: bool = False


def strip_exif(data: bytes) -> bytes | None:
    """Remove JPEG metadata segments, including any GPS tags.

    Returns ``None`` when the result cannot be guaranteed clean -- anything
    that is not a JPEG, or a JPEG whose segment structure does not parse. That
    is the important half of the contract. The obvious alternative, handing
    back the original bytes when parsing fails, quietly ships the very GPS
    tags this exists to remove; declining to look at a photograph is a far
    smaller failure than leaking where it was taken.

    Written by hand rather than with Pillow: this is the only image processing
    in the project, and a ~30MB dependency in every Lambda bundle to delete a
    few bytes is a bad trade. Telegram re-encodes anything sent as a photo to
    JPEG, so the narrow format support costs nothing on the path that matters.
    """
    if not data.startswith(b"\xff\xd8"):
        return None

    out = bytearray(b"\xff\xd8")
    i = 2
    end = len(data)
    while i < end - 1:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        # Start of scan: the rest is entropy-coded image data, copy verbatim.
        if marker == 0xDA:
            out += data[i:]
            return bytes(out)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            out += data[i : i + 2]
            i += 2
            continue
        if i + 4 > end:
            return None
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        # A length that does not fit means the file is truncated, or this is
        # not where a segment really starts. Either way the walk is lost.
        if length < 2 or i + 2 + length > end:
            return None
        # APPn (0xE0-0xEF) carries EXIF, GPS and XMP; COM is a free-text
        # comment. Neither is needed to decode the image.
        if not (0xE0 <= marker <= 0xEF or marker == 0xFE):
            out += data[i : i + 2 + length]
        i += 2 + length

    # Ran out of file without ever reaching the image data.
    return None


def look(image: bytes, *, target: str) -> Look:
    """Describe one photograph. Never raises."""
    if not image:
        return Look(plausible=False, description="", unavailable=True)
    if len(image) > MAX_IMAGE_BYTES:
        logs.warn("vision.too_large", bytes=len(image))
        return Look(plausible=True, description="", unavailable=True)

    clean = strip_exif(image)
    if clean is None:
        # Declining to look is the safe failure: the alternative is sending an
        # image whose metadata could not be verified clean.
        logs.warn("vision.unstrippable", bytes=len(image))
        return Look(plausible=True, description="", unavailable=True)
    logs.info("vision.stripped", before=len(image), after=len(clean))

    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(clean).decode("ascii"),
            },
        },
        {
            "type": "text",
            "text": (
                f"The player was asked to photograph something at {target}. "
                "Look at what they actually sent and call the looked tool."
            ),
        },
    ]

    try:
        verdict = model.judge(
            system=(
                "You are describing a photograph for a storyteller who cannot "
                "see it. Be accurate and plain. The storyteller will repeat "
                "what you say to the person who took the picture, so an "
                "invented detail will be caught immediately."
            ),
            messages=[{"role": "user", "content": content}],
            tool=LOOK_TOOL,
            max_tokens=config.VISION_MAX_TOKENS,
            effort=config.VISION_EFFORT,
            timeout=config.VISION_TIMEOUT_S,
            retries=config.VISION_RETRIES,
            purpose="vision",
        )
    except model.ModelUnavailable as exc:
        logs.warn("vision.unavailable", error=str(exc))
        return Look(plausible=True, description="", unavailable=True)

    result = Look(
        plausible=bool(verdict.get("plausible")),
        description=str(verdict.get("description") or "").strip(),
        people_visible=bool(verdict.get("people_visible")),
    )
    logs.info(
        "vision.looked",
        plausible=result.plausible,
        people_visible=result.people_visible,
        chars=len(result.description),
    )
    return result
