"""Drive the ROS 2 Workspace in a headless browser against real ROS 2 fixture nodes.

The page is opened at ``#ros/workspace`` beside the shared fixture nodes and
the management fixtures. The tests check the five regions, that every
peripheral panel shows real discovery data, that chips open the existing
inspectors, that a parameter change still needs the existing confirmation,
that panels follow nodes appearing and disappearing, and that the canvas
stays empty while zoom, pan, reset and fit work and survive refreshes.
"""
from pathlib import Path

import pytest

from conftest import fetch, stop
from test_browser_management import parameter_value
from test_management import SETTINGS, start_fixture, wait_until
from test_workspace import start_dynamic

pytestmark = pytest.mark.browser
ARTIFACTS = Path(__file__).resolve().parent / 'artifacts'


def chip(kind, name, node=None):
    """Return the CSS selector of one interface chip; parameters are also selected by their node."""
    selector = f'.ws-chip[data-kind="{kind}"][data-name="{name}"]'
    return selector + (f'[data-node="{node}"]' if node else '')


def viewport(page):
    """Read the canvas viewport the page publishes on the canvas element, as plain numbers."""
    canvas = page.locator('#workspace-canvas')
    return dict(zoom=float(canvas.get_attribute('data-zoom')), x=float(canvas.get_attribute('data-pan-x')),
                y=float(canvas.get_attribute('data-pan-y')), elements=int(canvas.get_attribute('data-elements')))


def canvas_size(page):
    """Return the canvas element's width and height in pixels."""
    return page.evaluate("() => { const c = document.getElementById('workspace-canvas'); return [c.clientWidth, c.clientHeight]; }")


def open_workspace(page):
    """Show the Workspace and wait until the first discovery of every panel has been drawn."""
    page.click('nav[aria-label="ROS 2 views"] button[data-view="workspace"]')
    page.wait_for_selector('#workspace-canvas')
    page.wait_for_selector(chip('topic', '/fixture/chatter'))
    page.wait_for_selector(chip('parameter', 'level', SETTINGS), timeout=20000)


@pytest.fixture(scope='module')
def page(ros_environment, server, browser):
    """Start the management fixtures and open the Workspace in one page; fail on any page error."""
    process = start_fixture(ros_environment)
    wait_until(lambda: fetch(f'{server}/api/v1/ros/nodes/parameters?node={SETTINGS}')['state'] == 'ok')
    errors = []
    context = browser.new_context(viewport={'width': 1500, 'height': 950})
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(server + '/#ros/workspace')
    page.wait_for_selector(chip('parameter', 'level', SETTINGS), timeout=20000)
    yield page
    context.close()
    stop(process)
    assert errors == [], f'The page reported errors: {errors}'


def test_workspace_has_five_regions_with_the_canvas_dominant(page):
    """Topics on top, services left, actions right, parameters at the bottom and the largest region is the canvas."""
    assert 'Workspace' in page.locator('nav[aria-label="ROS 2 views"] button').all_inner_texts()
    boxes = {region: page.locator(f'[data-panel="{region}"]').bounding_box()
             for region in ('topics', 'services', 'actions', 'parameters', 'canvas')}
    canvas = boxes['canvas']
    assert boxes['topics']['y'] + boxes['topics']['height'] <= canvas['y'] + 1
    assert boxes['services']['x'] + boxes['services']['width'] <= canvas['x'] + 1
    assert boxes['actions']['x'] >= canvas['x'] + canvas['width'] - 1
    assert boxes['parameters']['y'] >= canvas['y'] + canvas['height'] - 1
    area = {region: box['width'] * box['height'] for region, box in boxes.items()}
    assert area['canvas'] == max(area.values())
    page.screenshot(path=str(ARTIFACTS / 'workspace.png'))


def test_topics_appear_with_their_message_types(page):
    """The top panel shows each topic with its message type; filtering narrows it; hidden topics are optional."""
    chatter = page.locator(chip('topic', '/fixture/chatter'))
    assert chatter.get_attribute('data-type') == 'std_msgs/msg/String' and 'std_msgs/msg/String' in chatter.inner_text()
    assert page.locator(chip('topic', '/fixture/scan')).get_attribute('data-type') == 'sensor_msgs/msg/LaserScan'
    assert page.locator(chip('topic', '/fixture/chatter')).get_attribute('data-state') == 'ok'
    assert page.locator('[data-panel="topics"] .ws-chip[data-name*="/_action/"]').count() == 0
    page.fill('[data-panel="topics"] .ws-filter', 'chatter')
    page.wait_for_function("document.querySelectorAll('[data-panel=topics] .ws-chip').length === 1")
    assert page.inner_text('[data-panel="topics"] .ws-count').startswith('1 of ')
    page.check('[data-panel="topics"] input[data-option="hidden"]')
    page.fill('[data-panel="topics"] .ws-filter', '/_action/')
    page.wait_for_selector('[data-panel="topics"] .ws-chip[data-name="/fixture/fibonacci/_action/status"]')
    page.uncheck('[data-panel="topics"] input[data-option="hidden"]')
    page.fill('[data-panel="topics"] .ws-filter', '')
    page.wait_for_selector(chip('topic', '/fixture/scan'))


def test_services_appear_in_the_left_panel(page):
    """The left panel lists services with types; parameter plumbing appears only when asked for."""
    service = page.locator(chip('service', '/fixture/add_two_ints'))
    assert service.get_attribute('data-type') == 'example_interfaces/srv/AddTwoInts'
    assert page.locator(chip('service', '/fixture_talker/get_parameters')).count() == 0
    page.check('[data-panel="services"] input[data-option="plumbing"]')
    page.wait_for_selector(chip('service', '/fixture_talker/get_parameters'))
    page.uncheck('[data-panel="services"] input[data-option="plumbing"]')
    page.wait_for_selector(chip('service', '/fixture_talker/get_parameters'), state='detached')
    page.fill('[data-panel="services"] .ws-filter', 'AddTwoInts')
    page.wait_for_function("document.querySelectorAll('[data-panel=services] .ws-chip').length === 1")
    page.fill('[data-panel="services"] .ws-filter', '')


def test_actions_appear_in_the_right_panel(page):
    """The right panel lists actions with their types."""
    assert page.locator(chip('action', '/fixture/fibonacci')).get_attribute('data-type') == 'example_interfaces/action/Fibonacci'
    assert page.locator(chip('action', '/fixture/lookup_transform')).get_attribute('data-type') == 'tf2_msgs/action/LookupTransform'
    assert page.locator(chip('action', '/fixture/fibonacci')).get_attribute('data-state') == 'ok'


def test_parameters_appear_grouped_by_their_nodes(page):
    """The bottom panel groups parameters under their owning node with values and types; silent nodes are explained."""
    group = page.locator('.ws-group[data-node="/fixture_ns/fixture_parameters"]')
    assert group.get_attribute('data-state') == 'ok'
    integer = group.locator('.ws-chip[data-name="fixture_integer"]')
    assert integer.get_attribute('data-value') == '42' and integer.get_attribute('data-type') == 'integer'
    assert '42' in integer.inner_text() and 'integer' in integer.inner_text()
    assert group.locator('.ws-chip[data-name="fixture_string"]').get_attribute('data-value') == '"hello"'
    assert page.locator(chip('parameter', 'fixture_integer')).count() == 1  # only under its own node
    page.wait_for_selector('.ws-group[data-node="/fixture_silent"][data-state="not_responding"]')
    assert 'not responding' in page.inner_text('.ws-group[data-node="/fixture_silent"]')
    assert page.locator(chip('parameter', 'frozen', SETTINGS)).inner_text().count('read-only') == 1
    page.fill('[data-panel="parameters"] .ws-filter', 'fixture_double')
    page.wait_for_function("document.querySelectorAll('[data-panel=parameters] .ws-group').length === 1")
    assert page.locator('[data-panel="parameters"] .ws-chip').count() == 1
    page.fill('[data-panel="parameters"] .ws-filter', '')


def test_chips_open_the_existing_inspectors(page):
    """A topic, service, action or parameter chip opens the existing inspector for exactly that interface."""
    page.click(chip('topic', '/fixture/chatter'))
    page.wait_for_function("document.querySelector('#topic-detail h2') && "
                           "document.querySelector('#topic-detail h2').textContent === '/fixture/chatter'")
    assert page.url.endswith('#ros/topics//fixture/chatter')
    open_workspace(page)
    page.click(chip('service', '/fixture/add_two_ints'))
    page.wait_for_function("document.getElementById('service-title') && "
                           "document.getElementById('service-title').textContent === '/fixture/add_two_ints'")
    open_workspace(page)
    page.click(chip('action', '/fixture/fibonacci'))
    page.wait_for_function("document.getElementById('editor-action') && "
                           "document.getElementById('editor-action').textContent === '/fixture/fibonacci'")
    assert page.url.endswith('#actions//fixture/fibonacci')
    page.wait_for_selector('#goal-editor input')
    page.goto(page.url.split('#')[0] + '#ros/workspace')
    page.wait_for_selector(chip('parameter', 'level', SETTINGS), timeout=20000)
    page.click(chip('parameter', 'level', SETTINGS))
    page.wait_for_selector('#parameter-table tr[data-parameter="level"][data-focused]')
    assert page.inner_text('#parameter-detail h2') == SETTINGS
    assert page.locator('#parameter-table tr[data-focused]').count() == 1
    open_workspace(page)


def test_parameter_change_from_the_workspace_needs_the_existing_confirmation(page, server):
    """Editing a parameter reached from the Workspace changes nothing until the existing dialog is confirmed."""
    page.click(chip('parameter', 'level', SETTINGS))
    page.wait_for_selector('#parameter-table tr[data-parameter="level"][data-focused]')
    before = parameter_value(server, 'level')
    proposed = 6 if before != 6 else 8
    page.click('button[data-edit="level"]')
    page.fill('tr[data-parameter="level"] .proposal-input', str(proposed))
    page.click('#parameter-apply')
    page.wait_for_selector('#parameter-dialog[open]')
    assert parameter_value(server, 'level') == before  # nothing is sent before the confirmation
    page.click('#parameter-confirm')
    page.wait_for_selector('#parameter-outcome[data-outcome="applied"]')
    assert parameter_value(server, 'level') == proposed
    open_workspace(page)
    page.wait_for_selector(chip('parameter', 'level', SETTINGS) + f'[data-value="{proposed}"]', timeout=15000)


def test_panels_follow_entities_appearing_and_disappearing(page, ros_environment):
    """Starting a node adds its topic, service, action and parameter chips; stopping it marks them gone, then removes them."""
    added = [chip('topic', '/fixture_ws/dynamic_topic'), chip('service', '/fixture_ws/dynamic_service'),
             chip('action', '/fixture_ws/dynamic_action'), chip('parameter', 'dynamic_threshold', '/fixture_workspace/dynamic')]
    for selector in added:
        assert page.locator(selector).count() == 0
    process = start_dynamic(ros_environment)
    try:
        for selector in added:
            page.wait_for_selector(selector + '[data-state="ok"]', timeout=30000)
        assert page.locator(added[3]).get_attribute('data-value') == '0.5'
    finally:
        stop(process)
    page.wait_for_selector(added[0] + '[data-state="gone"]', timeout=30000)
    page.wait_for_selector(added[2] + '[data-state="gone"]', timeout=30000)
    for selector in added:
        page.wait_for_selector(selector, state='detached', timeout=45000)


def test_unreachable_gateway_is_shown_with_the_last_known_interfaces(page):
    """When a discovery request fails, the panel keeps its chips, dims them and says the gateway is unreachable."""
    page.route('**/api/v1/ros/actions', lambda route: route.abort())
    try:
        page.wait_for_selector('[data-panel="actions"][data-offline]', timeout=10000)
        assert 'unreachable' in page.inner_text('[data-panel="actions"] .ws-status')
        assert page.locator(chip('action', '/fixture/fibonacci')).count() == 1
    finally:
        page.unroute('**/api/v1/ros/actions')
    page.wait_for_selector('[data-panel="actions"]:not([data-offline])', timeout=10000)


def test_canvas_stays_empty(page):
    """Nothing discovered is ever placed on the canvas; it only shows the empty-canvas hint."""
    assert viewport(page)['elements'] == 0
    assert page.is_visible('#workspace-canvas-hint')
    assert page.locator('#workspace-canvas .ws-chip').count() == 0


def test_zoom_pan_reset_and_fit(page):
    """Buttons, wheel and dragging change the viewport; reset centres the origin at 100 %; fit frames the sheet."""
    width, height = canvas_size(page)
    page.click('#workspace-reset')
    assert viewport(page) == dict(zoom=1.0, x=round(width / 2, 1), y=round(height / 2, 1), elements=0)
    page.click('#workspace-zoom-in')
    assert viewport(page)['zoom'] == pytest.approx(1.25, abs=1e-3)
    assert page.inner_text('#workspace-zoom') == '125 %'
    page.click('#workspace-zoom-out')
    assert viewport(page)['zoom'] == pytest.approx(1.0, abs=1e-3)
    box = page.locator('#workspace-canvas').bounding_box()
    centre = (box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
    page.mouse.move(*centre)
    page.mouse.wheel(0, -400)
    page.wait_for_function("Number(document.getElementById('workspace-canvas').dataset.zoom) > 1.02")
    page.click('#workspace-reset')
    page.mouse.move(*centre)
    page.mouse.down()
    page.mouse.move(centre[0] + 60, centre[1] + 30, steps=4)
    page.mouse.move(centre[0] + 120, centre[1] + 60, steps=4)
    page.mouse.up()
    moved = viewport(page)
    assert moved['x'] == pytest.approx(width / 2 + 120, abs=3) and moved['y'] == pytest.approx(height / 2 + 60, abs=3)
    page.click('#workspace-fit')
    expected = max(0.1, min(4.0, min((width - 80) / 1200, (height - 80) / 800)))
    assert viewport(page) == dict(zoom=pytest.approx(expected, abs=1e-3), x=pytest.approx(width / 2, abs=0.2),
                                  y=pytest.approx(height / 2, abs=0.2), elements=0)
    page.focus('#workspace-canvas')
    page.keyboard.press('0')
    assert viewport(page)['zoom'] == 1.0
    page.uncheck('#workspace-grid')
    assert page.get_attribute('#workspace-canvas', 'data-grid') == 'false'
    page.check('#workspace-grid')
    assert page.get_attribute('#workspace-canvas', 'data-grid') == 'true'


def test_viewport_survives_refreshes_and_inspector_visits(page):
    """Discovery refreshes and a visit to an inspector leave the canvas viewport exactly as it was."""
    page.click('#workspace-reset')
    page.click('#workspace-zoom-in')
    page.focus('#workspace-canvas')
    page.keyboard.press('ArrowLeft')
    before = viewport(page)
    page.wait_for_timeout(5500)  # at least two topic refreshes and one parameter refresh
    assert viewport(page) == before
    page.click(chip('topic', '/fixture/chatter'))
    page.wait_for_selector('#topic-detail h2')
    open_workspace(page)
    assert viewport(page) == before


def test_panels_collapse_and_resize(page):
    """A panel collapses to its header (and stops refreshing) and back; dragging a splitter resizes it, remembered."""
    services = page.locator('[data-panel="services"]')
    canvas_before = page.locator('[data-panel="canvas"]').bounding_box()['width']
    page.click('[data-panel="services"] .ws-collapse')
    page.wait_for_function("document.querySelector('[data-panel=services]').classList.contains('collapsed')")
    assert services.bounding_box()['width'] <= 40
    assert page.locator('[data-panel="canvas"]').bounding_box()['width'] > canvas_before + 150
    requests = []
    page.on('request', lambda request: requests.append(request.url) if '/api/v1/ros/services' in request.url else None)
    page.wait_for_timeout(4500)  # two refresh periods: a collapsed panel asks for nothing
    assert requests == []
    page.click('[data-panel="services"] .ws-collapse')
    page.wait_for_function("document.querySelectorAll('[data-panel=services] .ws-chip').length > 0")
    assert requests
    width = services.bounding_box()['width']
    assert width > 150
    splitter = page.locator('.ws-split[data-edge="left"]').bounding_box()
    x, y = splitter['x'] + splitter['width'] / 2, splitter['y'] + splitter['height'] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + 80, y, steps=5)
    page.mouse.up()
    assert services.bounding_box()['width'] == pytest.approx(width + 80, abs=4)
    page.reload()
    page.wait_for_selector('#workspace-canvas')
    assert page.locator('[data-panel="services"]').bounding_box()['width'] == pytest.approx(width + 80, abs=4)
    page.evaluate("() => { localStorage.removeItem('smpa-gui.workspace.layout'); }")
    page.reload()
    page.wait_for_selector(chip('parameter', 'level', SETTINGS), timeout=20000)


def test_layout_adapts_to_a_narrow_window(server, browser):
    """In a narrow window the regions stack, the splitters disappear and the canvas keeps a usable height."""
    context = browser.new_context(viewport={'width': 600, 'height': 900})
    narrow = context.new_page()
    try:
        narrow.goto(server + '/#ros/workspace')
        narrow.wait_for_selector(chip('topic', '/fixture/chatter'))
        assert not narrow.is_visible('.ws-split[data-edge="left"]')
        canvas = narrow.locator('[data-panel="canvas"]').bounding_box()
        assert canvas['height'] >= 320 and canvas['width'] <= 600
        assert narrow.evaluate('() => document.documentElement.scrollWidth') <= 600
    finally:
        context.close()
