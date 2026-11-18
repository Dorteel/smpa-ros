"""Drive the real frontend in a headless browser against the real server and fixtures.

These tests need Playwright with a Chromium browser. They are skipped when
Playwright is not installed, so the rest of the suite does not depend on it.
Screenshots are written to ``test/artifacts`` for visual inspection.
"""
import json
from pathlib import Path
import time

import pytest

from conftest import fetch

try:
    from playwright import sync_api as playwright
except ImportError:  # the browser tests are optional
    playwright = None
pytestmark = [pytest.mark.browser, pytest.mark.skipif(playwright is None, reason='Playwright is not installed')]
ARTIFACTS = Path(__file__).resolve().parent / 'artifacts'


def subscriptions(server):
    """Return the set of topics the gateway is subscribed to right now."""
    return {item['topic'] for item in fetch(server + '/api/v1/health')['active_subscriptions']}


def wait_for(condition, timeout=15.0, message='condition was not reached'):
    """Poll a condition until it holds or fail after the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.25)
    raise AssertionError(message)


@pytest.fixture(scope='module')
def page(server, browser):
    """Open the GUI in one page shared by the tests and fail on any page error.

    The tests in this module build on each other in order, like a user
    working through the interface: panels opened by one test are used by the
    next.
    """
    errors = []
    page = browser.new_page(viewport={'width': 1500, 'height': 950})
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
    page.goto(server + '/')
    yield page
    page.close()
    assert errors == [], f'The page reported errors: {errors}'


def test_shell_has_four_tabs_and_five_ros_views(page):
    """The primary tabs are ROS 2, Sensors, Actions and SMPA; the image viewer is not a ROS 2 view."""
    assert page.locator('#primary-tabs button').all_inner_texts() == ['ROS 2', 'Sensors', 'Actions', 'SMPA']
    assert page.locator('.subtabs button').all_inner_texts() == ['Graph', 'Topics', 'Parameters', 'Services', 'Actions']
    page.wait_for_function("document.getElementById('connection').textContent.includes('domain')")


def graph_labels(page, selector='node'):
    """Return the labels of the graph elements matching a Cytoscape selector."""
    return page.evaluate(f"window.__graph.elements('{selector}').map((e) => e.data('label') || e.id())")


def test_graph_shows_connections_filters_and_keeps_the_viewport(page):
    """The graph shows fixture connections, hides the GUI by default, and refreshing never moves the view."""
    page.goto(page.url.split('#')[0] + '#ros/graph')
    page.wait_for_function("window.__graph && window.__graph.getElementById('node:/fixture_talker').length === 1")
    labels = graph_labels(page)
    assert '/fixture_listener' in labels and '/fixture/chatter' in labels and '/fixture/fibonacci' in labels
    assert '/smpa_gui' not in labels and '/rosout' not in labels and '/parameter_events' not in labels
    assert page.evaluate("window.__graph.getElementById('node:/fixture_talker').edgesTo('[id = \"topic:/fixture/chatter\"]').length") == 1
    page.screenshot(path=str(ARTIFACTS / 'ros-graph.png'))

    page.evaluate("() => { window.__graph.zoom(1.7); window.__graph.pan({x: 123, y: -45}); }")
    before = page.evaluate("window.__graph.getElementById('node:/fixture_talker').position()")
    time.sleep(5)
    assert page.evaluate("window.__graph.zoom()") == pytest.approx(1.7)
    assert page.evaluate("window.__graph.pan()") == {'x': 123, 'y': -45}
    assert page.evaluate("window.__graph.getElementById('node:/fixture_talker').position()") == before

    page.fill('#graph-search', 'chatter')
    assert page.evaluate("window.__graph.getElementById('topic:/fixture/chatter').hasClass('dim')") is False
    assert page.evaluate("window.__graph.getElementById('node:/fixture_sensors').hasClass('dim')") is True
    page.fill('#graph-search', '')
    page.uncheck('input[data-filter="topics"]')
    assert '/fixture/chatter' not in graph_labels(page, 'node')
    assert '/fixture/chatter' in graph_labels(page, 'edge[kind = "direct"]')
    page.check('input[data-filter="topics"]')
    page.check('input[data-filter="gateway"]')
    page.check('input[data-filter="infrastructure"]')
    assert {'/smpa_gui', '/rosout'} <= set(graph_labels(page))
    page.uncheck('input[data-filter="gateway"]')
    page.uncheck('input[data-filter="infrastructure"]')
    page.evaluate("() => { window.__graph.getElementById('node:/fixture_talker').emit('tap'); }")
    assert '/fixture/chatter' in page.inner_text('.graph-side')


def test_topics_view_shows_endpoints_and_live_messages(page, server):
    """Selecting a topic lists its endpoints; the echo subscribes only while it runs."""
    page.goto(page.url.split('#')[0] + '#ros/topics')
    page.click('tr[data-topic="/fixture/chatter"]')
    page.wait_for_selector('#echo-toggle')
    detail = page.inner_text('#topic-detail')
    assert 'std_msgs/msg/String' in detail and '/fixture_talker' in detail and '/fixture_listener' in detail
    assert 'reliable' in detail and subscriptions(server) == set()
    page.click('#echo-toggle')
    page.wait_for_function("document.getElementById('echo-output').textContent.includes('fixture message')")
    assert subscriptions(server) == {'/fixture/chatter'}
    page.screenshot(path=str(ARTIFACTS / 'ros-topics.png'))
    page.click('#echo-toggle')
    wait_for(lambda: subscriptions(server) == set(), message='stopping the echo did not unsubscribe')


def test_parameters_view_handles_responsive_and_silent_nodes(page):
    """A responsive node shows its parameters; a silent node shows an explanation instead of an error."""
    page.goto(page.url.split('#')[0] + '#ros/parameters//fixture_ns/fixture_parameters')
    page.wait_for_selector('#parameter-table')
    table = page.inner_text('#parameter-table')
    assert 'fixture_integer' in table and '42' in table and 'bool_array' in table and '"hello"' in table
    page.screenshot(path=str(ARTIFACTS / 'ros-parameters.png'))
    page.click('tr[data-node="/fixture_silent"]')
    page.wait_for_selector('#parameter-state[data-state="not_responding"]', timeout=10000)
    assert 'did not answer' in page.inner_text('#parameter-detail')


def test_actions_view_lists_servers_and_clients(page):
    """The action table shows the fixture action with its type, server and client."""
    page.goto(page.url.split('#')[0] + '#ros/actions')
    page.wait_for_selector('tr[data-action="/fixture/fibonacci"]')
    row = page.inner_text('tr[data-action="/fixture/fibonacci"]')
    assert 'example_interfaces/action/Fibonacci' in row and '/fixture_action_server' in row and '/fixture_action_client' in row


def open_sensor(page, topic):
    """Open a discovered sensor topic in a new panel and return the panel locator."""
    page.click(f'.sensor-row[data-topic="{topic}"] button')
    return page.locator('.panel').last


def test_sensor_panels_render_every_supported_type(page, server):
    """Each sensor type renders live data in its own panel, all at the same time."""
    page.goto(page.url.split('#')[0] + '#sensors')
    page.wait_for_selector('.sensor-row[data-topic="/fixture/scan"]')
    assert subscriptions(server) == set(), 'discovery alone must not subscribe to sensor topics'

    image = open_sensor(page, '/fixture/camera/image_raw')
    page.wait_for_function("Number(document.querySelector('.panel img').dataset.frames) >= 3")
    assert image.locator('img').evaluate('(img) => img.naturalWidth') == 64
    compressed = open_sensor(page, '/fixture/camera/image_raw/compressed')
    compressed.locator('img[data-frames]').wait_for()

    cloud = open_sensor(page, '/fixture/points')
    cloud.locator('canvas[data-points="4999"]').wait_for()
    scan = open_sensor(page, '/fixture/scan')
    scan.locator('canvas[data-points="359"]').wait_for()
    imu = open_sensor(page, '/fixture/imu')
    imu.locator('[data-field="rpy"]').wait_for()
    assert imu.locator('[data-field="rpy"]').inner_text() == '0.0°, 0.0°, 90.0°'
    joints = open_sensor(page, '/fixture/joint_states')
    joints.locator('tr[data-joint="fixture_joint_b"]').wait_for()
    assert '0.5000' in joints.locator('tr[data-joint="fixture_joint_b"]').inner_text()
    gps = open_sensor(page, '/fixture/gps/fix')
    gps.locator('canvas[data-fixes="1"]').wait_for()
    assert gps.locator('[data-field="position"]').inner_text() == '52.334000°, 4.866000°'
    distance = open_sensor(page, '/fixture/range')
    distance.locator('[data-field="range"]').filter(has_text='1.000 m').wait_for()
    calibration = open_sensor(page, '/fixture/camera/camera_info')
    calibration.locator('[data-field="focal"]').wait_for()
    assert calibration.locator('[data-field="size"]').inner_text() == '64 × 48'
    assert 'fx 50.00, fy 50.00' in calibration.locator('[data-field="focal"]').inner_text()

    assert page.locator('.panel').count() == 9
    wait_for(lambda: len(subscriptions(server)) == 9)
    for state in page.locator('.panel-state').all_inner_texts():
        assert state == 'streaming'
    time.sleep(1)
    page.screenshot(path=str(ARTIFACTS / 'sensors.png'), full_page=True)
    cloud.screenshot(path=str(ARTIFACTS / 'sensor-pointcloud.png'))


def camera(panel):
    """Read the 3D viewer's camera state from the canvas."""
    return json.loads(panel.locator('canvas').get_attribute('data-camera'))


def test_point_cloud_viewer_rotates_zooms_and_pans(page):
    """Dragging rotates, the wheel zooms and Shift-dragging pans the 3D view."""
    panel = page.locator('.panel[data-viewer="pointcloud"]')
    panel.scroll_into_view_if_needed()
    box = panel.locator('canvas').bounding_box()
    x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
    start = camera(panel)
    page.mouse.move(x, y); page.mouse.down(); page.mouse.move(x + 80, y + 30, steps=4); page.mouse.up()
    page.wait_for_timeout(200)
    rotated = camera(panel)
    assert rotated['yaw'] != start['yaw'] and rotated['pitch'] != start['pitch']
    assert rotated['distance'] == start['distance'] and rotated['target'] == start['target']
    page.mouse.move(x, y); page.mouse.wheel(0, -400)
    page.wait_for_timeout(200)
    zoomed = camera(panel)
    assert zoomed['distance'] < rotated['distance'] and zoomed['yaw'] == rotated['yaw']
    page.keyboard.down('Shift'); page.mouse.down(); page.mouse.move(x + 60, y, steps=3); page.mouse.up(); page.keyboard.up('Shift')
    page.wait_for_timeout(200)
    panned = camera(panel)
    assert panned['target'] != zoomed['target'] and panned['yaw'] == zoomed['yaw'] and panned['distance'] == zoomed['distance']


def test_topic_switching_unsupported_data_and_panel_closing(page, server):
    """Switching a panel's topic moves its subscription; unsupported data is explained; closing unsubscribes."""
    panel = page.locator('.panel[data-viewer="image"]').first
    panel.locator('select').select_option('/fixture/camera/depth')
    panel.locator('.panel-foot', has_text='contrast-stretched').first.wait_for()
    wait_for(lambda: '/fixture/camera/depth' in subscriptions(server) and '/fixture/camera/image_raw' not in subscriptions(server),
             message='switching topics did not move the subscription')
    panel.locator('select').select_option('/fixture/camera/unsupported')
    panel.locator('.panel-state[data-state="unsupported"]').wait_for()
    assert 'bayer_rggb16' in panel.locator('.panel-foot').last.inner_text()
    assert panel.locator('img').get_attribute('src') is None, 'the previous topic\'s picture must not linger'
    before = page.locator('.panel').count()
    panel.locator('button[title="Close panel"]').click()
    assert page.locator('.panel').count() == before - 1
    wait_for(lambda: '/fixture/camera/unsupported' not in subscriptions(server))


def test_leaving_the_sensors_tab_unsubscribes_and_returning_restores_panels(page, server):
    """Sensor subscriptions exist only while the Sensors tab is shown; panels come back afterwards."""
    panels = page.locator('.panel').count()
    assert panels == 8 and len(subscriptions(server)) == 8
    page.click('#primary-tabs button[data-tab="smpa"]')
    page.wait_for_selector('#session-select')
    page.screenshot(path=str(ARTIFACTS / 'smpa.png'))
    wait_for(lambda: subscriptions(server) == set(), message='leaving the Sensors tab left subscriptions behind')
    page.click('#primary-tabs button[data-tab="sensors"]')
    page.wait_for_selector('.panel')
    wait_for(lambda: page.locator('.panel').count() == panels, message='not every sensor panel came back')
    wait_for(lambda: len(subscriptions(server)) == 8)
    page.click('#add-panel')
    empty = page.locator('.panel').last
    assert empty.locator('.panel-state').inner_text() == 'idle'
    while page.locator('.panel').count():
        page.locator('.panel button[title="Close panel"]').first.click()
    wait_for(lambda: subscriptions(server) == set())


def test_sensors_tab_without_any_sensor_publishers(lonely_server, browser):
    """With no sensors on the ROS graph the Sensors tab explains that and panels wait quietly."""
    base, _, _ = lonely_server
    errors = []
    page = browser.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(base + '/#sensors')
    page.wait_for_selector('#sensors-none')
    page.select_option('#add-viewer', 'laserscan')
    page.click('#add-panel')
    panel = page.locator('.panel')
    assert panel.locator('.panel-state').inner_text() == 'idle'
    assert 'no compatible topics' in panel.locator('select').inner_text()
    page.goto(base + '/#ros/graph')
    page.wait_for_function("document.getElementById('graph-summary').textContent.startsWith('0 nodes')")
    page.screenshot(path=str(ARTIFACTS / 'empty-graph.png'))
    page.close()
    assert errors == []
