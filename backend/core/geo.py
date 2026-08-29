"""Distance and geofencing.

Pure functions over coordinates, with no I/O and no storage, because of where
they get used: a player's coordinates arrive in the Lambda, are compared
against a waypoint here, and are then thrown away. Only the boolean and the
waypoint's own label are ever written down.

That is the spec's rule -- "GPS coordinates are never persisted; the geofence
is evaluated in the Lambda and only a boolean plus a place label is stored" --
and keeping the arithmetic in a module that cannot write anything is what
makes it hard to break by accident.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: Mean Earth radius, metres. Good to ~0.5% at any distance we care about.
EARTH_RADIUS_M = 6_371_008.8

#: How close counts as "there". A GPS fix from a phone in a street is good to
#: roughly 5-20m, worse between tall buildings, so a fence much tighter than
#: this fails for reasons the player cannot see or fix.
DEFAULT_FENCE_M = 75

#: A fence larger than this stops meaning "at the thing" and starts meaning
#: "in the neighbourhood", which makes for a bad puzzle.
MAX_FENCE_M = 400


@dataclass(frozen=True)
class Point:
    lat: float
    lon: float

    def __post_init__(self) -> None:
        if not -90 <= self.lat <= 90:
            raise ValueError(f"latitude out of range: {self.lat}")
        if not -180 <= self.lon <= 180:
            raise ValueError(f"longitude out of range: {self.lon}")


def distance_m(a: Point, b: Point) -> float:
    """Great-circle distance in metres (haversine).

    Haversine rather than a flat-earth approximation: the error is negligible
    either way at street scale, but the flat version needs a cos(latitude)
    correction that is easy to forget and silently wrong at high latitudes.
    """
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def within(player: Point, target: Point, radius_m: float = DEFAULT_FENCE_M) -> bool:
    """Is the player inside the fence around this waypoint?"""
    return distance_m(player, target) <= clamp_fence(radius_m)


def clamp_fence(radius_m: float) -> float:
    """Keep a fence inside the range where it means anything.

    Arc files are data and can be wrong; a 5m fence would be unreachable and a
    5km one would fire from the next town.
    """
    return max(20.0, min(float(radius_m), MAX_FENCE_M))


def bearing_deg(a: Point, b: Point) -> float:
    """Initial compass bearing from ``a`` to ``b``, degrees clockwise from north."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlon = math.radians(b.lon - a.lon)
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(
        dlon
    )
    return (math.degrees(math.atan2(y, x)) + 360) % 360


_COMPASS = (
    "north",
    "north-east",
    "east",
    "south-east",
    "south",
    "south-west",
    "west",
    "north-west",
)


def compass(a: Point, b: Point) -> str:
    """A spoken direction, for a hint that does not read out coordinates."""
    return _COMPASS[int((bearing_deg(a, b) + 22.5) % 360 // 45)]


def describe_progress(player: Point, target: Point, radius_m: float) -> dict:
    """What the story is allowed to know about where the player is.

    Deliberately lossy. It answers "are they there yet, and roughly how far" --
    never "where are they". This dict is what reaches the model and what goes
    on the timeline; the coordinates that produced it do not survive the call.
    """
    metres = distance_m(player, target)
    return {
        "arrived": metres <= clamp_fence(radius_m),
        "distance_band": _band(metres),
        "direction": compass(player, target),
    }


def _band(metres: float) -> str:
    """Coarse distance. A band cannot be triangulated back into a position."""
    if metres <= 100:
        return "very close"
    if metres <= 400:
        return "a short walk"
    if metres <= 1500:
        return "a fair walk"
    if metres <= 5000:
        return "across town"
    return "far away"
