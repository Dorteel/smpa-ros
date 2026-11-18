"""Tests of the frontend's pure logic, run with Node.js when it is installed.

The functions under test decide what the graph draws, where elements are
placed, and how sensor values are converted. They use no browser features,
so Node.js can import them directly. Without Node.js these tests are skipped;
the browser tests cover the same code through the real page.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / 'smpa_gui' / 'frontend'
pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is not installed')


def run_module(script):
    """Run an ES-module script in Node.js from the frontend directory and return its JSON output."""
    result = subprocess.run(['node', '--input-type=module', '-e', script], cwd=FRONTEND,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


GRAPH = dict(
    nodes=[dict(id='node:/a', full_name='/a', is_gateway=False, hidden=False),
           dict(id='node:/b', full_name='/b', is_gateway=False, hidden=False),
           dict(id='node:/smpa_gui', full_name='/smpa_gui', is_gateway=True, hidden=False)],
    topics=[dict(id='topic:/t', name='/t', types=['x'], hidden=False, infrastructure=False, action=None),
            dict(id='topic:/rosout', name='/rosout', types=['x'], hidden=False, infrastructure=True, action=None),
            dict(id='topic:/leaf', name='/leaf', types=['x'], hidden=False, infrastructure=False, action=None),
            dict(id='topic:/act/_action/status', name='/act/_action/status', types=['x'], hidden=True,
                 infrastructure=False, action='/act')],
    edges=[dict(id='e1', source='node:/a', target='topic:/t', kind='publishes'),
           dict(id='e2', source='topic:/t', target='node:/b', kind='subscribes'),
           dict(id='e3', source='node:/a', target='topic:/rosout', kind='publishes'),
           dict(id='e4', source='node:/a', target='topic:/leaf', kind='publishes'),
           dict(id='e5', source='topic:/t', target='node:/smpa_gui', kind='subscribes')],
    actions=[dict(id='action:/act', name='/act', types=['x'], servers=['/b'], clients=['/a'])])


def desired(**options):
    """Ask the frontend which element ids it would draw for the given filter options."""
    defaults = dict(topics=True, infrastructure=False, hidden=False, gateway=False, leaves=True, actions=True)
    script = ("import {desiredElements} from './tabs/ros/graph.js';"
              f"console.log(JSON.stringify(desiredElements({json.dumps(GRAPH)}, {json.dumps(dict(defaults, **options))})"
              ".map((element) => element.data.id)))")
    return set(run_module(script))


def test_graph_filters_choose_the_drawn_elements():
    """Defaults hide the GUI, infrastructure and action plumbing; each filter changes exactly its part."""
    default = desired()
    assert default == {'node:/a', 'node:/b', 'topic:/t', 'topic:/leaf', 'e1', 'e2', 'e4', 'action:/act',
                       'action:/act<node:/a', 'action:/act>node:/b'}
    assert desired(leaves=False) == default - {'topic:/leaf', 'e4'}
    assert desired(infrastructure=True) == default | {'topic:/rosout', 'e3'}
    assert desired(gateway=True) == default | {'node:/smpa_gui', 'e5'}
    assert desired(actions=False) == default - {'action:/act', 'action:/act<node:/a', 'action:/act>node:/b'}
    assert desired(topics=False, actions=False) == {'node:/a', 'node:/b', 'direct:node:/a>/t>node:/b'}


def test_layered_layout_flows_left_to_right_and_survives_cycles():
    """Publishers are placed left of their topics and subscribers; a feedback loop does not hang."""
    script = """import {layeredPositions} from './tabs/ros/graph.js';
      const nodes = ['a', 't', 'b', 'u', 'lonely'].map((id) => ({id, label: id}));
      const edges = [['a', 't'], ['t', 'b'], ['b', 'u'], ['u', 'a']].map(([source, target]) => ({source, target}));
      console.log(JSON.stringify(Object.fromEntries(layeredPositions(nodes, edges))));"""
    positions = run_module(script)
    assert positions['a']['x'] < positions['t']['x'] < positions['b']['x'] < positions['u']['x']
    assert set(positions) == {'a', 't', 'b', 'u', 'lonely'}


def test_sensor_conversions():
    """A quarter turn about z is a 90 degree yaw; Mercator puts 0°,0° in the middle of the map."""
    script = """import {rollPitchYaw} from './tabs/sensors/viewers/imu.js';
      import {mercator} from './tabs/sensors/viewers/navsatfix.js';
      import {niceStep} from './tabs/sensors/viewers/canvas.js';
      console.log(JSON.stringify({rpy: rollPitchYaw({x: 0, y: 0, z: Math.SQRT1_2, w: Math.SQRT1_2}),
        centre: mercator(0, 0, 1), north: mercator(60, 0, 1), steps: [niceStep(10, 4), niceStep(3, 4), niceStep(0.9, 4)]}));"""
    result = run_module(script)
    assert result['rpy'] == pytest.approx([0, 0, 90])
    assert result['centre'] == pytest.approx([256, 256]) and result['north'][1] < 256
    assert result['steps'] == pytest.approx([2, 1, 0.2])


def test_goal_form_defaults_merging_and_validation():
    """The goal editor's pure logic fills defaults, keeps given values and reports the same rules as the gateway."""
    script = """import {defaultFor, validate, withDefaults} from './tabs/actions/form.js';
      const integer = {kind: 'integer', type: 'int8', minimum: -128, maximum: 127};
      const big = {kind: 'integer', type: 'uint64', minimum: 0, maximum: 18446744073709551615};
      const schema = {kind: 'message', type: 'pkg/msg/Goal', fields: [
        {name: 'flag', schema: {kind: 'boolean', type: 'boolean'}}, {name: 'count', schema: integer},
        {name: 'big', schema: big}, {name: 'ratio', schema: {kind: 'float', type: 'double', limit: null}},
        {name: 'label', schema: {kind: 'string', type: 'string', max_length: 2}},
        {name: 'pair', schema: {kind: 'array', item: integer, fixed_size: 2, max_size: null}},
        {name: 'list', schema: {kind: 'array', item: {kind: 'message', type: 'pkg/msg/P', fields: [{name: 'x', schema: integer}]}, fixed_size: null, max_size: 1}}]};
      const base = defaultFor(schema);
      const paths = (value) => validate(schema, value).map((error) => error.path);
      console.log(JSON.stringify({base, merged: withDefaults(schema, {count: 5, list: [{}]}, base),
        good: paths({...base, count: -128, pair: [1, 2], list: [{x: 3}], label: 'ab'}),
        bad: paths({flag: 'yes', count: 200, big: 9007199254740993, ratio: '1', label: 'abc', pair: [1], list: [{x: 1.5}, {x: 1}], extra: 1}),
        notObject: paths([1])}));"""
    result = run_module(script)
    assert result['base'] == dict(flag=False, count=0, big=0, ratio=0, label='', pair=[0, 0], list=[])
    assert result['merged'] == dict(flag=False, count=5, big=0, ratio=0, label='', pair=[0, 0], list=[{'x': 0}])
    assert result['good'] == []
    assert result['bad'] == ['flag', 'count', 'big', 'ratio', 'label', 'pair', 'list', 'list[0].x', 'extra']
    assert result['notObject'] == ['']


def test_saved_configurations_store():
    """Configurations are stored per action type, survive re-reading, and tolerate broken storage."""
    script = """import {configurationNames, deleteConfiguration, loadConfiguration, saveConfiguration} from './tabs/actions/store.js';
      const data = new Map();
      const storage = {getItem: (key) => (data.has(key) ? data.get(key) : null), setItem: (key, value) => data.set(key, value)};
      saveConfiguration('a/action/T', 'home', {order: 3}, storage);
      saveConfiguration('a/action/T', 'away', {order: 9}, storage);
      saveConfiguration('b/action/U', 'home', {x: 1}, storage);
      const names = configurationNames('a/action/T', storage);
      const loaded = loadConfiguration('a/action/T', 'home', storage);
      deleteConfiguration('a/action/T', 'home', storage);
      const after = configurationNames('a/action/T', storage);
      const other = loadConfiguration('b/action/U', 'home', storage);
      data.set('smpa-gui.goal-configurations.v1', 'not json');
      const broken = configurationNames('a/action/T', storage);
      const refusing = {getItem: () => null, setItem: () => { throw new Error('full'); }};
      console.log(JSON.stringify({names, loaded, after, other, broken, refused: saveConfiguration('t', 'n', {}, refusing),
        missing: loadConfiguration('a/action/T', 'nothing', storage)}));"""
    result = run_module(script)
    assert result == dict(names=['away', 'home'], loaded={'order': 3}, after=['away'], other={'x': 1}, broken=[],
                          refused=False, missing=None)


PARAMETER = dict(name='level', type='integer', value=4, read_only=False, dynamic_typing=False,
                 integer_range={'from': 0, 'to': 10, 'step': 2}, floating_point_range=None, descriptor_known=True)


def parameter_check(parameter, type_name, text):
    """Ask the frontend to parse a typed value for a parameter and return its value and problems."""
    script = ("import {check, parseInput} from './tabs/ros/parameter_values.js';"
              f"const parsed = parseInput({json.dumps(type_name)}, {json.dumps(text)});"
              f"console.log(JSON.stringify({{value: parsed.value ?? null, problems: parsed.error ? [parsed.error] "
              f": check({json.dumps(parameter)}, {json.dumps(type_name)}, parsed.value)}}))")
    return run_module(script)


def test_parameter_proposals_follow_the_gateway_rules():
    """The page parses typed values per type and applies the same range, step, type and read-only rules as the gateway."""
    assert parameter_check(PARAMETER, 'integer', '6') == {'value': 6, 'problems': []}
    assert 'steps of 2' in parameter_check(PARAMETER, 'integer', '5')['problems'][0]
    assert 'within 0…10' in parameter_check(PARAMETER, 'integer', '12')['problems'][0]
    assert parameter_check(PARAMETER, 'integer', '10')['problems'] == []
    assert parameter_check(PARAMETER, 'integer', '2.5')['problems'] == ['must be a whole number']
    assert 'must stay of type integer' in parameter_check(PARAMETER, 'string', 'x')['problems'][0]
    assert parameter_check(dict(PARAMETER, dynamic_typing=True, integer_range=None), 'string', 'x')['problems'] == []
    assert parameter_check(dict(PARAMETER, read_only=True), 'integer', '6')['problems'][0].startswith('is read-only')
    speed = dict(PARAMETER, type='double', integer_range=None, floating_point_range={'from': 0, 'to': 1, 'step': 0.25})
    assert parameter_check(speed, 'double', '0.75')['problems'] == []
    assert 'steps of 0.25' in parameter_check(speed, 'double', '0.3')['problems'][0]
    names = dict(PARAMETER, type='string_array', integer_range=None)
    assert parameter_check(names, 'string_array', '["a", "b,c"]') == {'value': ['a', 'b,c'], 'problems': []}
    assert parameter_check(names, 'string_array', '["a", 1]')['problems'] == ['[1] must be text']
    assert 'JSON list' in parameter_check(names, 'string_array', 'a, b')['problems'][0]
    raw = dict(PARAMETER, type='byte_array', integer_range=None)
    assert parameter_check(raw, 'byte_array', '[0, 256]')['problems'] == ['[1] must be between 0 and 255']
    flag = dict(PARAMETER, type='bool', integer_range=None)
    assert parameter_check(flag, 'bool', 'false') == {'value': False, 'problems': []}


def test_non_finite_doubles_are_shown_by_name_and_never_proposed():
    """Names of non-finite doubles are displayed as such, start no edit text, fail every check, and open ranges work."""
    script = ("import {check, displayValue, editText, isNonFinite, parseInput} from './tabs/ros/parameter_values.js';"
              "const unbounded = {type: 'double', read_only: false, dynamic_typing: false, "
              "floating_point_range: {from: '-inf', to: 'inf', step: 0}};"
              "const stepped = {...unbounded, floating_point_range: {from: 0, to: 'inf', step: 0.5}};"
              "const list = {type: 'double_array', read_only: false, dynamic_typing: false};"
              "console.log(JSON.stringify({"
              "shown: [displayValue('double', 'nan'), displayValue('double', '-inf'), displayValue('double_array', [1.5, 'nan', 'inf']),"
              "        displayValue('string', 'nan'), displayValue('double', 0.25), displayValue('double', null)],"
              "named: [isNonFinite('double', 'inf'), isNonFinite('string', 'inf'), isNonFinite('double', 'infinity')],"
              "edit: [editText('double', 'nan'), editText('double', 'inf'), editText('double_array', [1.5, 'nan']), editText('string', 'nan')],"
              "parsed: [parseInput('double', 'nan').error, parseInput('double', 'Infinity').error],"
              "listProblems: check(list, 'double_array', JSON.parse(editText('double_array', [1.5, 'nan']))),"
              "openRange: [check(unbounded, 'double', 1e300), check(stepped, 'double', 2.5), check(stepped, 'double', 2.25).length,"
              "            check(stepped, 'double', -1).length]}))")
    assert run_module(script) == dict(
        shown=['nan', '-inf', '[1.5,nan,inf]', '"nan"', '0.25', '–'],
        named=[True, False, False],
        edit=['', '', '[1.5,"nan"]', 'nan'],
        parsed=['must be a number', 'must be a number'],
        listProblems=['[1] must be a finite number'],
        openRange=[[], [], 1, 1])
