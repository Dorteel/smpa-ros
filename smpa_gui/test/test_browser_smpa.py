"""Drive the SMPA tab's Sense workspace in a headless browser against fixture sessions and live ROS fixtures."""
from pathlib import Path
import subprocess
import time

import pytest

from conftest import GUI, PYTHON, fetch, start_server, stop

pytestmark = pytest.mark.browser
ARTIFACTS = Path(__file__).resolve().parent / 'artifacts'
ROBOT = '11111111-1111-4111-8111-111111111111'
CONVERSATION = '22222222-2222-4222-8222-222222222222'
ACTIVE = '55555555-5555-4555-8555-555555555555'
STALE = '66666666-6666-4666-8666-666666666666'
EMPTY = '77777777-7777-4777-8777-777777777777'
BROKEN = '88888888-8888-4888-8888-888888888888'


def subscriptions(server):
    """Return the topics the gateway is subscribed to right now."""
    return {item['topic'] for item in fetch(server + '/api/v1/health')['active_subscriptions']}


def wait_for(condition, timeout=20.0, message='condition was not reached'):
    """Poll a condition until it holds or fail after the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.2)
    raise AssertionError(message)


@pytest.fixture(scope='module')
def active_run(ros_environment, server):
    """Run the stand-in for an active SMPA robot session."""
    process = subprocess.Popen([PYTHON, str(GUI / 'test/fixtures/active_run_node.py'), ACTIVE], env=ros_environment,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert 'ACTIVE RUN READY' in process.stdout.readline()
    yield process
    stop(process)


@pytest.fixture(scope='module')
def page(server, browser, active_run):
    """Open the SMPA tab in a fresh browser context and fail on any page error."""
    errors = []
    context = browser.new_context(viewport={'width': 1600, 'height': 1100})
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(server + '/#smpa')
    page.wait_for_function("document.querySelectorAll('#session-select option').length > 1")
    yield page
    context.close()
    assert errors == [], f'The page reported errors: {errors}'


def select(page, identity):
    """Choose a session in the selector and wait until its summary shows."""
    page.select_option('#session-select', identity)
    page.wait_for_function(f"document.getElementById('session-id') && document.getElementById('session-id').textContent === '{identity}'")


def test_sections_and_session_selector(page):
    """Five sections are offered; Model, Plan and Act are placeholders; the selector lists every session with its state."""
    assert page.locator('nav[aria-label="SMPA sections"] button').all_inner_texts() == ['Dashboard', 'Sense', 'Model', 'Plan', 'Act']
    for section in ('model', 'plan', 'act'):
        page.click(f'button[data-section="{section}"]')
        assert 'not implemented in this iteration' in page.inner_text(f'[data-placeholder="{section}"]')
    page.click('button[data-section="sense"]')
    page.wait_for_selector('#see')
    options = page.locator('#session-select option').all_inner_texts()
    assert len(options) == 10 and any('fixture_all_senses' in text and 'Ended' in text for text in options)
    assert any('fixture_active' in text for text in options)
    assert 'fixture_test' not in page.inner_text('#memory-root') and 'read-only' in page.inner_text('#memory-root')


def test_ended_robot_session_shows_recorded_senses_and_labels_live_data(page, server):
    """See, Hear and Read show what SMPA recorded; the live camera is labelled as not belonging to the ended session."""
    select(page, ROBOT)
    page.wait_for_selector('#see-recorded img')
    assert page.get_attribute('#session-activity', 'data-activity') == 'ended'
    see = page.inner_text('#see-recorded')
    assert 'A red test card fills the view.' in see and 'evidence_without_description' in see and 'RECORDED' in see
    assert page.locator('#see-recorded img').count() == 2
    assert page.locator('#see-recorded img').first.evaluate('(img) => img.complete && img.naturalWidth') == 64
    hear = page.inner_text('#hear-recorded')
    assert 'Please look at the red card.' in hear and 'faster-whisper-base' in hear
    assert page.locator('#hear-recorded audio').get_attribute('src').endswith('recording.wav')
    read = page.inner_text('#read-recorded')
    assert 'The card belongs to Ana.' in read and 'unverified user statement' in read and 'message 3' in read
    assert page.locator('#read textarea, #read input[type="text"], #read button').count() == 0, 'Read offers no input'
    page.wait_for_selector('#live-camera img[data-frames]')
    assert 'not part of it' in page.inner_text('#see .live-area')
    assert 'not active' in page.inner_text('#live-listen')
    assert '/fixture/camera/image_raw' in subscriptions(server)
    time.sleep(0.5)
    page.screenshot(path=str(ARTIFACTS / 'smpa-sense.png'), full_page=True)


def test_timeline_order_filters_and_provenance(page):
    """The timeline merges every modality chronologically, filters by modality and shows provenance."""
    select(page, ROBOT)
    page.wait_for_selector('#timeline tr[data-id]')
    rows = page.locator('#timeline tr[data-id]')
    assert rows.count() == 7 and page.inner_text('#timeline-count') == '7 of 7 entries'
    modalities = [rows.nth(index).get_attribute('data-modality') for index in range(7)]
    assert modalities == ['vision', 'sense', 'hear', 'vision', 'sense', 'read', 'read'], 'newest first by default'
    page.uncheck('input[data-filter="newestFirst"]')
    times = page.locator('#timeline tr[data-id] td:first-child').all_inner_texts()
    assert times == sorted(times) and page.locator('#timeline tr[data-id]').first.get_attribute('data-modality') == 'read'
    page.uncheck('input[data-filter="sense"]')
    page.uncheck('input[data-filter="read"]')
    assert {row.get_attribute('data-modality') for row in page.locator('#timeline tr[data-id]').all()} == {'vision', 'hear'}
    page.locator('#timeline tr[data-modality="hear"]').click()
    details = page.inner_text('.provenance-row')
    assert '"sense_record": "state/cycle_000001/sense.json"' in details and '"timestamp_source": "transcript Timestamp"' in details
    assert '"Recognition model": "faster-whisper-base"' in details
    for key in ('sense', 'read', 'newestFirst'):
        page.check(f'input[data-filter="{key}"]')
    assert 'faster-whisper-base' in page.inner_text('.provenance-row'), 'expanded rows stay expanded'


def test_read_binding_and_conversation_session(page):
    """The robot run links to the conversation it read; the conversation session shows its own messages."""
    select(page, ROBOT)
    page.click('#read-binding')
    page.wait_for_function(f"document.getElementById('session-id').textContent === '{CONVERSATION}'")
    assert page.get_attribute('#session-activity', 'data-activity') == 'running_unverified'
    page.wait_for_selector('#read-recorded [data-kind="user_message"]')
    assert page.locator('#read-recorded [data-kind="user_message"]').count() == 2
    assert 'was not enabled' in page.inner_text('#see-recorded') and 'was not enabled' in page.inner_text('#hear-recorded')
    assert 'not part of this kind of session' in page.inner_text('#live-listen')
    assert 'no camera topic' in page.inner_text('#live-camera-state')


def test_active_session_shows_live_listening_state(page, server):
    """An active session is marked active and its Listen status is shown live, alternating as it changes."""
    select(page, ACTIVE)
    page.wait_for_function("document.getElementById('session-activity').dataset.activity === 'active'", timeout=20000)
    states = set()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and states != {'listening', 'idle'}:
        states.add(page.get_attribute('#live-listen', 'data-state'))
        time.sleep(0.3)
    assert {'listening', 'idle'} <= states
    assert 'Nothing recorded yet' in page.inner_text('#see-recorded')
    assert 'No observations recorded in this session.' in page.inner_text('#timeline-empty')
    page.screenshot(path=str(ARTIFACTS / 'smpa-active.png'))


def test_stale_empty_and_damaged_sessions(page):
    """A stale running session, an empty session and unreadable metadata are each explained."""
    select(page, STALE)
    assert page.get_attribute('#session-activity', 'data-activity') == 'running_not_visible'
    assert 'stopped without finalizing' in page.inner_text('#session-explanation')
    select(page, EMPTY)
    page.wait_for_selector('#timeline-empty')
    assert 'Nothing was recorded for this modality.' in page.inner_text('#see-recorded')
    select(page, BROKEN)
    assert 'unreadable' in page.inner_text('#session-error')


def test_switching_and_leaving_close_live_streams(page, server):
    """Selecting a session without a camera, and leaving the tab, end the live subscriptions."""
    select(page, ROBOT)
    wait_for(lambda: '/fixture/camera/image_raw' in subscriptions(server))
    select(page, CONVERSATION)
    wait_for(lambda: '/fixture/camera/image_raw' not in subscriptions(server), message='the camera stream stayed open')
    select(page, ACTIVE)
    wait_for(lambda: any(topic.endswith('/listen/_action/status') for topic in subscriptions(server)))
    page.click('#primary-tabs button[data-tab="ros"]')
    wait_for(lambda: subscriptions(server) == set(), message='leaving the SMPA tab left subscriptions open')


def test_reload_restores_section_and_session(page):
    """The address remembers the section and session, so a reload shows the same view."""
    page.goto(page.url.split('#')[0] + f'#smpa/sense/{ROBOT}')
    page.wait_for_selector('#see-recorded img')
    page.reload()
    page.wait_for_function(f"document.getElementById('session-id') && document.getElementById('session-id').textContent === '{ROBOT}'")
    page.click('button[data-section="plan"]')
    assert page.url.endswith(f'#smpa/plan/{ROBOT}')


def test_disconnection_is_reported_and_recovers(page):
    """When the GUI server cannot be reached the last data stays and a banner says so; it clears on recovery."""
    page.goto(page.url.split('#')[0] + f'#smpa/sense/{ROBOT}')
    page.wait_for_selector('#see-recorded img')
    page.route('**/api/v1/**', lambda route: route.abort())
    page.wait_for_selector('#smpa-offline', state='visible', timeout=10000)
    assert page.locator('#see-recorded img').count() == 2, 'recorded data stays visible'
    page.unroute('**/api/v1/**')
    page.wait_for_selector('#smpa-offline', state='hidden', timeout=10000)


def test_unconfigured_and_demonstration_banners(ros_environment, browser, tmp_path):
    """Without a memory directory the tab explains how to configure it; demo data is labelled; disabled SMPA is said so."""
    from conftest import build_memory
    cases = ((dict(SMPA_GUI_MEMORY_ROOT=str(tmp_path / 'absent')), '#smpa-unconfigured'),
             (dict(SMPA_GUI_MEMORY_ROOT=str(build_memory(tmp_path / 'demo', demo=True))), '#smpa-demo'),
             (dict(SMPA_GUI_NO_SMPA='1'), '#smpa-disabled'))
    for settings, banner in cases:
        process, base = start_server(dict(ros_environment, **settings))
        try:
            page = browser.new_page()
            page.goto(base + '/#smpa/sense')
            page.wait_for_selector(banner, state='visible')
            if banner == '#smpa-demo':
                assert 'DEMONSTRATION DATA' in page.inner_text(banner)
                page.wait_for_selector('#see-recorded')
                page.screenshot(path=str(ARTIFACTS / 'smpa-demo.png'))
            elif banner == '#smpa-disabled':
                assert 'disabled' in page.inner_text(banner) and not page.is_visible('#smpa-unconfigured')
                assert not page.is_visible('#smpa-offline')
            else:
                assert '--memory-root' in page.inner_text(banner) and 'no sessions' in page.inner_text('#session-select')
            page.close()
        finally:
            stop(process)
