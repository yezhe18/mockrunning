import json
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import pytest
from ios_location_controller.web import Handler
from ios_location_controller import __version__

@pytest.fixture
def server():
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown();server.server_close();thread.join()

def test_static_and_health(server):
    for path in ['/','/static/app.js','/static/app.css','/static/leaflet.js']:
        with urlopen(server+path) as r:
            assert r.status==200 and r.headers['Cache-Control']=='no-store'
    assert json.load(urlopen(server+'/api/health'))['version']==__version__
    with pytest.raises(HTTPError) as exc: urlopen(server+'/static/../../README.md')
    assert exc.value.code==404

def test_bad_import_and_origin(server):
    request=Request(server+'/api/import',data=b'{"gpx":"<gpx/>"}',headers={'Content-Type':'application/json'})
    with pytest.raises(HTTPError) as exc: urlopen(request)
    assert exc.value.code==400
    request.add_header('Origin','http://untrusted.invalid')
    with pytest.raises(HTTPError) as exc: urlopen(request)
    assert exc.value.code==403

def test_foreign_host_is_rejected_for_all_methods(server):
    for path in ['/api/status', '/api/health', '/', '/static/app.js']:
        request=Request(server+path,headers={'Host':'attacker.invalid'})
        with pytest.raises(HTTPError) as exc: urlopen(request)
        assert exc.value.code==403
    request=Request(server+'/api/import',data=b'{}',headers={
        'Content-Type':'application/json','Host':'attacker.invalid'})
    with pytest.raises(HTTPError) as exc: urlopen(request)
    assert exc.value.code==403

def test_gpx_import(server):
    body=json.dumps({'gpx':'<gpx><trk><trkseg><trkpt lat="31" lon="121"/><trkpt lat="32" lon="121"/></trkseg></trk></gpx>'}).encode()
    result=json.load(urlopen(Request(server+'/api/import',data=body,headers={'Content-Type':'application/json'})))
    assert len(result['points'])==2
