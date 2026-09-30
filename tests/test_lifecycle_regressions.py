import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest
from ios_location_controller.device import LocationDevice
from ios_location_controller.playback import PlaybackController
from test_playback import FakeDevice


def eventually(predicate, timeout=2):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def test_readback_refreshes_and_clears_without_masking_sent_coordinates(tmp_path):
    class Reader(FakeDevice):
        reads = 0
        async def read_location(self):
            self.reads += 1
            return {'lat':float(self.reads), 'lng':10.0}
    class Controller(PlaybackController):
        READBACK_INTERVAL = .02
    controller = Controller(tmp_path / 'session.json', Reader)
    try:
        result = controller.call('connect')
        assert result['real_current'] == {'lat':1.0, 'lng':10.0}
        eventually(lambda: controller.device.reads >= 2)
        assert controller.status()['real_current']['lat'] >= 2
        result = controller.call('position', {'lat':31, 'lng':121})
        assert result['current'] == {'lat':31, 'lng':121}
        assert result['real_current'] is None
        assert result['readback_age_s'] is None
        result = controller.call('stop')
        assert result['current'] is None and result['real_current'] is None
        result = controller.call('clear-route')
        assert result['real_current'] is None
        controller.call('disconnect')
    finally:
        controller.close()
    assert controller.loop.is_closed() and not controller.thread.is_alive()
    controller.close()  # shutdown is idempotent
    with pytest.raises(RuntimeError, match='closed'):
        controller.status()


def test_delayed_readback_does_not_restore_a_cleared_sample(tmp_path):
    class Reader(FakeDevice):
        reads = 0
        async def read_location(self):
            self.reads += 1
            if self.reads > 1:
                await asyncio.sleep(.1)
            return {'lat':1.0, 'lng':2.0}
    class Controller(PlaybackController):
        READBACK_INTERVAL = .01
    controller = Controller(tmp_path / 'session.json', Reader)
    try:
        controller.call('connect')
        eventually(lambda: controller.device.reads == 2)
        controller.call('stop')
        time.sleep(.105)
        assert controller.status()['real_current'] is None
    finally:
        controller.close()


def test_paused_structural_settings_cannot_invalidate_motion(tmp_path):
    controller = PlaybackController(tmp_path / 'session.json', FakeDevice)
    try:
        controller.call('route', {'points':[{'lat':0,'lng':0}, {'lat':0,'lng':.01}]})
        controller.call('connect')
        controller.call('start')
        controller.call('pause')
        with pytest.raises(ValueError, match='Stop'):
            controller.call('settings', {'random_seed':123})
        assert controller.status()['settings']['random_seed'] == 42
        controller.call('settings', {'speed_kmh':8})
        controller.call('start')
        assert controller.status()['state'] == 'playing'
    finally:
        controller.close()


def test_wda_subprocess_is_reaped_even_if_transport_cleanup_fails():
    class Resources:
        async def aclose(self):
            raise RuntimeError('transport lost')
    device = LocationDevice()
    device._resources = Resources()
    process = Mock()
    process.poll.return_value = None
    device._wda_process = process
    with pytest.raises(RuntimeError, match='transport lost'):
        asyncio.run(device.close())
    process.terminate.assert_called_once()
    process.wait.assert_called_once_with(timeout=2)
    assert device._wda_process is None


@pytest.mark.skipif(os.name == 'nt', reason='Windows terminate is not a catchable POSIX signal')
@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT])
def test_web_server_signals_run_cleanup(tmp_path, signum):
    ready, cleaned = tmp_path / 'ready', tmp_path / 'cleaned'
    script = '''
import pathlib, signal, sys
from ios_location_controller import web
ready, cleaned = map(pathlib.Path, sys.argv[1:])
original_signal = signal.signal
def register(sig, handler):
    previous = original_signal(sig, handler)
    if sig == signal.SIGTERM:
        ready.write_text('ready')
    return previous
signal.signal = register
class Controller:
    def __init__(self, path): pass
    def close(self): cleaned.write_text('cleaned')
web.PlaybackController = Controller
web.serve(port=0)
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(ready), str(cleaned)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        eventually(ready.exists, timeout=5)
        process.send_signal(signum)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr.decode()
        assert cleaned.read_text() == 'cleaned'
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux ownership check uses /proc')
def test_stop_script_does_not_signal_an_unrelated_process(tmp_path):
    import shutil
    project = Path(__file__).parents[1]
    shutil.copy2(project / 'stop-server.sh', tmp_path / 'stop-server.sh')
    (tmp_path / '.venv/bin').mkdir(parents=True)
    (tmp_path / '.venv/bin/python').symlink_to(sys.executable)
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=tmp_path)
    try:
        (tmp_path / 'server.pid').write_text(str(process.pid) + '\n')
        result = subprocess.run(['bash', str(tmp_path / 'stop-server.sh')], capture_output=True, timeout=5)
        assert result.returncode == 1
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)
