import asyncio
from types import SimpleNamespace
import pytest
from ios_location_controller.device import LocationDevice
from ios_location_controller.gpx import Point, distance_meters, offset_point
from ios_location_controller.motion import Motion, Settings


class Recorder(LocationDevice):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.sent = []

    async def set_point(self, point):
        self.sent.append((self.clock[0], point))


def fake_clock(monkeypatch):
    clock = [0.0]
    async def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    monkeypatch.setattr(asyncio, 'get_running_loop', lambda: SimpleNamespace(time=lambda: clock[0]))
    return clock


def test_cli_uses_the_same_samples_as_the_shared_motion_engine(monkeypatch):
    clock = fake_clock(monkeypatch)
    points = [Point(0, 0), offset_point(Point(0, 0), 0, 3)]
    device = Recorder(clock)
    asyncio.run(device.play(points, .1, speed_kmh=5, speed_variation_pct=10,
                            lateral_variation_m=1, random_seed=42))
    settings = Settings(interval=.1)
    motion = Motion(points, settings)
    last = 0
    assert device.sent[0] == (0, points[0])
    for elapsed, actual in device.sent[1:]:
        expected, _, _ = motion.advance(elapsed - last, settings)
        assert distance_meters(actual, expected) < .0001
        last = elapsed
    assert device.sent[1][0] >= .1
    assert distance_meters(device.sent[-1][1], points[-1]) < .0001


def test_short_route_does_not_emit_the_endpoint_before_waiting(monkeypatch):
    clock = fake_clock(monkeypatch)
    points = [Point(0, 0), offset_point(Point(0, 0), 0, .2)]
    device = Recorder(clock)
    asyncio.run(device.play(points, 1, speed_kmh=3.6))
    assert device.sent[0][0] == 0
    assert device.sent[-1][0] >= 1


def test_pause_time_is_excluded_from_cli_motion(monkeypatch):
    clock = [0.0]
    class Gate:
        active = True
        async def wait(self):
            if not self.active:
                clock[0] += 100
                self.active = True
        def is_set(self):
            return self.active
    gate = Gate()
    paused = False
    async def sleep(seconds):
        nonlocal paused
        clock[0] += seconds
        if not paused:
            gate.active = False
            paused = True
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    monkeypatch.setattr(asyncio, 'get_running_loop', lambda: SimpleNamespace(time=lambda: clock[0]))
    points = [Point(0, 0), offset_point(Point(0, 0), 0, 3)]
    device = Recorder(clock)
    asyncio.run(device.play(points, .1, speed_kmh=5, pause_event=gate))
    expected = Motion(points, Settings(interval=.1, speed_variation_pct=0, lateral_variation_m=0))
    for _, point in device.sent[1:]:
        sample, _, _ = expected.advance(.1, expected._settings)
        assert distance_meters(point, sample) < .001
    assert device.sent[1][0] >= 100


@pytest.mark.parametrize('kwargs', [{'interval':float('nan')}, {'interval':0},
                                    {'interval':1, 'speed_kmh':float('inf')},
                                    {'interval':1, 'random_seed':-1}])
def test_invalid_play_parameters_do_not_write_to_the_device(kwargs):
    device = Recorder([0.0])
    with pytest.raises(ValueError):
        asyncio.run(device.play([Point(0, 0), Point(0, .01)], **kwargs))
    assert device.sent == []
