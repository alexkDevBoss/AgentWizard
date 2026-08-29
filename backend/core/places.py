"""Real places near a point, from OpenStreetMap.

A location-based adventure cannot invent its waypoints. "The fountain on the
corner" has to be a fountain that exists, has a name the player can recognise,
and is somewhere they can legally stand in daylight. This module asks Overpass
for candidates; :mod:`backend.core.arcsmith` picks between them and writes the
story around the ones it picks.

Chosen over the Google Places API because it is free and needs no key, which
keeps every cost on the AWS bill where the budget alarm can see it. The
trade-off is coverage: Overpass is excellent in a city centre and thin in a
suburb. :func:`find_nearby` returning too few candidates is therefore a normal
outcome the caller has to handle, not an error.

**The origin is used and discarded.** It is the player's own position, so it is
passed in, turned into a list of public places, and never returned or stored.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from backend.core import logs
from backend.core.geo import DEFAULT_FENCE_M, Point, distance_m

#: Public instances, tried in order. Overpass is a donated service with a
#: fair-use policy; one arc generation makes one query, which is well inside
#: it, but a mirror keeps a single instance's downtime from blocking onboarding.
ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

USER_AGENT = "adventure-agent/1.0 (interactive fiction; contact via operator)"
TIMEOUT_S = 25

#: Categories worth walking to, scored by how well each makes a waypoint.
#: A named memorial is a landmark somebody can find and photograph; a chain
#: coffee shop is a place, but it is not a destination in a story.
_CATEGORIES: dict[str, dict[str, int]] = {
    "tourism": {
        "artwork": 10,
        "memorial": 9,
        "viewpoint": 9,
        "museum": 8,
        "gallery": 7,
        "attraction": 7,
    },
    "historic": {
        "monument": 10,
        "memorial": 9,
        "archaeological_site": 8,
        "ruins": 8,
        "castle": 8,
        "wayside_cross": 7,
        "building": 6,
    },
    "amenity": {
        "fountain": 10,
        "clock": 9,
        "bandstand": 9,
        "marketplace": 8,
        "theatre": 8,
        "arts_centre": 7,
        "library": 7,
    },
    "man_made": {
        "obelisk": 10,
        "lighthouse": 10,
        "tower": 8,
        "water_tower": 8,
    },
    "leisure": {
        "park": 7,
        "garden": 7,
        "common": 6,
        "playground": 4,
    },
}


class PlacesUnavailable(RuntimeError):
    """Overpass could not be reached. Onboarding should retry, not invent places."""


@dataclass(frozen=True)
class Waypoint:
    """One real, named, publicly reachable place."""

    osm_id: str
    name: str
    kind: str
    lat: float
    lon: float
    #: Metres from the origin the search was centred on. Used for ordering a
    #: walkable route; never stored against the player.
    distance_m: float = 0.0
    fence_m: int = DEFAULT_FENCE_M
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def point(self) -> Point:
        return Point(self.lat, self.lon)

    def to_item(self) -> dict:
        return {
            "osm_id": self.osm_id,
            "name": self.name,
            "kind": self.kind,
            "lat": self.lat,
            "lon": self.lon,
            "fence_m": self.fence_m,
        }

    def render(self) -> str:
        """One line for a prompt. Coordinates are deliberately absent.

        The writer needs to know what the place *is*, not where it is -- and a
        model that has been shown coordinates will eventually write them out.
        """
        return f"{self.name} ({self.kind.replace('_', ' ')})"


def _query(origin: Point, radius_m: int) -> str:
    clauses = []
    for key, values in _CATEGORIES.items():
        alternatives = "|".join(values)
        clauses.append(
            f'  nwr["name"]["{key}"~"^({alternatives})$"]'
            f"(around:{radius_m},{origin.lat},{origin.lon});"
        )
    body = "\n".join(clauses)
    # `out center` gives ways and relations a centroid, so a park comes back as
    # a single point rather than an outline nobody can stand "at".
    return f"[out:json][timeout:{TIMEOUT_S}];\n(\n{body}\n);\nout center 120;"


def _post(url: str, query: str) -> dict:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        return json.loads(response.read().decode("utf-8"))


def _element_to_waypoint(element: dict, origin: Point) -> Waypoint | None:
    tags = element.get("tags") or {}
    name = (tags.get("name") or "").strip()
    if not name:
        return None

    # `center` for ways and relations, plain lat/lon for nodes.
    centre = element.get("center") or element
    lat, lon = centre.get("lat"), centre.get("lon")
    if lat is None or lon is None:
        return None

    kind = next(
        (
            f"{key}:{tags[key]}"
            for key, values in _CATEGORIES.items()
            if tags.get(key) in values
        ),
        None,
    )
    if kind is None:
        return None

    return Waypoint(
        osm_id=f"{element.get('type', 'node')}/{element.get('id')}",
        name=name,
        kind=kind.split(":", 1)[1],
        lat=float(lat),
        lon=float(lon),
        distance_m=round(distance_m(origin, Point(float(lat), float(lon))), 1),
        tags={k: v for k, v in tags.items() if k in _CATEGORIES or k == "name"},
    )


def _score(waypoint: Waypoint) -> float:
    """Rank by how good a destination it is, then by how close it is.

    Distance is a tiebreak rather than the main term: a memorial 600m away
    makes a better waypoint than a playground next door.
    """
    base = 0
    for values in _CATEGORIES.values():
        if waypoint.kind in values:
            base = max(base, values[waypoint.kind])
    return base - (waypoint.distance_m / 1000.0)


def find_nearby(
    origin: Point, *, radius_m: int = 1200, limit: int = 25
) -> list[Waypoint]:
    """Named, public, walkable places near ``origin``, best first.

    ``radius_m`` defaults to a comfortable walk. Returns an empty list rather
    than raising when the area genuinely has nothing mapped -- that is a real
    answer about a real place, and the caller has to say so to the customer.
    """
    query = _query(origin, radius_m)
    last: Exception | None = None

    for url in ENDPOINTS:
        try:
            with logs.timed("places.overpass", endpoint=url, radius_m=radius_m):
                payload = _post(url, query)
            break
        # Deliberately broad. Overpass is a donated public service that sheds
        # load by hanging up mid-request, which arrives as
        # http.client.RemoteDisconnected -- not a URLError, and so not caught
        # by the obvious except clause. Anything that stops this endpoint
        # answering should move on to the mirror rather than crash onboarding.
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            OSError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            last = exc
            logs.warn("places.endpoint_failed", endpoint=url, error=str(exc))
    else:
        raise PlacesUnavailable(f"no Overpass endpoint answered: {last}")

    seen: set[str] = set()
    found: list[Waypoint] = []
    for element in payload.get("elements", []):
        waypoint = _element_to_waypoint(element, origin)
        if waypoint is None or waypoint.name.lower() in seen:
            continue
        seen.add(waypoint.name.lower())
        found.append(waypoint)

    found.sort(key=_score, reverse=True)
    varied = _diversify(found, limit)
    logs.info(
        "places.found",
        candidates=len(found),
        returned=len(varied),
        kinds=sorted({w.kind for w in varied}),
        radius_m=radius_m,
    )
    return varied


#: How many of any one kind may appear in the candidate list.
#:
#: Without this the whole list comes back as `artwork`, because a city centre
#: has forty statues and they all outrank a park on category score. A route of
#: five statues is a worse walk than a statue, a fountain, a park and a museum,
#: and the generator can only pick from what it is given.
MAX_PER_KIND = 3


def _diversify(ranked: list[Waypoint], limit: int) -> list[Waypoint]:
    """Best-first, but no more than ``MAX_PER_KIND`` of any one category."""
    counts: dict[str, int] = {}
    kept: list[Waypoint] = []
    overflow: list[Waypoint] = []

    for waypoint in ranked:
        if counts.get(waypoint.kind, 0) < MAX_PER_KIND:
            counts[waypoint.kind] = counts.get(waypoint.kind, 0) + 1
            kept.append(waypoint)
        else:
            overflow.append(waypoint)

    # Backfill from the overflow rather than return a short list: a thin area
    # with only statues should still produce a usable route.
    return (kept + overflow)[:limit]
