"""Drive the Actions tab in a headless browser against real fixture action servers.

The tests work through one page in order, like a user. The count of goals
known to the gateway is the measure of whether anything was sent: every step
that must not send a goal asserts that the count did not change.
"""
from pathlib import Path
import time

import pytest

from conftest import fetch, stop

pytestmark = pytest.mark.browser
ARTIFACTS = Path(__file__).resolve().parent / 'artifacts'
API = '/api/v1'


def goals(base):
    """Return the goal records the gateway currently holds, newest first."""
    return fetch(base + API + '/actions/goals')['goals']


def wait_for(condition, timeout=20.0, message='condition was not reached'):
    """Poll a condition until it holds or fail after the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.2)
    raise AssertionError(message)


@pytest.fixture(scope='module')
def page(server, browser):
    """Open the Actions tab in a fresh browser context and fail on any page error."""
    errors = []
    context = browser.new_context(viewport={'width': 1600, 'height': 1000})
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(server + '/#actions')
    page.wait_for_selector('tr[data-action="/fixture/fibonacci"]')
    yield page
    context.close()
    assert errors == [], f'The page reported errors: {errors}'


def choose(page, action):
    """Select an action server in the list and wait for its editor."""
    page.click(f'tr[data-action="{action}"]')
    page.wait_for_function(f"document.getElementById('editor-action').textContent === '{action}'")
    page.wait_for_selector('#goal-editor [data-path], #goal-editor .muted')


def send(page, confirm=True):
    """Click Send Goal, then confirm or decline in the dialog."""
    page.click('#send-goal')
    page.wait_for_selector('#confirm-dialog[open]')
    page.click('#confirm-send' if confirm else '#decline-send')
    page.wait_for_selector('#confirm-dialog:not([open])', state='attached')


def set_order(page, value):
    """Type a value into the Fibonacci goal's only field."""
    page.fill('#goal-editor [data-path="order"]', str(value))


def test_selecting_and_editing_never_sends(page, server):
    """Opening the tab, selecting servers and editing values sends nothing."""
    count = len(goals(server))
    assert 'available' in page.inner_text('tr[data-action="/fixture/fibonacci"]')
    assert 'example_interfaces/action/Fibonacci' in page.inner_text('tr[data-action="/fixture/fibonacci"]')
    assert page.is_disabled('#send-goal') and page.is_disabled('#cancel-goal')
    choose(page, '/fixture/fibonacci')
    field = page.locator('#goal-editor [data-path="order"]')
    assert field.get_attribute('type') == 'number' and field.input_value() == '0'
    assert 'int32' in page.inner_text('#goal-editor')
    assert 'robot will move' in page.inner_text('#physical-warning')
    set_order(page, 3)
    choose(page, '/fixture/lookup_transform')
    choose(page, '/fixture/fibonacci')
    assert field.input_value() == '3', 'the draft of an action is kept while switching'
    time.sleep(1)
    assert len(goals(server)) == count


def test_invalid_values_are_caught_before_the_dialog(page, server):
    """A value that does not fit the field type is reported and no dialog opens."""
    count = len(goals(server))
    set_order(page, 2.5)
    page.click('#send-goal')
    assert 'whole number' in page.inner_text('#goal-errors')
    assert page.locator('#confirm-dialog[open]').count() == 0
    assert 'invalid' in page.locator('#goal-editor [data-path="order"]').get_attribute('class')
    set_order(page, 99999999999)
    page.click('#send-goal')
    assert 'between' in page.inner_text('#goal-errors') and page.locator('#confirm-dialog[open]').count() == 0
    assert len(goals(server)) == count


def test_declining_the_dialog_sends_nothing_and_confirming_sends_once(page, server):
    """The dialog names the action, server and values with a warning; only confirming sends, exactly once."""
    count = len(goals(server))
    set_order(page, 5)
    page.click('#send-goal')
    page.wait_for_selector('#confirm-dialog[open]')
    dialog = page.inner_text('#confirm-dialog')
    assert '/fixture/fibonacci' in dialog and '/fixture_action_server' in dialog and '"order": 5' in dialog
    assert 'robot' in dialog and 'may start moving' in dialog
    page.screenshot(path=str(ARTIFACTS / 'actions-confirm.png'))
    page.click('#decline-send')
    time.sleep(0.7)
    assert len(goals(server)) == count
    page.click('#send-goal')
    page.wait_for_selector('#confirm-dialog[open]')
    page.keyboard.press('Escape')
    time.sleep(0.7)
    assert len(goals(server)) == count, 'closing the dialog with Escape must not send'

    send(page)
    page.wait_for_selector('#goal-state[data-state="succeeded"]', timeout=20000)
    assert len(goals(server)) == count + 1
    record = goals(server)[0]
    assert page.inner_text('#goal-uuid') == record['goal_id']
    assert '"sequence"' in page.inner_text('#goal-result') and '5' in page.inner_text('#goal-result')
    feedback = page.inner_text('#goal-feedback')
    assert '#4' in feedback and '#1' in feedback and 'Feedback (4 received' in page.inner_text('#goal-monitor')
    assert page.locator(f'tr[data-goal="{record["goal_id"]}"][data-state="succeeded"]').count() == 1
    monitor = page.inner_text('#goal-monitor')
    assert 'Sent' in monitor and 'Accepted' in monitor and 'Finished' in monitor and 'not requested' in monitor
    page.screenshot(path=str(ARTIFACTS / 'actions.png'))


def test_live_feedback_and_cancellation(page, server):
    """Feedback appears while the goal runs, and Cancel Goal stops it."""
    set_order(page, 60)
    send(page)
    page.wait_for_selector('#goal-state[data-state="executing"]', timeout=15000)
    assert 'Waiting for the result' in page.inner_text('#goal-result')
    first = page.inner_text('#goal-feedback')
    page.wait_for_function("document.getElementById('goal-feedback').children.length >= 4")
    assert page.inner_text('#goal-feedback') != first
    assert page.is_enabled('#cancel-goal')
    page.click('#cancel-goal')
    page.wait_for_selector('#goal-state[data-state="canceled"]', timeout=15000)
    assert 'server answer: accepted' in page.inner_text('#goal-cancel')
    assert page.is_disabled('#cancel-goal')
    assert goals(server)[0]['state'] == 'canceled'


def test_rejected_goal_is_shown(page, server):
    """A goal the server rejects is shown as rejected with an explanation."""
    set_order(page, -1)
    send(page)
    page.wait_for_selector('#goal-state[data-state="rejected"]', timeout=15000)
    assert 'rejected the goal' in page.inner_text('#goal-error')
    assert page.is_disabled('#cancel-goal')


def test_concurrent_goals_are_listed_and_selectable(page, server):
    """Two goals run at the same time; each can be selected and shows its own feedback."""
    set_order(page, 24)
    send(page)
    set_order(page, 26)
    send(page)
    wait_for(lambda: [goal['state'] for goal in goals(server)[:2]] == ['executing', 'executing'],
             message='the two goals did not execute concurrently')
    newest, older = goals(server)[0], goals(server)[1]
    page.wait_for_function("document.querySelectorAll('#goal-history tr[data-state=\"executing\"]').length === 2")
    page.click(f'tr[data-goal="{older["goal_id"]}"]')
    assert page.inner_text('#goal-uuid') == older['goal_id'] and '"order": 24' in page.inner_text('#goal-values')
    page.click(f'tr[data-goal="{newest["goal_id"]}"]')
    assert '"order": 26' in page.inner_text('#goal-values')
    page.click('#cancel-goal')
    page.wait_for_selector('#goal-state[data-state="canceled"]', timeout=15000)
    assert goals(server)[1]['state'] in ('executing', 'succeeded'), 'cancelling one goal must not affect the other'
    wait_for(lambda: goals(server)[1]['state'] == 'succeeded')


def test_nested_form_json_mode_and_saved_configurations(page, server):
    """Nested goals get a generated form; JSON editing and saved configurations work and never send."""
    count = len(goals(server))
    choose(page, '/fixture/lookup_transform')
    assert page.locator('#goal-editor [data-path="target_frame"]').get_attribute('type') == 'text'
    assert page.locator('#goal-editor [data-path="advanced"]').get_attribute('type') == 'checkbox'
    assert page.locator('#goal-editor [data-path="source_time.sec"]').get_attribute('type') == 'number'
    page.fill('#goal-editor [data-path="target_frame"]', 'map')
    page.fill('#goal-editor [data-path="timeout.sec"]', '4')
    page.check('#goal-editor [data-path="advanced"]')
    page.click('#mode-json')
    text = page.input_value('#goal-json')
    assert '"target_frame": "map"' in text and '"advanced": true' in text and '"sec": 4' in text
    page.fill('#goal-json', '{"target_frame": "odom", "source_frame": "base", "timeout": {"sec": 2}}')
    page.click('#mode-form')
    assert page.input_value('#goal-editor [data-path="target_frame"]') == 'odom'
    assert page.input_value('#goal-editor [data-path="timeout.sec"]') == '2'
    assert page.input_value('#goal-editor [data-path="timeout.nanosec"]') == '0', 'omitted fields take their defaults'
    page.click('#mode-json')
    page.fill('#goal-json', '{"target_frame": ')
    page.click('#mode-form')
    assert 'not valid JSON' in page.inner_text('#goal-errors') and page.is_visible('#goal-json')
    page.click('#send-goal')
    assert 'not valid JSON' in page.inner_text('#goal-errors') and page.locator('#confirm-dialog[open]').count() == 0
    page.fill('#goal-json', '{"target_frame": "odom", "source_frame": "base", "wrong": 1}')
    page.click('#send-goal')
    assert 'not a field' in page.inner_text('#goal-errors')
    page.fill('#goal-json', '{"target_frame": "odom", "source_frame": "base", "timeout": {"sec": 2}}')
    page.click('#mode-form')

    page.fill('#config-name', 'odom lookup')
    page.click('#config-save')
    page.click('#reset-defaults')
    assert page.input_value('#goal-editor [data-path="target_frame"]') == ''
    page.select_option('#config-select', 'odom lookup')
    page.click('#config-load')
    assert page.input_value('#goal-editor [data-path="target_frame"]') == 'odom'
    assert 'Nothing was sent' in page.inner_text('#action-notice')
    time.sleep(0.7)
    assert len(goals(server)) == count

    send(page)
    page.wait_for_selector('#goal-state[data-state="succeeded"]', timeout=15000)
    result = page.inner_text('#goal-result')
    assert '"frame_id": "odom"' in result and '"child_frame_id": "base"' in result
    page.screenshot(path=str(ARTIFACTS / 'actions-nested.png'))


def test_generated_form_handles_arrays_and_nested_messages(page):
    """The form component adds and removes array elements and edits nested messages inside arrays."""
    result = page.evaluate("""async () => {
      const {createEditor, defaultFor, validate} = await import('/tabs/actions/form.js');
      const integer = {kind: 'integer', type: 'int32', minimum: -2147483648, maximum: 2147483647};
      const point = {kind: 'message', type: 'pkg/msg/Point', fields: [{name: 'x', schema: {kind: 'float', type: 'double', limit: null}},
                                                                     {name: 'name', schema: {kind: 'string', type: 'string', max_length: null}}]};
      const schema = {kind: 'message', type: 'pkg/msg/Goal', fields: [
        {name: 'numbers', schema: {kind: 'array', item: integer, fixed_size: null, max_size: 2}},
        {name: 'fixed', schema: {kind: 'array', item: integer, fixed_size: 3, max_size: null}},
        {name: 'points', schema: {kind: 'array', item: point, fixed_size: null, max_size: null}}]};
      const editor = createEditor(schema, defaultFor(schema), () => {});
      document.body.append(editor.element);
      const type = (path, text) => { const input = editor.element.querySelector(`[data-path="${path}"]`); input.value = text; input.dispatchEvent(new Event('input')); };
      editor.element.querySelector('[data-add="numbers"]').click();
      editor.element.querySelector('[data-add="numbers"]').click();
      const addAfterMaximum = editor.element.querySelector('[data-add="numbers"]') !== null;
      type('numbers[1]', '7');
      type('fixed[2]', 'abc');
      editor.element.querySelector('[data-add="points"]').click();
      editor.element.querySelector('[data-add="points"]').click();
      type('points[1].x', '2.5'); type('points[1].name', 'goal');
      editor.element.querySelector('[data-path="points"] .goal-array-item button').click();
      const value = editor.get();
      const fixedHasAdd = editor.element.querySelector('[data-add="fixed"]') !== null;
      const errors = validate(schema, value);
      editor.showErrors(errors);
      const marked = editor.element.querySelector('[data-path="fixed[2]"]').classList.contains('invalid');
      editor.element.remove();
      return {value, addAfterMaximum, fixedHasAdd, errors, marked};
    }""")
    assert result['value'] == {'numbers': [0, 7], 'fixed': [0, 0, ''], 'points': [{'x': 2.5, 'name': 'goal'}]}
    assert result['addAfterMaximum'] is False and result['fixedHasAdd'] is False
    assert [error['path'] for error in result['errors']] == ['fixed[2]'] and result['marked'] is True


def test_reloading_and_navigating_never_resends(page, server):
    """Reloads, back/forward navigation and restoring a configuration show the history but send nothing."""
    count = len(goals(server))
    newest = goals(server)[0]['goal_id']
    for _ in range(3):
        page.reload()
        page.wait_for_selector(f'tr[data-goal="{newest}"]')
    page.goto(page.url.split('#')[0] + '#ros/actions')
    page.wait_for_selector('tr[data-action="/fixture/fibonacci"]')
    page.go_back()
    page.wait_for_selector(f'tr[data-goal="{newest}"]')
    page.go_forward()
    page.go_back()
    page.wait_for_selector('#goal-history tr[data-goal]')
    assert page.locator('#goal-history tr[data-goal]').count() == count, 'the history comes back from the gateway'
    choose(page, '/fixture/lookup_transform')
    assert 'odom lookup' in page.inner_text('#config-select'), 'saved configurations survive a reload'
    page.select_option('#config-select', 'odom lookup')
    page.click('#config-load')
    page.click(f'tr[data-goal="{newest}"]')
    page.click('#reuse-goal')
    assert 'Nothing was sent' in page.inner_text('#action-notice')
    time.sleep(1.2)
    assert len(goals(server)) == count


def test_connection_failures_never_resend(page, server):
    """A failed submission is reported as unknown and not retried; reconnecting after an outage sends nothing."""
    choose(page, '/fixture/fibonacci')
    set_order(page, 2)
    count = len(goals(server))
    attempts = []
    page.route('**/api/v1/actions/goals', lambda route: (attempts.append(route.request.method), route.abort())
               if route.request.method == 'POST' else route.continue_())
    send(page)
    page.wait_for_function("document.getElementById('action-notice').textContent.includes('not known whether')")
    time.sleep(2.5)
    assert attempts == ['POST'], 'a failed submission must not be retried automatically'
    assert len(goals(server)) == count
    page.unroute('**/api/v1/actions/goals')

    page.route('**/api/v1/**', lambda route: route.abort())
    page.wait_for_selector('#actions-offline', state='visible', timeout=10000)
    assert 'nothing is resent' in page.inner_text('#actions-offline')
    page.unroute('**/api/v1/**')
    page.wait_for_selector('#actions-offline', state='hidden', timeout=10000)
    time.sleep(1.5)
    assert len(goals(server)) == count

    send(page)
    wait_for(lambda: len(goals(server)) == count + 1)
    page.click('#send-goal')
    page.wait_for_selector('#confirm-dialog[open]')
    page.dblclick('#confirm-send')
    time.sleep(1.5)
    assert len(goals(server)) == count + 2, 'a double click on the confirmation sends one goal'


def test_passive_inspector_remains_and_links_to_the_actions_tab(page):
    """The read-only ROS 2 action inspector still lists actions and points to the Actions tab."""
    page.goto(page.url.split('#')[0] + '#ros/actions')
    page.wait_for_selector('tr[data-action="/fixture/lookup_transform"]')
    assert page.locator('#send-goal').count() == 0
    page.click('a[href="#actions"]')
    page.wait_for_selector('#send-goal')


def test_read_only_mode_disables_sending_in_the_page(browser):
    """With --read-only the page explains that sending is disabled and keeps the Send button off."""
    from test_actions import run_lonely
    environment, fixtures, process, base = run_lonely(['--read-only'])
    try:
        page = browser.new_page()
        page.goto(base + '/#actions')
        page.click('tr[data-action="/fixture/fibonacci"]')
        page.wait_for_selector('#goal-editor [data-path="order"]')
        page.wait_for_selector('#read-only-notice', state='visible')
        assert page.is_disabled('#send-goal') and 'read-only' in page.inner_text('#send-reason')
        page.close()
    finally:
        stop(process)
        stop(fixtures)
