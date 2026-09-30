from dataclasses import replace
from datetime import datetime, timezone, timedelta
import math
import pytest

from ios_location_controller.gpx import Point, distance_meters, interpolate, offset_point, load_points
from ios_location_controller.motion import Motion, Settings, route_points, smooth_noise
from ios_location_controller.web import parse_gpx


def test_date_line_uses_the_short_arc_and_offsets_wrap():
    a, b = Point(0, 179.9), Point(0, -179.9)
    mid = interpolate(a, b, .5)
    assert abs(abs(mid.longitude) - 180) < 1e-8
    assert distance_meters(a, mid) == pytest.approx(distance_meters(a, b) / 2)
    moved = offset_point(Point(0, 179.9999), 0, 100)
    assert -180 <= moved.longitude < 180
    assert distance_meters(Point(0, 179.9999), moved) == pytest.approx(100, abs=.001)


def test_output_interval_does_not_change_the_simulated_path():
    points = [Point(0, 0), Point(0, .01)]
    settings = Settings()
    fine, coarse = Motion(points, settings), Motion(points, settings)
    for _ in range(40):
        p1, speed1, _ = fine.advance(.13, settings)
    for _ in range(4):
        p2, speed2, _ = coarse.advance(1.3, settings)
    assert fine.distance == pytest.approx(coarse.distance, abs=1e-8)
    assert distance_meters(p1, p2) < 1e-6
    assert speed1 == pytest.approx(speed2, abs=1e-8)


def test_collinear_node_insertion_does_not_reset_lateral_variation():
    a, b = Point(0, 0), Point(0, .001)
    settings = Settings(lateral_variation_m=5, speed_variation_pct=0)
    plain = Motion([a, b], settings)
    split = Motion([a, interpolate(a, b, .5), b], settings)
    for _ in range(80):
        p1, _, _ = plain.advance(.5, settings)
        p2, _, _ = split.advance(.5, settings)
        assert distance_meters(p1, p2) < .0001


def test_speed_changes_are_bounded_including_braking_to_the_endpoint():
    points = [Point(0, 0), offset_point(Point(0, 0), 0, 30)]
    settings = Settings(speed_kmh=12, speed_variation_pct=40, lateral_variation_m=0,
                        max_acceleration_mps2=.4)
    motion = Motion(points, settings)
    previous = 0
    for _ in range(1000):
        point, speed, done = motion.advance(.1, settings)
        assert abs(speed - previous) / 3.6 <= .4 * .1 + .001
        assert 0 <= speed <= 12 * 1.4
        previous = speed
        if done:
            assert speed == 0
            assert distance_meters(point, points[-1]) < .001
            break
    else:
        pytest.fail('Route never completed')


def test_rounded_corner_slows_down_and_preserves_endpoints():
    a = Point(0, 0)
    b = offset_point(a, 0, 30)
    c = offset_point(b, 30, 0)
    settings = Settings(speed_kmh=18, speed_variation_pct=0, lateral_variation_m=0,
                        corner_radius_m=4, max_lateral_acceleration_mps2=.5)
    motion = Motion([a, b, c], settings)
    corner_speeds = []
    assert motion.total < 60  # local curve cuts inside the corner
    for _ in range(2000):
        point, speed, done = motion.advance(.1, settings)
        if distance_meters(point, b) < 2:
            corner_speeds.append(speed)
        if done:
            assert distance_meters(point, c) < .001
            break
    assert corner_speeds and max(corner_speeds) < 6


def test_loop_crossing_has_no_teleport():
    a, b = Point(0, 0), offset_point(Point(0, 0), 0, 10)
    settings = Settings(loop=True, lateral_variation_m=0, speed_variation_pct=0)
    motion = Motion([a, b, a], settings)
    previous, crossed = a, False
    for _ in range(1000):
        point, speed, done = motion.advance(.1, settings)
        assert not done
        assert distance_meters(previous, point) <= settings.speed_kmh / 3.6 * .1 + .001
        previous = point
        if motion.laps:
            crossed = True
            break
    assert crossed


def test_noise_is_nonperiodic_and_uses_independent_streams():
    speed = [smooth_noise(t, 12, 42, 'speed') for t in (0, 12, 24, 36)]
    lateral = [smooth_noise(t, 12, 42, 'lateral') for t in (0, 12, 24, 36)]
    assert len(set(speed)) == 4
    assert speed != lateral
    assert all(-1 <= value <= 1 for value in speed + lateral)


def stamped_route():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a = Point(0, 0, start)
    hold = Point(0, 0, start + timedelta(seconds=5))
    b = Point(0, .001, start + timedelta(seconds=15))
    return [a, hold, b]


def test_timestamp_replay_preserves_stops_and_ignores_synthetic_variation():
    points = stamped_route()
    settings = Settings(timing_mode='timestamps', lateral_variation_m=20, speed_variation_pct=80)
    motion = Motion(points, settings)
    point, speed, done = motion.advance(2, settings)
    assert point.latitude == 0 and point.longitude == 0 and speed == 0
    point, speed, done = motion.advance(5, settings)
    assert distance_meters(point, interpolate(points[1], points[2], .2)) < .0001
    assert speed == pytest.approx(distance_meters(points[1], points[2]) / 10 * 3.6)
    point, speed, done = motion.advance(20, settings)
    assert done and speed == 0 and point == points[-1]


@pytest.mark.parametrize('change', ['missing', 'naive', 'same', 'reverse'])
def test_timestamp_mode_rejects_invalid_times(change):
    points = stamped_route()
    if change == 'missing':
        points[1] = replace(points[1], timestamp=None)
    elif change == 'naive':
        points[1] = replace(points[1], timestamp=points[1].timestamp.replace(tzinfo=None))
    elif change == 'same':
        points[1] = replace(points[1], timestamp=points[0].timestamp)
    else:
        points[2] = replace(points[2], timestamp=points[0].timestamp)
    with pytest.raises(ValueError):
        Motion(points, Settings(timing_mode='timestamps'))


def test_cli_web_import_and_json_round_trip_preserve_timestamps(tmp_path):
    xml = '<gpx><rte><rtept lat="0" lon="0"><time>2026-01-01T00:00:00Z</time></rtept><rtept lat="0" lon="0.01"><time>2026-01-01T00:00:10Z</time></rtept></rte></gpx>'
    path = tmp_path / 'route.gpx'
    path.write_text(xml)
    assert load_points(path) == route_points(parse_gpx(xml))
    assert all(p.timestamp is not None for p in load_points(path))


@pytest.mark.parametrize('xml', [
    '<gpx><trk><trkseg><trkpt lat="0" lon="0"/></trkseg><trkseg><trkpt lat="1" lon="1"/></trkseg></trk></gpx>',
    '<!DOCTYPE gpx [<!ENTITY a "value">]><gpx/>',
    '<gpx>'
])
def test_cli_and_web_both_reject_unsafe_or_disconnected_gpx(tmp_path, xml):
    path = tmp_path / 'route.gpx'
    path.write_text(xml)
    for loader, arg in ((load_points, path), (parse_gpx, xml)):
        with pytest.raises(ValueError):
            loader(arg)


@pytest.mark.parametrize('data', [
    {'max_acceleration_mps2':0}, {'max_lateral_acceleration_mps2':float('inf')},
    {'corner_radius_m':-1}, {'timing_mode':'unknown'}, {'interval':None}
])
def test_new_settings_are_validated(data):
    with pytest.raises(ValueError):
        Settings.parse(data)


@pytest.mark.parametrize('dt', [float('nan'), float('inf'), -1])
def test_invalid_time_steps(dt):
    settings = Settings()
    motion = Motion([Point(0, 0), Point(0, .001)], settings)
    with pytest.raises(ValueError):
        motion.advance(dt, settings)


@pytest.mark.parametrize('corner_radius', [0, 3])
@pytest.mark.parametrize('lateral', [0, 5])
def test_uturn_reaches_zero_speed_before_reversing(corner_radius, lateral):
    a, b = Point(0, 0), offset_point(Point(0, 0), 0, 10)
    settings = Settings(corner_radius_m=corner_radius, lateral_variation_m=lateral, speed_variation_pct=0)
    motion = Motion([a, b, a], settings)
    stopped_at_turn = False
    for _ in range(1000):
        point, speed, done = motion.advance(.05, settings)
        if distance_meters(point, b) < .00001 and speed == 0:
            stopped_at_turn = True
        if done:
            break
    assert stopped_at_turn
