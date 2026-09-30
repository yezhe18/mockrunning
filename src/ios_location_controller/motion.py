"""Reproducible route simulation shared by the web player and CLI.

Dynamics use a fixed integration grid independent of the output interval.
This is a kinematic test model, not a model of a person's gait or GPS receiver.
"""
from bisect import bisect_right
from dataclasses import dataclass, asdict
from datetime import datetime
from functools import lru_cache
from itertools import accumulate
import math
import random

from .gpx import Point, distance_meters, interpolate, offset_point, bearing_radians


def route_points(items):
    if not isinstance(items, list) or not 2 <= len(items) <= 10000:
        raise ValueError("Route must contain 2 to 10000 nodes")
    points = []
    for item in items:
        try:
            lat, lon = float(item["lat"]), float(item["lng"])
            stamp = item.get("time")
            timestamp = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp is not None else None
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise ValueError("Invalid route coordinate or timestamp") from exc
        if not math.isfinite(lat) or not math.isfinite(lon) or not -85 <= lat <= 85 or not -180 <= lon <= 180:
            raise ValueError("Coordinates out of range (latitude -85..85, longitude -180..180)")
        points.append(Point(lat, lon, timestamp))
    if sum(distance_meters(a, b) for a, b in zip(points, points[1:])) < 0.1:
        raise ValueError("Route must have a non-zero length")
    return points


@dataclass(frozen=True)
class Settings:
    speed_kmh: float = 5.0
    interval: float = 1.0
    speed_variation_pct: float = 10.0
    lateral_variation_m: float = 1.0
    variation_period: float = 12.0
    max_acceleration_mps2: float = 0.8
    corner_radius_m: float = 3.0
    max_lateral_acceleration_mps2: float = 1.5
    timing_mode: str = "speed"
    loop: bool = False
    random_seed: int = 42

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or set(data) - set(asdict(cls())):
            raise ValueError("Unknown settings")
        values = asdict(cls()) | data
        for key, low, high in [("speed_kmh", .1, 300), ("interval", .1, 10),
                               ("speed_variation_pct", 0, 80), ("lateral_variation_m", 0, 20),
                               ("variation_period", 2, 120), ("max_acceleration_mps2", .05, 10),
                               ("corner_radius_m", 0, 50), ("max_lateral_acceleration_mps2", .05, 10)]:
            try:
                value = float(values[key])
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{key} must be numeric") from exc
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key} must be within {low}..{high}")
            values[key] = value
        if type(values["loop"]) is not bool:
            raise ValueError("loop must be boolean")
        seed = values["random_seed"]
        if type(seed) is not int or not 0 <= seed <= 2147483647:
            raise ValueError("random_seed must be an integer in 0..2147483647")
        if values["timing_mode"] not in ("speed", "timestamps"):
            raise ValueError("timing_mode must be speed or timestamps")
        return cls(**values)


def _smoothstep(value):
    value = max(0.0, min(1.0, value))
    return value ** 3 * (10 + value * (-15 + 6 * value))


@lru_cache(maxsize=2048)
def _random_knot(seed, stream, bucket):
    return random.Random(f"{seed}:{stream}:{bucket}").uniform(-1, 1)


def smooth_noise(elapsed, period, seed, stream):
    """Independent bounded random knots with continuous first/second derivatives."""
    bucket = math.floor(elapsed / period)
    blend = _smoothstep(elapsed / period - bucket)
    a, b = _random_knot(seed, stream, bucket), _random_knot(seed, stream, bucket + 1)
    return a + (b - a) * blend


def _turn(a, b, c):
    incoming = bearing_radians(b, a) + math.pi
    return abs((bearing_radians(b, c) - incoming + math.pi) % math.tau - math.pi)


class Route:
    """Arc-length index, optional local corner rounding, and braking limits."""

    def __init__(self, points, radius=0):
        clean = [points[0]]
        for point in points[1:]:
            if distance_meters(clean[-1], point) > 1e-7:
                interpolate(clean[-1], point, .5)  # reject ambiguous antipodal arcs
                clean.append(point)
        rounded = [clean[0]]
        for a, b, c in zip(clean, clean[1:], clean[2:]):
            turn = _turn(a, b, c)
            inset = min(radius, distance_meters(a, b) * .25, distance_meters(b, c) * .25)
            if inset <= 0 or turn < .01 or turn > math.pi - .01:
                rounded.append(b)
                continue
            entry = interpolate(a, b, 1 - inset / distance_meters(a, b))
            exit_point = interpolate(b, c, inset / distance_meters(b, c))
            rounded.append(entry)
            steps = min(64, max(8, math.ceil(2 * inset / .5)))
            for step in range(1, steps + 1):
                t = step / steps
                rounded.append(interpolate(interpolate(entry, b, t), interpolate(b, exit_point, t), t))
        rounded.append(clean[-1])
        self.points = rounded
        self.lengths = [distance_meters(a, b) for a, b in zip(rounded, rounded[1:])]
        self.cumulative = [0.0, *accumulate(self.lengths)]
        self.total = self.cumulative[-1]
        self.caps = None
        self.stops = []

    def locate(self, distance):
        index = min(len(self.lengths) - 1, max(0, bisect_right(self.cumulative, distance) - 1))
        fraction = (distance - self.cumulative[index]) / self.lengths[index]
        a, b = self.points[index:index + 2]
        return interpolate(a, b, fraction), bearing_radians(a, b), index

    def configure(self, settings):
        accel = settings.max_acceleration_mps2
        peak = settings.speed_kmh / 3.6 * (1 + settings.speed_variation_pct / 100)
        caps = [peak ** 2] * len(self.points)
        for index in range(1, len(self.points) - 1):
            turn = _turn(*self.points[index - 1:index + 2])
            if turn > .01:
                if settings.corner_radius_m == 0 or turn > .8:
                    caps[index] = 0.0  # stop before an unsmoothed sharp turn
                    continue
                radius = min(self.lengths[index - 1], self.lengths[index]) / (2 * math.sin(turn / 2))
                caps[index] = min(caps[index], radius * settings.max_lateral_acceleration_mps2)
        caps[0] = caps[-1] = 0.0  # rest at endpoints, including the loop seam
        for index in range(len(caps) - 2, -1, -1):
            caps[index] = min(caps[index], caps[index + 1] + 2 * accel * self.lengths[index])
        for index in range(1, len(caps)):
            caps[index] = min(caps[index], caps[index - 1] + 2 * accel * self.lengths[index - 1])
        self.caps = caps
        self.stops = [distance for distance, cap in zip(self.cumulative[1:], caps[1:]) if cap == 0]

    def speed_limit(self, distance, acceleration):
        index = min(len(self.lengths) - 1, max(0, bisect_right(self.cumulative, distance) - 1))
        before = self.caps[index] + 2 * acceleration * (distance - self.cumulative[index])
        after = self.caps[index + 1] + 2 * acceleration * (self.cumulative[index + 1] - distance)
        return math.sqrt(max(0, min(before, after)))


class Motion:
    STEP = .05
    STRUCTURAL_SETTINGS = ("timing_mode", "corner_radius_m", "loop", "random_seed",
                           "max_acceleration_mps2", "max_lateral_acceleration_mps2")

    def __init__(self, points, settings):
        settings = Settings.parse(asdict(settings))
        if len(points) < 2 or len(points) > 10000:
            raise ValueError("Route must contain 2 to 10000 nodes")
        if any(not math.isfinite(p.latitude) or not math.isfinite(p.longitude) or
               not -85 <= p.latitude <= 85 or not -180 <= p.longitude <= 180 for p in points):
            raise ValueError("Invalid route coordinates")
        if settings.loop and distance_meters(points[0], points[-1]) > .1:
            raise ValueError("Loop requires a closed route; add an explicit return path first")
        self.points = list(points)
        if settings.loop:
            self.points[-1] = Point(points[0].latitude, points[0].longitude, points[-1].timestamp)
        self.route = Route(self.points, settings.corner_radius_m if settings.timing_mode == "speed" else 0)
        self.total = self.route.total
        if self.total < .1:
            raise ValueError("Route must have a non-zero length")
        self.distance = self.elapsed = self.speed_mps = 0.0
        self.laps = 0
        self._integrated = self._travelled = self._velocity = 0.0
        self._observed_distance = 0.0
        self._settings = settings
        self._done = False
        self.route.configure(settings)
        self.times = None
        if settings.timing_mode == "timestamps":
            stamps = [p.timestamp for p in self.points]
            if any(t is None or t.utcoffset() is None for t in stamps):
                raise ValueError("Timestamp replay requires a timezone-qualified time on every node")
            self.times = [(t - stamps[0]).total_seconds() for t in stamps]
            if any(b <= a for a, b in zip(self.times, self.times[1:])):
                raise ValueError("GPX timestamps must be strictly increasing")
            self.recorded_lengths = [distance_meters(a, b) for a, b in zip(self.points, self.points[1:])]
            self.recorded_cumulative = [0.0, *accumulate(self.recorded_lengths)]

    def _step(self, distance, velocity, elapsed, dt, settings):
        position = distance % self.total if settings.loop else distance
        accel = settings.max_acceleration_mps2
        nominal = settings.speed_kmh / 3.6
        noise = smooth_noise(elapsed + dt / 2, settings.variation_period, settings.random_seed, "speed")
        desired = nominal * (1 + settings.speed_variation_pct / 100 * noise)
        # Look half a step ahead to avoid a zero-speed equilibrium at the start.
        limit = self.route.speed_limit(min(self.total, position + max(velocity * dt / 2, accel * dt * dt / 2)), accel)
        desired = min(desired, limit)
        lap = math.floor(distance / self.total) if settings.loop else 0
        stop_index = min(len(self.route.stops) - 1, bisect_right(self.route.stops, position + 1e-8))
        boundary = lap * self.total + self.route.stops[stop_index]
        final_stop = not settings.loop and boundary == self.total
        remaining = boundary - distance
        if velocity > 0 and remaining <= velocity * dt / 2 + 1e-10:
            # Stop inside this step instead of integrating negative velocity.
            return boundary, 0.0, final_stop
        # Ensure the next state still has enough distance to stop. Solving
        # u² <= 2*a*(remaining - (v+u)*dt/2) avoids a last-frame speed reset.
        safe_end = max(0.0, math.sqrt(max(0.0, (accel * dt / 2) ** 2 + 2 * accel * remaining - accel * dt * velocity)) - accel * dt / 2)
        desired = min(desired, safe_end)
        next_speed = max(0.0, velocity + max(-accel * dt, min(accel * dt, desired - velocity)))
        next_distance = distance + (velocity + next_speed) * dt / 2
        if next_distance >= boundary or boundary - next_distance < 1e-8:
            return boundary, 0.0, final_stop
        return next_distance, next_speed, False

    def advance(self, dt, settings):
        if not math.isfinite(dt) or dt < 0:
            raise ValueError("Elapsed time must be finite and nonnegative")
        if any(getattr(settings, key) != getattr(self._settings, key) for key in self.STRUCTURAL_SETTINGS):
            raise ValueError("Stop and restart before changing replay mode, corners, acceleration limits, loop or seed")
        if self._done:
            return self.points[-1], 0.0, True
        if self.times is not None:
            return self._recorded(dt, settings)
        if settings != self._settings:
            self._integrated, self._travelled, self._velocity = self.elapsed, self._observed_distance, self.speed_mps
            self.route.configure(settings)
            self._settings = settings
        target = self.elapsed + dt
        while self._integrated + self.STEP <= target + 1e-9:
            self._travelled, self._velocity, self._done = self._step(
                self._travelled, self._velocity, self._integrated, self.STEP, settings)
            self._integrated += self.STEP
            if self._done:
                target = self._integrated
                break
        remaining = max(0.0, target - self._integrated)
        distance, speed, done = self._step(self._travelled, self._velocity, self._integrated, remaining, settings) if remaining else (self._travelled, self._velocity, self._done)
        self.elapsed, self.speed_mps, self._observed_distance = target, speed, distance
        self._done = done
        self.laps = math.floor(distance / self.total) if settings.loop else 0
        self.distance = distance % self.total if settings.loop else min(distance, self.total)
        if done:
            return self.points[-1], 0.0, True
        point, bearing, _ = self.route.locate(self.distance)
        taper_length = max(3.0, settings.lateral_variation_m * 2)
        stop_index = bisect_right(self.route.stops, self.distance)
        previous_stop = self.route.stops[stop_index - 1] if stop_index else 0.0
        next_stop = self.route.stops[min(stop_index, len(self.route.stops) - 1)]
        taper = _smoothstep((self.distance - previous_stop) / taper_length) * _smoothstep((next_stop - self.distance) / taper_length)
        sway = settings.lateral_variation_m * smooth_noise(self.elapsed, settings.variation_period, settings.random_seed, "lateral") * taper
        point = offset_point(point, -math.sin(bearing) * sway, math.cos(bearing) * sway)
        return point, speed * 3.6, False

    def _recorded(self, dt, settings):
        self.elapsed += dt
        duration = self.times[-1]
        self.laps = math.floor(self.elapsed / duration) if settings.loop else 0
        elapsed = self.elapsed % duration if settings.loop else min(self.elapsed, duration)
        self._done = elapsed >= duration and not settings.loop
        index = min(len(self.points) - 2, bisect_right(self.times, elapsed) - 1)
        span = self.times[index + 1] - self.times[index]
        fraction = (elapsed - self.times[index]) / span
        self.distance = self.recorded_cumulative[index] + self.recorded_lengths[index] * fraction
        self.speed_mps = 0.0 if self._done else self.recorded_lengths[index] / span
        return interpolate(self.points[index], self.points[index + 1], fraction), self.speed_mps * 3.6, self._done
