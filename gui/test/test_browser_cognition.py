"""Drive the Dialogue's cognitive views and robot panel in a headless browser against SMPA stand-ins.

The stand-ins (``fixtures/cognitive_nodes.py``) serve SMPA's run control,
inference server and the four stage actions with SMPA's generated types and
persist records the way SMPA's stages do. The tests cover the live strip, the
context choice, a full cognitive step with the local Act approval, the
recorded Sense, Model, Plan and Act views with their links, the world-model
history, a failed stage with its recorded reason, the run and model dialogs,
and view switching. Skipped without Playwright or ``smpa_ros_interfaces``.
"""
import json
from pathlib import Path

import pytest

from conftest import start_server, stop
from test_dialogue_ros import (ROBOT_RUN, get, interfaces_installed, robot_run, start_stand_ins, wait_until,
                               write_memory)

pytestmark = [pytest.mark.browser,
              pytest.mark.skipif(not interfaces_installed(), reason='smpa_ros_interfaces with the stage actions is not installed')]
ARTIFACTS = Path(__file__).resolve().parent / 'artifacts'


@pytest.fixture(scope='module')
def cognition(ros_environment, tmp_path_factory):
    """Run the stand-ins and a GUI on the same memory directory; yield the base URL and memory."""
    folder = tmp_path_factory.mktemp('cognition')
    memory = write_memory(folder / 'memory')
    environment = dict(ros_environment, SMPA_GUI_MEMORY_ROOT=str(memory))
    stand_ins = start_stand_ins(environment, memory)
    process, base = start_server(environment)
    wait_until(lambda: all(stage['available'] for stage in get(base, '/smpa/ros')['stages'].values()),
               message='the stand-ins never became visible')
    yield dict(base=base, memory=memory)
    stop(process)
    stop(stand_ins)


@pytest.fixture(scope='module')
def page(cognition, browser):
    """Open the Dialogue on the robot run and fail on any page error."""
    errors = []
    context = browser.new_context(viewport={'width': 1500, 'height': 950})
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(cognition['base'] + f'/#smpa/dialogue/{ROBOT_RUN}')
    page.wait_for_selector('#robot-run-state')
    yield page
    context.close()
    assert errors == [], f'The page reported errors: {errors}'


def view(page, stage, mode):
    """Switch the right side to a view and one of its modes."""
    page.click(f'[data-view="{stage}"]')
    page.click(f'#view-modes [data-mode="{mode}"]')


def test_live_strip_and_panel_show_smpas_ros_state_apart_from_history(page):
    """The LIVE strip lists the four stage servers; the panel shows run control and the inference server."""
    page.wait_for_selector('#cognition-live [data-stage="act"][data-state="online"]')
    assert 'LIVE · ROS 2' in page.inner_text('#cognition-live')
    for stage in ('sense', 'model', 'plan'):
        assert page.get_attribute(f'#cognition-live [data-stage="{stage}"]', 'data-state') in ('online', 'active')
    assert page.get_attribute('#robot-run-state', 'data-state') == 'running'
    assert page.get_attribute('#robot-run-id', 'data-run-id') == ROBOT_RUN
    page.wait_for_selector('#inference-server[data-state="ready"]')
    assert page.inner_text('#inference-model') == 'stub-a'


def test_independent_context_explains_why_no_step_runs_and_shared_enables_it(page):
    """In the independent context the step button is disabled with a reason; shared enables it."""
    page.click('#dialogue-context [data-context="independent"]')
    page.wait_for_selector('#step-run[disabled]')
    assert page.inner_text('#step-reason')
    page.click('#dialogue-context [data-context="shared"]')
    page.wait_for_selector('#step-run:not([disabled])')
    page.wait_for_selector('#dialogue-path:has-text("Shared context")')


def test_step_asks_before_act_and_records_appear_in_each_view(page, cognition):
    """Sense, Model and Plan run; Act waits for local approval showing the decision; approving records Act."""
    page.click('#step-run')
    page.wait_for_selector('#robot-dialog[data-kind="act"][open]', timeout=30000)
    decision = json.loads(page.inner_text('#act-decision'))
    assert decision['action'] == 'observe' and decision['target'] == 'green card'
    assert page.get_attribute('#step-progress', 'data-state') == 'awaiting_act'
    assert [page.get_attribute(f'#step-progress [data-stage="{stage}"]', 'data-state') for stage in ('sense', 'model', 'plan')] \
        == ['succeeded'] * 3
    assert 'null' not in page.inner_text('#robot-dialog') and 'null' not in page.inner_text('#robot-panel')
    page.screenshot(path=str(ARTIFACTS / 'dialogue-act-approval.png'))
    page.click('#act-confirm')
    page.wait_for_selector('#step-progress[data-state="completed"]', timeout=30000)
    assert not list(robot_run(cognition).glob('state/cycle_000001/act*.json')) == []

    view(page, 'act', 'records')
    page.wait_for_selector('#cognitive-history .record-card[data-stage="act"]')
    assert page.inner_text('#history-label').startswith('RECORDED')
    card = page.locator('#cognitive-history .record-card[data-stage="act"]').first
    assert 'observe' in card.inner_text()
    view(page, 'plan', 'records')
    page.wait_for_selector('#cognitive-history .record-card[data-stage="plan"] .record-action')
    assert page.inner_text('#cognitive-history .record-card[data-stage="plan"] .record-action') == 'observe'
    view(page, 'sense', 'records')
    page.wait_for_selector('#cognitive-history .record-card[data-stage="sense"] img.record-thumb')
    page.screenshot(path=str(ARTIFACTS / 'dialogue-sense-records.png'))


def test_record_links_jump_to_the_linked_record(page):
    """A link to a source record switches to that stage's records and highlights the card."""
    view(page, 'plan', 'records')
    link = page.locator('#cognitive-history .record-card[data-stage="plan"] [data-relation="source"] a.record-jump').first
    target = link.get_attribute('data-target')
    link.click()
    page.wait_for_selector(f'#cognitive-history .record-card.highlight[data-path="{target}"]')
    assert page.get_attribute('[data-view="model"]', 'aria-selected') == 'true'


def test_world_model_history_names_the_record_that_produced_each_snapshot(page):
    """The Model view's history lists the run's snapshots with the Model record that produced them."""
    view(page, 'model', 'history')
    page.wait_for_selector('#cognitive-history .snapshot-card')
    first = page.locator('#cognitive-history .snapshot-card').first
    assert 'world_model/iteration_' in first.inner_text() and 'produced by' in first.inner_text()
    page.screenshot(path=str(ARTIFACTS / 'dialogue-world-model-history.png'))


def test_declining_act_executes_nothing(page, cognition):
    """Declining the Act dialog ends the step as declined without a new Act record."""
    before = len(list(robot_run(cognition).glob('state/cycle_000001/act*.json')))
    page.click('#step-run')
    page.wait_for_selector('#robot-dialog[data-kind="act"][open]', timeout=30000)
    page.click('#act-decline')
    page.wait_for_selector('#step-progress[data-state="declined"]')
    assert len(list(robot_run(cognition).glob('state/cycle_000001/act*.json'))) == before


def test_failed_stage_shows_the_recorded_reason_and_retry_completes(page, cognition):
    """A failed Plan shows SMPA's recorded error; Retry resumes and reaches the Act decision."""
    (cognition['memory'] / 'scenario.json').write_text(json.dumps(dict(fail='plan')))
    page.click('#step-run')
    page.wait_for_selector('#step-progress[data-state="failed"]', timeout=30000)
    assert page.get_attribute('#step-progress [data-stage="plan"]', 'data-state') in ('aborted', 'failed')
    assert 'recorded reason' in page.inner_text('#step-error')
    view(page, 'plan', 'records')
    page.wait_for_selector('#cognitive-history .record-card[data-stage="plan"][data-status="failed"] .record-error')
    page.click('#step-retry')
    page.wait_for_selector('#robot-dialog[data-kind="act"][open]', timeout=30000)
    page.click('#act-decline')
    page.wait_for_selector('#step-progress[data-state="declined"]')


def test_view_switching_keeps_live_and_recorded_areas_apart(page):
    """Each view offers its own modes; the graph mode hides the history and shows the Turtle controls."""
    for stage, modes in (('sense', ['records', 'graph']), ('model', ['graph', 'history', 'records']),
                         ('plan', ['records', 'graph']), ('act', ['records'])):
        page.click(f'[data-view="{stage}"]')
        assert page.locator('#view-modes [data-mode]').evaluate_all('(items) => items.map((item) => item.dataset.mode)') == modes
    view(page, 'model', 'graph')
    page.wait_for_selector('#graph-state')
    assert not page.is_visible('#cognitive-history') and page.is_visible('#cognition-live')
    view(page, 'model', 'records')
    assert page.is_visible('#cognitive-history') and not page.is_visible('#graph-state')


def test_model_load_and_run_dialogs_go_through_confirmation(page, cognition):
    """Loading a model, stopping and starting the run all need a confirmation dialog first."""
    page.click('#model-load')
    page.wait_for_selector('#robot-dialog[data-kind="model-load"][open]')
    page.select_option('#model-load-select', 'stub-b')
    page.click('#model-load-confirm')
    page.wait_for_selector('#inference-model:has-text("stub-b")', timeout=20000)
    page.click('#robot-stop')
    page.wait_for_selector('#robot-dialog[data-kind="robot-stop"][open]')
    page.click('#robot-stop-decline')
    assert get(cognition['base'], '/smpa/ros')['robot']['run_state'] == 'running'
    page.click('#robot-stop')
    page.click('#robot-stop-confirm')
    page.wait_for_selector('#robot-run-state[data-state="completed"]', timeout=20000)
    page.click('#robot-start')
    page.wait_for_selector('#robot-dialog[data-kind="robot-start"][open]')
    page.fill('#robot-goal', 'Find the green card')
    page.click('#robot-start-confirm')
    page.wait_for_selector('#robot-run-state[data-state="running"]', timeout=20000)
