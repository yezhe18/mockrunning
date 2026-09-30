"""Loopback-only UI server. Editing routes never starts device playback."""
import json
import mimetypes
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import __version__
from .gpx import parse_points, point_data
from .playback import PlaybackController

STATIC = Path(__file__).parent / "static"


def parse_gpx(text):
    return [point_data(p) for p in parse_points(text)]


class Handler(BaseHTTPRequestHandler):
    def host_allowed(self):
        host = self.headers.get("Host", "").partition(":")[0].lower()
        return host in ("localhost", "127.0.0.1")

    def send_data(self, body, content_type, code=200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, code=200):
        self.send_data(json.dumps(data, ensure_ascii=False, allow_nan=False).encode(), "application/json; charset=utf-8", code)

    def do_GET(self):
        if not self.host_allowed():
            self.json({"error": "Host denied"}, 403)
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/status":
                self.json(self.server.controller.status())
                return
            if path == "/api/health":
                self.json({"app": "ios-location-controller", "version": __version__})
                return
            name = "index.html" if path == "/" else path.removeprefix("/static/")
            target = (STATIC / name).resolve()
            if (path != "/" and not path.startswith("/static/")) or not target.is_relative_to(STATIC.resolve()) or not target.is_file():
                self.json({"error": "Not found"}, 404)
                return
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix in (".js", ".css", ".html"):
                mime += "; charset=utf-8"
            self.send_data(target.read_bytes(), mime)
        except Exception as exc:
            self.json({"error": str(exc) or type(exc).__name__}, 500)

    def do_POST(self):
        try:
            # Reject cross-site browser requests; this service controls a local device.
            origin = self.headers.get("Origin")
            if not self.host_allowed():
                self.json({"error": "Host denied"}, 403)
                return
            if origin and origin != "http://" + self.headers.get("Host", ""):
                self.json({"error": "Origin denied"}, 403)
                return
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Content-Type must be application/json")
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 2_500_000:
                raise ValueError("Request must be between 1 byte and 2.5 MB")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Request must be a JSON object")
            action = urlparse(self.path).path.removeprefix("/api/")
            if action == "import":
                self.json({"points": parse_gpx(data.get("gpx")), "name": str(data.get("name", "Route"))[:120]})
            elif action == "search":
                query = str(data.get("query", "")).strip()
                if not query or len(query) > 200:
                    raise ValueError("请输入有效地点")
                params = urlencode({"q": query, "format": "jsonv2", "limit": 5, "addressdetails": 1})
                request = Request("https://nominatim.openstreetmap.org/search?" + params,
                                  headers={"User-Agent": "RouteStudio/2.0 local location test tool"})
                with urlopen(request, timeout=10) as response:
                    results = json.loads(response.read(1_000_000).decode("utf-8"))
                self.json({"results": [{"name": str(item.get("display_name", ""))[:300],
                                         "lat": float(item["lat"]), "lng": float(item["lon"])}
                                        for item in results]})
            elif action in {"route", "clear-route", "settings", "connect", "pair", "disconnect", "start", "pause", "stop", "position"}:
                self.json(self.server.controller.call(action, data))
            else:
                self.json({"error": "Not found"}, 404)
        except Exception as exc:
            self.json({"error": str(exc) or type(exc).__name__}, 400)

    def log_message(self, format, *args):
        if "/api/status" not in str(args):
            super().log_message(format, *args)


def serve(host="127.0.0.1", port=8765):
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("This device controller only listens on localhost")
    server = ThreadingHTTPServer((host, port), Handler)
    path = Path(os.environ.get("IOS_LOCATION_STATE", Path.home() / ".ios-location-controller" / "session.json"))
    server.controller = PlaybackController(path)
    print(f"iOS Location Controller {__version__}: http://{host}:{port}", flush=True)
    handlers = {}
    def stop(signum, frame):
        raise KeyboardInterrupt
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Further signals must not interrupt device cleanup midway.
        for signum in handlers:
            signal.signal(signum, signal.SIG_IGN)
        try:
            server.controller.close()
        finally:
            server.server_close()
            for signum, previous in handlers.items():
                signal.signal(signum, previous)


def main():
    serve()


if __name__ == "__main__":
    main()
