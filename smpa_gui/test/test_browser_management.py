"""Drive the Parameters and Services panels in a headless browser against real ROS 2 fixture nodes."""
import json
from pathlib import Path
import time

import pytest

from conftest import fetch, start_server, stop
from test_management import SETTINGS, start_fixture, wait_until

pytestmark = pytest.mark.browser
ARTIFACTS = Path(__file__).resolve().parent / 'artifacts'


def parameter_value(server, name):
    """Read one parameter of the settings node through the GUI's API."""
    result = fetch(f'{server}/api/v1/ros/nodes/parameters?node={SETTINGS}')
    return next(item['value'] for item in result['parameters'] if item['name'] == name)


def call_count(server, name):
    """Ask the fixture how often a service was called, through the API (this query is not counted)."""
    from test_management import counts
    token = fetch(server + '/api/v1/session')['token']
    return counts(dict(base=server, token=token)).get(name, 0)


@pytest.fixture(scope='module')
def page(ros_environment, server, browser):
    """Start the management fixtures and open one page; fail on any page error."""
    process = start_fixture(ros_environment)
    wait_until(lambda: any(item['name'] == '/fixture/add_two_ints'
                           for item in fetch(server + '/api/v1/ros/services')['services']))
    wait_until(lambda: fetch(f'{server}/api/v1/ros/nodes/parameters?node={SETTINGS}')['state'] == 'ok')
    errors = []
    context = browser.new_context(viewport={'width': 1500, 'height': 1000})
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(server + '/#ros/parameters' + SETTINGS)
    page.wait_for_selector('#parameter-table')
    yield page
    context.close()
    stop(process)
    assert errors == [], f'The page reported errors: {errors}'


def test_ros_tab_offers_parameters_and_services(page):
    """The ROS 2 tab has Graph, Workspace, Topics, Parameters, Services and Actions views."""
    assert page.locator('nav[aria-label="ROS 2 views"] button').all_inner_texts() == ['Graph', 'Workspace', 'Topics', 'Parameters', 'Services', 'Actions']


def test_parameters_show_constraints_and_read_only(page):
    """Constraints and descriptions are shown; read-only parameters offer no edit button."""
    row = page.inner_text('tr[data-parameter="level"]')
    assert '0…10 step 2' in row and 'Even level' in row
    assert 'read-only' in page.inner_text('tr[data-parameter="frozen"]')
    assert page.locator('button[data-edit="frozen"]').count() == 0
    assert page.is_visible('#parameter-warning')


def test_invalid_proposal_blocks_apply_and_nothing_is_sent_without_confirmation(page, server):
    """An out-of-range value is flagged and blocks Apply; declining the dialog changes nothing."""
    page.click('button[data-edit="level"]')
    page.fill('tr[data-parameter="level"] .proposal-input', '5')
    assert 'steps of 2' in page.inner_text('tr[data-parameter="level"] .proposal-note')
    assert page.is_disabled('#parameter-apply')
    page.fill('tr[data-parameter="level"] .proposal-input', '6')
    assert page.inner_text('tr[data-parameter="level"] .proposal-note') == ''
    page.click('button[data-edit="label"]')
    page.fill('tr[data-parameter="label"] .proposal-input', 'from-browser')
    assert '2 proposed changes' in page.inner_text('#parameter-pending')
    page.click('#parameter-apply')
    page.wait_for_selector('#parameter-dialog[open]')
    dialog = page.inner_text('#parameter-confirm-changes')
    assert '4 (integer)' in dialog and '6 (integer)' in dialog and '"from-browser" (string)' in dialog
    page.click('#parameter-decline')
    time.sleep(1.0)
    assert parameter_value(server, 'level') == 4 and parameter_value(server, 'label') == 'alpha'


def test_confirmed_changes_are_applied_once_and_shown(page, server):
    """Confirming applies the changes, shows the node's answers and the new values, and clears the proposals."""
    page.click('#parameter-apply')
    page.wait_for_selector('#parameter-dialog[open]')
    page.screenshot(path=str(ARTIFACTS / 'parameters-confirm.png'))
    page.dblclick('#parameter-confirm')
    page.wait_for_selector('#parameter-outcome[data-outcome="applied"]')
    assert '4 → 6' in page.inner_text('[data-result="level"]')
    assert '6' in page.inner_text('tr[data-parameter="level"] td:nth-child(3)')
    assert parameter_value(server, 'level') == 6 and parameter_value(server, 'label') == 'from-browser'
    changes = fetch(server + '/api/v1/ros/parameters/changes')['changes']
    assert len([change for change in changes if change['node'] == SETTINGS and change['outcome'] == 'applied'
                and {result['name'] for result in change['results']} == {'level', 'label'}]) == 1
    assert page.locator('.proposal').count() == 0
    page.screenshot(path=str(ARTIFACTS / 'parameters.png'))


def test_node_rejection_and_partial_application_are_reported(page, server):
    """Individually applied changes report the node's rejection and keep the rejected proposal for correction."""
    page.click('button[data-edit="guarded"]')
    page.fill('tr[data-parameter="guarded"] .proposal-input', 'forbidden')
    page.click('button[data-edit="enabled"]')
    page.select_option('tr[data-parameter="enabled"] .proposal-input', 'false')
    page.uncheck('#parameter-atomic')
    page.click('#parameter-apply')
    page.click('#parameter-confirm')
    page.wait_for_selector('#parameter-outcome[data-outcome="partial"]')
    assert 'forbidden is not allowed here' in page.inner_text('[data-result="guarded"]')
    assert parameter_value(server, 'enabled') is False and parameter_value(server, 'guarded') == 'ok'
    assert page.locator('tr[data-parameter="guarded"] .proposal').count() == 1


def test_array_and_dynamic_type_editing(page, server):
    """Arrays are edited as JSON lists; a dynamically typed parameter offers a type choice."""
    page.click('button[data-discard="guarded"]')
    page.click('button[data-edit="gains"]')
    page.fill('tr[data-parameter="gains"] .proposal-input', '[3, 4.5')
    assert 'JSON list' in page.inner_text('tr[data-parameter="gains"] .proposal-note')
    page.fill('tr[data-parameter="gains"] .proposal-input', '[3, 4.5]')
    page.click('button[data-edit="anything"]')
    assert page.is_enabled('tr[data-parameter="anything"] .proposal-type')
    assert page.is_disabled('tr[data-parameter="gains"] .proposal-type')
    page.select_option('tr[data-parameter="anything"] .proposal-type', 'string_array')
    page.fill('tr[data-parameter="anything"] .proposal-input', '["x", "y"]')
    page.check('#parameter-atomic')
    page.click('#parameter-apply')
    page.click('#parameter-confirm')
    page.wait_for_selector('#parameter-outcome[data-outcome="applied"]')
    assert parameter_value(server, 'gains') == [3.0, 4.5] and parameter_value(server, 'anything') == ['x', 'y']


def test_unresponsive_node_is_explained(page):
    """A node that never answers is explained instead of offering edits."""
    page.goto(page.url.split('#')[0] + '#ros/parameters/fixture_silent')
    page.wait_for_selector('#parameter-state[data-state="not_responding"]', timeout=10000)
    assert page.locator('#parameter-apply').count() == 0


def test_services_list_filters_and_interface(page):
    """Services are listed with providers; plumbing is hidden by default; nested request forms are generated."""
    page.goto(page.url.split('#')[0] + '#ros/services')
    page.wait_for_selector('tr[data-service="/fixture/add_two_ints"]')
    assert '/fixture_mgmt/services' in page.inner_text('tr[data-service="/fixture/add_two_ints"]')
    assert page.locator(f'tr[data-service="{SETTINGS}/set_parameters"]').count() == 0
    page.check('input[data-filter="parameter"]')
    page.wait_for_selector(f'tr[data-service="{SETTINGS}/set_parameters"]')
    page.uncheck('input[data-filter="parameter"]')
    page.fill('#service-filter', 'nested')
    page.wait_for_function("document.querySelectorAll('#service-list tr[data-service]').length === 1")
    page.click('tr[data-service="/fixture/nested"]')
    page.wait_for_selector('#service-form [data-add="parameters"]')
    page.click('#service-form [data-add="parameters"]')
    page.fill('#service-form input[data-path="parameters[0].name"]', 'p1')
    page.fill('#service-form input[data-path="parameters[0].value.type"]', '7')
    page.click('#service-form [data-add="parameters[0].value.integer_array_value"]')
    page.fill('#service-form input[data-path="parameters[0].value.integer_array_value[0]"]', '5')
    assert 'results' in page.inner_text('#service-response-outline')


def test_nested_call_is_confirmed_shown_and_made_once(page, server):
    """The dialog shows the complete request; confirming (even with a double click) calls exactly once."""
    before = call_count(server, 'nested')
    page.click('#service-call-button')
    page.wait_for_selector('#service-dialog[open]')
    request = json.loads(page.inner_text('#service-confirm-request'))
    assert request['parameters'][0]['name'] == 'p1' and request['parameters'][0]['value']['integer_array_value'] == [5]
    page.screenshot(path=str(ARTIFACTS / 'services-confirm.png'))
    page.dblclick('#service-confirm')
    page.wait_for_selector('#service-call[data-state="succeeded"]')
    assert 'echo:p1:7:1' in page.inner_text('#service-response')
    time.sleep(1.0)
    assert call_count(server, 'nested') == before + 1
    page.reload()
    page.wait_for_selector('#service-history tr[data-call]')
    time.sleep(1.0)
    assert call_count(server, 'nested') == before + 1
    page.screenshot(path=str(ARTIFACTS / 'services.png'))


def test_invalid_request_and_declined_dialog_call_nothing(page, server):
    """Invalid JSON values are flagged before any dialog; declining the dialog calls nothing."""
    page.goto(page.url.split('#')[0] + '#ros/services/fixture/add_two_ints')
    page.wait_for_selector('#service-form input[data-path="a"]')
    before = call_count(server, 'add_two_ints')
    page.click('#service-mode-json')
    page.fill('#service-json', '{"a": "two", "b": 1}')
    page.click('#service-call-button')
    assert 'whole number' in page.inner_text('#service-errors') and not page.is_visible('#service-dialog')
    page.fill('#service-json', '{"a": 2, "b": 3}')
    page.click('#service-call-button')
    page.wait_for_selector('#service-dialog[open]')
    page.click('#service-decline')
    time.sleep(1.0)
    assert call_count(server, 'add_two_ints') == before


def test_timeout_is_reported_as_unknown(page):
    """A call that misses its timeout is reported as timed out, with the outcome unknown."""
    page.goto(page.url.split('#')[0] + '#ros/services/fixture/slow')
    page.wait_for_selector('#service-title:has-text("/fixture/slow")')
    page.wait_for_function("!document.getElementById('service-call-button').disabled")
    page.fill('#service-timeout', '1')
    page.click('#service-call-button')
    page.click('#service-confirm')
    page.wait_for_selector('#service-call[data-state="timed_out"]', timeout=15000)
    assert 'not repeated' in page.inner_text('#service-call-error')


def test_read_only_gui_disables_changes_in_the_page(ros_environment, browser):
    """In read-only mode the panels explain that changes are disabled and offer no apply or call."""
    process, base = start_server(dict(ros_environment, SMPA_GUI_READ_ONLY='1'))
    management = start_fixture(ros_environment)
    try:
        page = browser.new_page()
        page.goto(base + '/#ros/parameters' + SETTINGS)
        page.wait_for_selector('#parameter-table', timeout=20000)
        assert page.is_visible('#parameter-readonly') and page.locator('button[data-edit]').count() == 0
        page.goto(base + '/#ros/services/fixture/add_two_ints')
        page.wait_for_selector('#service-readonly', state='visible')
        assert page.is_disabled('#service-call-button')
        page.close()
    finally:
        stop(management)
        stop(process)
