"""Distance, fences, and what the story is allowed to learn from a position.

The privacy property is tested as hard as the arithmetic: everything that
leaves this module has to be too coarse to put the player back on a map.
"""

from __future__ import annotations

import pytest

from backend.core.geo import (
    DEFAULT_FENCE_M,
    MAX_FENCE_M,
    Point,
    clamp_fence,
    compass,
    describe_progress,
    distance_m,
    within,
)

# Two real landmarks in central Tel Aviv, 375m apart.
DIZENGOFF = Point(32.0640, 34.7740)
FOUNDERS = Point(32.0664, 34.7712)


def test_the_distance_to_yourself_is_zero() -> None:
    assert distance_m(DIZENGOFF, DIZENGOFF) == pytest.approx(0, abs=0.01)


def test_a_known_short_distance_is_right() -> None:
    """375m, the walk between two Rothschild Boulevard landmarks."""
    assert distance_m(DIZENGOFF, FOUNDERS) == pytest.approx(375, abs=15)


def test_a_known_long_distance_is_right() -> None:
    """London to Tel Aviv, ~3600km. Catches a radians/degrees mix-up."""
    london = Point(51.5074, -0.1278)
    assert distance_m(london, DIZENGOFF) == pytest.approx(3_580_000, rel=0.02)


def test_distance_is_symmetric() -> None:
    assert distance_m(DIZENGOFF, FOUNDERS) == pytest.approx(
        distance_m(FOUNDERS, DIZENGOFF)
    )


@pytest.mark.parametrize("lat", [91, -91, 180])
def test_an_impossible_latitude_is_rejected(lat) -> None:
    """A swapped lat/lon pair is the classic bug; most longitudes catch it here."""
    with pytest.raises(ValueError, match="latitude"):
        Point(lat, 0)


def test_an_impossible_longitude_is_rejected() -> None:
    with pytest.raises(ValueError, match="longitude"):
        Point(0, 181)


# --------------------------------------------------------------------- fences


def test_standing_on_the_waypoint_is_inside_the_fence() -> None:
    assert within(DIZENGOFF, DIZENGOFF)


def test_a_block_away_is_outside_the_default_fence() -> None:
    assert not within(DIZENGOFF, FOUNDERS)


def test_a_generous_fence_reaches() -> None:
    assert within(DIZENGOFF, FOUNDERS, radius_m=400)


@pytest.mark.parametrize(
    ("asked", "used"),
    [(5, 20.0), (75, 75.0), (5000, MAX_FENCE_M), (-10, 20.0)],
)
def test_a_fence_is_clamped_to_something_meaningful(asked, used) -> None:
    """Arc files are data and can be wrong. A 5m fence is unreachable on GPS;
    a 5km one fires from the next town."""
    assert clamp_fence(asked) == used


def test_a_fence_from_a_bad_arc_file_cannot_fire_across_a_city() -> None:
    assert not within(DIZENGOFF, Point(51.5074, -0.1278), radius_m=10_000_000)


# ------------------------------------------------------------------ direction


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (Point(33.0, 34.7740), "north"),
        (Point(31.0, 34.7740), "south"),
        (Point(32.0640, 35.9), "east"),
        (Point(32.0640, 33.5), "west"),
    ],
)
def test_the_compass_points_the_right_way(target, expected) -> None:
    assert compass(DIZENGOFF, target) == expected


# -------------------------------------------------------------------- privacy


def test_arriving_is_reported_as_arrived() -> None:
    progress = describe_progress(DIZENGOFF, DIZENGOFF, DEFAULT_FENCE_M)
    assert progress["arrived"] is True


def test_being_elsewhere_is_not() -> None:
    progress = describe_progress(DIZENGOFF, FOUNDERS, DEFAULT_FENCE_M)
    assert progress["arrived"] is False


@pytest.mark.parametrize(
    ("metres_north", "band"),
    [(0.0005, "very close"), (0.002, "a short walk"), (0.01, "a fair walk")],
)
def test_distance_is_reported_only_in_bands(metres_north, band) -> None:
    target = Point(DIZENGOFF.lat + metres_north, DIZENGOFF.lon)
    assert (
        describe_progress(DIZENGOFF, target, DEFAULT_FENCE_M)["distance_band"] == band
    )


def test_progress_never_carries_a_coordinate() -> None:
    """This dict reaches the model and the timeline. It must not be invertible."""
    progress = describe_progress(DIZENGOFF, FOUNDERS, DEFAULT_FENCE_M)

    assert set(progress) == {"arrived", "distance_band", "direction"}
    serialised = repr(progress)
    for fragment in ("32.06", "34.77", "lat", "lon"):
        assert fragment not in serialised


def test_progress_carries_no_raw_distance() -> None:
    """An exact distance from two waypoints triangulates a position.

    `arrived` is excluded on purpose -- a bool is the one number this is
    allowed to carry, and in Python a bool *is* an int, which is why the check
    is written this way round.
    """
    progress = describe_progress(DIZENGOFF, FOUNDERS, DEFAULT_FENCE_M)
    numeric = [
        v for k, v in progress.items() if k != "arrived" and isinstance(v, (int, float))
    ]
    assert numeric == []
