from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is required for the UI regression test')
def test_live_position_priority_freshness_and_timestamp_export():
    source = Path(__file__).parents[1] / 'src/ios_location_controller/static/app.js'
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const text = fs.readFileSync(process.argv[1], 'utf8');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: id === 'platform' ? 'ios' : '', checked:false, textContent:'',
    replaceChildren(){}, add(){}, querySelectorAll(){return [];}
  });
  return elements.get(id);
}
const context = vm.createContext({
  document:{getElementById:element}, window:{addEventListener(){}},
  Number, JSON, Math, Object, String, Option:function(){},
});
vm.runInContext(text.slice(0, text.indexOf("$('tab-editor').onclick")), context);
const data = {
  connected:true, udid:'phone', state:'playing', devices:[], diagnostics:null,
  current:{lat:31,lng:121}, real_current:{lat:35,lng:139}, readback_age_s:0,
  settings:{speed_kmh:5}, route:{name:'route',points:[]},
  speed_kmh:5, total_m:100, distance_m:20, elapsed_s:10, laps:0
};
context.data = data;
vm.runInContext('renderStatus(data)', context);
assert.equal(element('position').textContent, '已发送：31.000000, 121.000000');
data.current = null;
vm.runInContext('renderStatus(data)', context);
assert.equal(element('position').textContent, 'WDA 读回：35.000000, 139.000000');
data.readback_age_s = 11;
vm.runInContext('renderStatus(data)', context);
assert.equal(element('position').textContent, '尚未发送模拟坐标');
let xml = '';
context.Blob = function(data) { this.data = data; };
context.URL = {createObjectURL(blob){xml = blob.data[0]; return 'blob:test';}, revokeObjectURL(){}};
context.setTimeout = function(){};
context.document.createElement = function(){return {click(){},remove(){}};};
context.document.body = {append(){}};
vm.runInContext("draft = [{lat:0,lng:0,time:'2026-01-01T00:00:00Z'},{lat:0,lng:1,time:'2026-01-01T00:01:00Z'}]; exportRoute()", context);
assert(xml.includes('<time>2026-01-01T00:00:00Z</time>'));
assert(xml.includes('<time>2026-01-01T00:01:00Z</time>'));
'''
    subprocess.run(['node', '-e', script, str(source)], check=True, capture_output=True, text=True)
