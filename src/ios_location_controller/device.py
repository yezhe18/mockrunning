from __future__ import annotations

import logging
from collections.abc import Sequence
import asyncio
import math
import json
import subprocess
import urllib.request
import sys
from contextlib import AsyncExitStack
from collections.abc import Awaitable, Callable

from .gpx import Point
from .motion import Motion, Settings

log = logging.getLogger(__name__)


class LocationDevice:
    """Adapter around pymobiledevice3's DVT location service."""

    def diagnostics(self):
        return {"horizontal_accuracy": False, "sensors": False, "raw_gnss": False,
                "mock": None, "injections": {}}

    async def set_route_point(self, point):
        return await self.set_point(point)

    def __init__(self, udid: str | None = None, rsd_host: str | None = None, rsd_port: int | None = None) -> None:
        self.udid = udid
        self.rsd_host = rsd_host
        self.rsd_port = rsd_port
        self._simulation = None
        self._provider = None
        self._transport = None
        self._tunnel = None
        self._resources = None
        self._wda_process = None
        self.wda_error = None

    async def connect(self) -> None:
        if (self.rsd_host is None) != (self.rsd_port is None):
            raise ValueError("RSD host and port must be provided together")
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        resources = AsyncExitStack()
        try:
            if self.rsd_host is not None:
                from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
                transport = RemoteServiceDiscoveryService((self.rsd_host, self.rsd_port))
                resources.push_async_callback(transport.close)
                await transport.connect()
                mode = "external RSD"
            else:
                from pymobiledevice3.lockdown import create_using_usbmux
                lockdown = await create_using_usbmux(self.udid)
                if int(lockdown.product_version.split('.')[0]) < 17:
                    transport = lockdown
                    resources.push_async_callback(lockdown.close)
                    mode = "USB lockdown"
                else:
                    await lockdown.close()
                    from pymobiledevice3.remote.rsd_tunnel import PreferredRsdTunnel
                    tunnel = PreferredRsdTunnel(serial=self.udid)
                    resources.push_async_callback(tunnel.aclose)
                    transport = await tunnel.aopen()
                    mode = "userspace RSD"
            provider = await resources.enter_async_context(DvtProvider(transport))
            self._simulation = await resources.enter_async_context(LocationSimulation(provider))
            self._resources = resources
        except BaseException:
            await resources.aclose()
            self._simulation = None
            raise
        log.info("Connected to iPhone (%s)", mode)

        if self.rsd_host is not None:
            self.wda_error = "External RSD: real GPS readback is unavailable"
            return

        # WDA is optional: the DVT simulation path must remain usable when it is absent.
        try:
            command = [sys.executable, "-m", "pymobiledevice3", "usbmux", "forward", "8100", "8100"]
            if self.udid:
                command.extend(["--serial", self.udid])
            self._wda_process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            await asyncio.sleep(0.25)
            await self.read_location()
        except Exception as exc:
            self.wda_error = str(exc) or type(exc).__name__
            self._stop_wda()

    async def read_location(self) -> dict:
        if self.rsd_host is not None:
            raise RuntimeError("External RSD: real GPS readback is unavailable")
        def request():
            with urllib.request.urlopen("http://127.0.0.1:8100/wda/device/location", timeout=3) as response:
                data = json.loads(response.read().decode("utf-8"))
            value = data.get("value", data)
            if "latitude" not in value or "longitude" not in value:
                raise RuntimeError(value.get("message", "WDA returned no location"))
            point = {"lat": float(value["latitude"]), "lng": float(value["longitude"]),
                     "altitude": float(value.get("altitude", 0))}
            if not all(math.isfinite(v) for v in point.values()) or not -90 <= point["lat"] <= 90 or not -180 <= point["lng"] <= 180:
                raise RuntimeError("WDA returned invalid coordinates")
            return point
        return await asyncio.to_thread(request)

    def _stop_wda(self):
        process, self._wda_process = self._wda_process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    async def set_point(self, point: Point) -> None:
        if self._simulation is None:
            raise RuntimeError("Device is not connected")
        await self._simulation.set(point.latitude, point.longitude)

    async def play(
        self,
        points: Sequence[Point],
        interval: float,
        loop: bool = False,
        speed_kmh: float | None = None,
        speed_variation_pct: float = 0.0,
        lateral_variation_m: float = 0.0,
        random_seed: int | None = None,
        on_point: Callable[[Point], Awaitable[None]] | None = None,
        pause_event: asyncio.Event | None = None,
        *,
        timing_mode: str = "speed",
        variation_period: float = 12.0,
        max_acceleration_mps2: float = 0.8,
        corner_radius_m: float = 3.0,
        max_lateral_acceleration_mps2: float = 1.5,
    ) -> None:
        settings = Settings.parse({
            "speed_kmh": 5.0 if speed_kmh is None else speed_kmh,
            "interval": interval, "loop": loop, "speed_variation_pct": speed_variation_pct,
            "lateral_variation_m": lateral_variation_m,
            "random_seed": 42 if random_seed is None else random_seed,
            "timing_mode": timing_mode, "variation_period": variation_period,
            "max_acceleration_mps2": max_acceleration_mps2, "corner_radius_m": corner_radius_m,
            "max_lateral_acceleration_mps2": max_lateral_acceleration_mps2,
        })
        motion = Motion(points, settings)
        if pause_event is not None:
            await pause_event.wait()
        await self.set_point(points[0])
        if on_point is not None:
            await on_point(points[0])
        clock = asyncio.get_running_loop().time
        last = clock()
        while True:
            if pause_event is not None and not pause_event.is_set():
                await pause_event.wait()
                last = clock()
            # Wait before moving: short segments must not reach their endpoint early.
            await asyncio.sleep(settings.interval)
            if pause_event is not None and not pause_event.is_set():
                await pause_event.wait()
                last = clock()
                continue
            now = clock()
            delta, last = min(now - last, settings.interval * 2), now
            point, _, done = motion.advance(delta, settings)
            sender = self.set_point if done else self.set_route_point
            sent = await sender(point)
            if on_point is not None and sent is not False:
                await on_point(point)
            if done:
                return

    async def clear(self) -> None:
        if self._simulation is not None:
            await self._simulation.clear()

    async def close(self) -> None:
        try:
            if self._simulation is not None:
                await self._simulation.clear()
        finally:
            self._simulation = None
            resources, self._resources = self._resources, None
            try:
                if resources is not None:
                    await resources.aclose()
            finally:
                self._stop_wda()
