"""Looking at a photograph, and what the story may say about it.

The rule these exist to protect: **the character describes only what somebody
actually saw.** Left to itself the writer invents detail -- "there is a marking
near the bottom edge, is there not?" about an image it never looked at -- and
the one person able to check is the person holding the photograph. That is not
a cosmetic failure; it is the story telling a player something untrue about
their own afternoon.
"""

from __future__ import annotations

import pytest

from backend.core import engine, model, store, vision
from backend.core.clock import iso
from backend.core.models import EventKind
from backend.story.arc import parse_arc
from tests.test_walk import DAYTIME, WALK


def _walk_player(player, order: int):
    player.arc_id = "walk-test"
    player.arc_started_at = iso(DAYTIME)
    player.beat_order = order
    store.put_player(player)
    store.put_arc(player.player_id, parse_arc(WALK).to_item())
    return player


@pytest.fixture
def photo_walker(player, table):
    """Standing on stop 2, which waits for a photograph."""
    return _walk_player(player, 2)


@pytest.fixture
def arrival_walker(player, table):
    """Standing on stop 1, which waits for a location."""
    return _walk_player(player, 1)


@pytest.fixture
def eyes(monkeypatch, telegram):
    """A fake camera and a fake pair of eyes."""
    state = {
        "look": vision.Look(
            plausible=True, description="A bronze statue of two figures."
        ),
        "downloaded": [],
    }

    def download(file_id, **_kwargs):
        state["downloaded"].append(file_id)
        return b"\xff\xd8\xff\xd9"

    telegram.download = download
    monkeypatch.setattr(vision, "look", lambda *_a, **_k: state["look"])
    return state


def photo(file_id: str = "photo1") -> dict:
    return {"has_photo": True, "has_location": False, "photo_file_id": file_id}


def order_of(player) -> int:
    return store.get_player(player.player_id).beat_order


# ------------------------------------------------------------- stripping EXIF


def segment(marker: int, payload: bytes) -> bytes:
    """A JPEG segment with a correct length field, which counts itself."""
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


QUANT = segment(0xDB, b"quantisation-table")
SCAN = b"\xff\xda" + b"the-actual-picture"


def test_a_jpeg_keeps_everything_needed_to_decode_it() -> None:
    jpeg = b"\xff\xd8" + QUANT + SCAN
    assert vision.strip_exif(jpeg) == jpeg


def test_exif_is_removed() -> None:
    """A phone photograph carries GPS in APP1. It must not leave the Lambda."""
    jpeg = b"\xff\xd8" + segment(0xE1, b"Exif\x00\x00GPSDATA") + QUANT + SCAN

    stripped = vision.strip_exif(jpeg)

    assert b"GPSDATA" not in stripped
    assert b"Exif" not in stripped
    assert stripped.endswith(b"the-actual-picture"), "the image survives"
    assert QUANT in stripped, "so does everything needed to decode it"


def test_a_comment_segment_goes_too() -> None:
    jpeg = b"\xff\xd8" + segment(0xFE, b"secret") + QUANT + SCAN
    assert b"secret" not in vision.strip_exif(jpeg)


def test_every_app_segment_goes() -> None:
    """XMP and vendor blocks live across the whole APPn range, not just APP1."""
    jpeg = (
        b"\xff\xd8"
        + segment(0xE0, b"JFIF-ish")
        + segment(0xE2, b"ICC")
        + segment(0xEF, b"vendor-junk")
        + QUANT
        + SCAN
    )

    stripped = vision.strip_exif(jpeg)

    assert b"vendor-junk" not in stripped
    assert b"ICC" not in stripped


def test_something_that_is_not_a_jpeg_is_refused() -> None:
    """Refusing to look beats sending bytes we could not verify are clean."""
    assert vision.strip_exif(b"\x89PNG\r\n\x1a\n" + b"whatever") is None


def test_a_jpeg_that_does_not_parse_is_refused_rather_than_passed_through() -> None:
    """The dangerous fallback is returning the original, GPS and all.

    A lying segment length is exactly how that happens: the walk loses its
    place, and "give up and send what we were given" would ship the metadata
    this function exists to remove.
    """
    lying = b"\xff\xd8" + b"\xff\xe1\xff\xf0" + b"Exif\x00\x00GPSDATA"
    assert vision.strip_exif(lying) is None


def test_a_jpeg_that_never_reaches_its_image_data_is_refused() -> None:
    assert vision.strip_exif(b"\xff\xd8") is None


def test_an_unstrippable_image_is_never_sent_to_the_model(story) -> None:
    result = vision.look(b"\x89PNG\r\n\x1a\n" + b"whatever", target="anywhere")

    assert result.unavailable
    assert story.reviews == []


# --------------------------------------------------------------- the gate


def test_a_plausible_photo_advances_the_stop(
    photo_walker, telegram, frozen, eyes, story
) -> None:
    frozen(DAYTIME)
    engine.respond(photo_walker, photo())

    assert order_of(photo_walker) == 3


def test_a_photo_of_something_else_does_not(
    photo_walker, telegram, frozen, eyes, story
) -> None:
    """A picture of a desk is not the picture they were asked for."""
    frozen(DAYTIME)
    eyes["look"] = vision.Look(
        plausible=False, description="A laptop and a mug on a desk indoors."
    )

    engine.respond(photo_walker, photo())

    assert order_of(photo_walker) == 2


def test_a_rejected_photo_is_flagged_for_the_operator(
    photo_walker, telegram, frozen, eyes, story
) -> None:
    frozen(DAYTIME)
    eyes["look"] = vision.Look(plausible=False, description="A cat.")

    engine.respond(photo_walker, photo())

    flagged = [
        e for e in store.timeline(photo_walker.player_id) if e.get("needs_review")
    ]
    assert flagged, "a confused player is worth a human glance"


def test_eyes_that_cannot_open_let_the_player_through(
    photo_walker, telegram, frozen, eyes, story
) -> None:
    """A timeout must not strand somebody who did exactly what was asked."""
    frozen(DAYTIME)
    eyes["look"] = vision.Look(plausible=False, description="", unavailable=True)

    engine.respond(photo_walker, photo())

    assert order_of(photo_walker) == 3


def test_a_failed_download_still_lets_them_through(
    photo_walker, telegram, frozen, story
) -> None:
    def broken(*_a, **_k):
        raise RuntimeError("telegram said no")

    telegram.download = broken

    frozen(DAYTIME)
    engine.respond(photo_walker, photo())

    assert order_of(photo_walker) == 3


def test_a_location_never_satisfies_a_photo_stop(
    photo_walker, telegram, frozen, eyes, story
) -> None:
    frozen(DAYTIME)
    engine.respond(photo_walker, {"has_photo": False, "has_location": True})

    assert order_of(photo_walker) == 2


def test_nothing_is_looked_at_on_an_arrival_stop(
    arrival_walker, telegram, frozen, eyes, story
) -> None:
    """Stop 1 waits for a location; a photo there is not worth a model call."""
    frozen(DAYTIME)
    engine.respond(arrival_walker, photo())

    assert eyes["downloaded"] == []


# ------------------------------------------------- what the story may say


def test_the_writer_is_given_what_was_actually_seen(
    photo_walker, telegram, frozen, eyes, story
) -> None:
    frozen(DAYTIME)
    eyes["look"] = vision.Look(
        plausible=True, description="Two bronze figures pressed into a tin shape."
    )

    engine.respond(photo_walker, photo())

    direction = story.last_messages[-1]["content"]
    assert "Two bronze figures pressed into a tin shape." in direction
    assert "Do not add details that are not in that description" in direction


def test_the_writer_is_told_it_is_blind_when_nobody_looked(
    photo_walker, telegram, frozen, story, monkeypatch
) -> None:
    """The bug that produced an invented inscription in a real run."""
    monkeypatch.setattr(
        vision,
        "look",
        lambda *_a, **_k: vision.Look(plausible=True, description="", unavailable=True),
    )

    frozen(DAYTIME)
    engine.respond(photo_walker, photo())

    direction = story.last_messages[-1]["content"]
    assert "could not see it" in direction
    assert "do not imply you looked" in direction


def test_a_person_in_the_frame_is_never_described(
    photo_walker, telegram, frozen, eyes, story
) -> None:
    """The player photographs things, but a passer-by wanders into shot."""
    frozen(DAYTIME)
    eyes["look"] = vision.Look(
        plausible=True, description="A stone memorial.", people_visible=True
    )

    engine.respond(photo_walker, photo())

    assert "do not describe them" in story.last_messages[-1]["content"]


def test_the_description_is_kept_on_the_timeline(
    photo_walker, telegram, frozen, eyes, story
) -> None:
    frozen(DAYTIME)
    engine.respond(photo_walker, photo())

    photos = [
        e
        for e in store.timeline(photo_walker.player_id)
        if e["kind"] == EventKind.PHOTO
    ]
    assert photos[-1]["text"] == "A bronze statue of two figures."
    assert photos[-1]["plausible"] is True


# --------------------------------------------------------------- the module


def test_an_empty_image_is_not_sent_to_the_model(story) -> None:
    result = vision.look(b"", target="anywhere")

    assert result.unavailable
    assert story.reviews == []


def test_an_oversized_image_is_refused_before_the_request(story) -> None:
    result = vision.look(b"\xff\xd8" + b"x" * vision.MAX_IMAGE_BYTES, target="anywhere")

    assert result.unavailable
    assert story.reviews == []


def test_an_unreachable_model_reports_unavailable(story) -> None:
    story.review_error = model.ModelUnavailable("timeout")

    result = vision.look(b"\xff\xd8" + QUANT + SCAN, target="anywhere")

    assert result.unavailable
    assert result.plausible, "an unseen photo gets the benefit of the doubt"


def test_the_image_reaches_the_model_with_the_target_named(story) -> None:
    vision.look(b"\xff\xd8" + QUANT + SCAN, target="A Couple in a Sardine Can")

    sent = story.reviews[-1]["messages"][0]["content"]
    assert any(part.get("type") == "image" for part in sent)
    assert any(
        "A Couple in a Sardine Can" in str(part.get("text", "")) for part in sent
    )


def test_the_verdict_does_not_demand_the_specific_place() -> None:
    """A model cannot know what one local sculpture looks like.

    Asking it to confirm one would refuse honest photographs all day, and a
    gate that refuses honest work gets switched off.
    """
    schema = vision.LOOK_TOOL["input_schema"]["properties"]["plausible"]
    assert "Do NOT require it to be the specific named place" in schema["description"]
