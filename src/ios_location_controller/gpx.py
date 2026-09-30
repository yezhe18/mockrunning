from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import asin, atan2, cos, degrees, hypot, isfinite, radians, sin, sqrt
from pathlib import Path
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class Point:
    latitude: float
    longitude: float
    timestamp: datetime | None = None


EARTH_RADIUS_M = 6_371_000.0


def distance_meters(a: Point, b: Point) -> float:
    """Return the great-circle distance between two GPS points."""
    lat1, lat2 = radians(a.latitude), radians(b.latitude)
    dlat = lat2 - lat1
    dlon = radians(b.longitude - a.longitude)
    hav = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(min(1.0, sqrt(hav)))


def interpolate(a: Point, b: Point, fraction: float) -> Point:
    """Interpolate on the short great-circle arc, including the date line."""
    fraction = max(0.0, min(1.0, fraction))
    if fraction == 0:
        return a
    if fraction == 1:
        return b
    angle = distance_meters(a, b) / EARTH_RADIUS_M
    if angle < 1e-12:
        return Point(a.latitude, a.longitude)
    if abs(sin(angle)) < 1e-10:
        raise ValueError("Antipodal route nodes have no unique short arc")
    weights = sin((1 - fraction) * angle) / sin(angle), sin(fraction * angle) / sin(angle)
    xyz = []
    for point in (a, b):
        lat, lon = radians(point.latitude), radians(point.longitude)
        xyz.append((cos(lat) * cos(lon), cos(lat) * sin(lon), sin(lat)))
    x, y, z = (weights[0] * xyz[0][i] + weights[1] * xyz[1][i] for i in range(3))
    return Point(degrees(atan2(z, hypot(x, y))), degrees(atan2(y, x)))


def offset_point(point: Point, north_m: float, east_m: float) -> Point:
    """Apply a local offset as a spherical destination; wrap longitude."""
    angle = hypot(north_m, east_m) / EARTH_RADIUS_M
    if angle == 0:
        return point
    bearing = atan2(east_m, north_m)
    lat, lon = radians(point.latitude), radians(point.longitude)
    target_lat = asin(max(-1.0, min(1.0, sin(lat) * cos(angle) + cos(lat) * sin(angle) * cos(bearing))))
    target_lon = lon + atan2(sin(bearing) * sin(angle) * cos(lat), cos(angle) - sin(lat) * sin(target_lat))
    return Point(degrees(target_lat), (degrees(target_lon) + 180) % 360 - 180)


def bearing_radians(a: Point, b: Point) -> float:
    """Return the initial bearing from a to b in radians."""
    lat1, lat2 = radians(a.latitude), radians(b.latitude)
    dlon = radians(b.longitude - a.longitude)
    return atan2(sin(dlon) * cos(lat2), cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dlon))


def parse_points(text: str) -> list[Point]:
    """Common GPX parser for the CLI and web import; keep recorded times."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > 2_000_000:
        raise ValueError("GPX exceeds 2 MB")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("XML entities are not supported")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid GPX XML: {exc}") from exc
    tag = lambda node: node.tag.rsplit("}", 1)[-1]
    tracks = [n for n in root.iter() if tag(n) == "trkseg" and any(tag(c) == "trkpt" for c in n)]
    routes = [n for n in root.iter() if tag(n) == "rte" and any(tag(c) == "rtept" for c in n)]
    kind = "trkpt" if any(tag(n) == "trkpt" for n in root.iter()) else "rtept"
    if len(tracks if kind == "trkpt" else routes) > 1:
        raise ValueError("Choose a single GPX segment; disconnected segments cannot be joined automatically")
    points: list[Point] = []
    for node in root.iter():
        if tag(node) != kind:
            continue
        try:
            lat, lon = float(node.attrib["lat"]), float(node.attrib["lon"])
        except (KeyError, ValueError) as exc:
            raise ValueError("GPX node must have numeric lat/lon attributes") from exc
        if not isfinite(lat) or not isfinite(lon) or not -85 <= lat <= 85 or not -180 <= lon <= 180:
            raise ValueError(f"Invalid coordinate: {lat}, {lon}")
        stamp = next((child.text for child in node if tag(child) == "time"), None)
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp else None
        points.append(Point(lat, lon, parsed))
        if len(points) > 10000:
            raise ValueError("GPX exceeds 10000 nodes")
    if len(points) < 2 or sum(distance_meters(a, b) for a, b in zip(points, points[1:])) < .1:
        raise ValueError("GPX must contain at least two nodes and a non-zero length")
    return points


def point_data(point: Point) -> dict:
    result = {"lat": point.latitude, "lng": point.longitude}
    if point.timestamp is not None:
        result["time"] = point.timestamp.isoformat()
    return result


def load_points(path: str | Path) -> list[Point]:
    with Path(path).open("rb") as source:
        data = source.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("GPX exceeds 2 MB")
    return parse_points(data.decode("utf-8-sig"))
