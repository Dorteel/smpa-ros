"""Drive the SMPA Dialogue in a headless browser: models, a real SMPA conversation, and live graph views."""
from pathlib import Path
import time

import pytest

from conftest import fetch

pytestmark = pytest.mark.browser
ARTIFACTS = Path(__file__).resolve().parent / 'artifacts'


def wait_for(condition, timeout=30.0, message='condition was not reached'):
    """Poll a condition until it holds or fail after the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.2)
    raise AssertionError(message)


@pytest.fixture(scope='module')
def page(conversation_server, browser):
    """Open the SMPA tab (Dialogue by default) and fail on any page error."""
    errors = []
    context = browser.new_context(viewport={'width': 1500, 'height': 950})
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(conversation_server['base'] + '/#smpa')
    page.wait_for_selector('#model-select option', state='attached')
    yield page
    context.close()
    assert errors == [], f'The page reported errors: {errors}'


def run_directory(world, run_id):
    """Find a run directory by run id."""
    return next(path for path in (world['memory'] / 'runs').iterdir() if path.name.endswith(run_id))


def show_graph(page, view):
    """Open the Turtle graph of a view: Sense, Plan, or the Model view's belief graph ("belief")."""
    page.click(f'[data-view="{"model" if view == "belief" else view}"]')
    page.click('#view-modes [data-mode="graph"]')


def node_labels(page):
    """Return the labels of the drawn graph nodes."""
    return sorted(page.evaluate("window.__rdf.nodes().map((node) => node.data('label'))"))


def test_dialogue_is_the_default_section_and_lists_models(page):
    """The SMPA tab opens on the Dialogue; models show provider, description and availability."""
    assert page.locator('nav[aria-label="SMPA sections"] button').all_inner_texts() == ['Dialogue', 'Sense', 'Model', 'Plan', 'Act']
    options = page.locator('#model-select option').all_inner_texts()
    assert any('stub · stub · default' in text for text in options) and any('absent-local-model' in text and 'unavailable' in text for text in options)
    assert page.input_value('#model-select') == 'api:stub'
    assert 'API provider stub, model stub-model' in page.inner_text('#model-description')
    assert page.get_attribute('#model-availability', 'data-state') == 'likely'
    page.select_option('#model-select', 'local:absent-local-model')
    assert page.get_attribute('#model-availability', 'data-state') == 'unavailable'
    assert 'Weights not found' in page.inner_text('#model-detail')
    page.select_option('#model-select', 'api:stub')
    assert page.get_attribute('#conversation-phase', 'data-phase') == 'stopped'
    assert page.is_disabled('#message-input')
    page.wait_for_selector('#dialogue-path[data-path="conversation_server"]')
    assert "Replies: SMPA's conversation server" in page.inner_text('#dialogue-path')


def test_old_dashboard_address_opens_the_dialogue(page):
    """An address or bookmark from before the rename (#smpa/dashboard) opens the Dialogue."""
    page.goto(page.url.split('#')[0] + '#smpa/dashboard')
    page.wait_for_selector('nav[aria-label="SMPA sections"] button[data-section="dialogue"][aria-selected="true"]')
    page.wait_for_selector('#model-select')
    page.goto(page.url.split('#')[0] + '#smpa/dialogue')
    page.wait_for_selector('#model-select')


def test_start_conversation_send_and_reload(page, conversation_server):
    """Starting selects the new SMPA run; Enter sends; the reply and the saved transcript survive a reload."""
    page.click('#conversation-start')
    page.wait_for_selector('#conversation-phase[data-phase="ready"]', timeout=60000)
    run_id = fetch(conversation_server['base'] + '/api/v1/smpa/conversation')['conversation']['run_id']
    conversation_server['browser_run'] = run_id
    page.wait_for_function(f"document.getElementById('session-id') && document.getElementById('session-id').textContent === '{run_id}'")
    assert page.url.endswith(f'#smpa/dialogue/{run_id}')
    assert 'Active conversation' in page.inner_text('#conversation-heading')
    page.fill('#message-input', 'Hello from the browser')
    page.press('#message-input', 'Enter')
    page.wait_for_selector('.message-assistant')
    assert page.locator('.message-user .message-text').all_inner_texts() == ['Hello from the browser']
    assert page.inner_text('.message-assistant .message-text').endswith('to: Hello from the browser')
    assert page.input_value('#message-input') == ''
    assert 'Hello from the browser' in (run_directory(conversation_server, run_id) / 'conversation.md').read_text()
    page.screenshot(path=str(ARTIFACTS / 'dialogue-conversation.png'))
    page.reload()
    page.wait_for_selector('.message-assistant')
    assert page.locator('.message').count() == 2 and 'Active conversation' in page.inner_text('#conversation-heading')


def test_failed_reply_shows_smpa_explanation_and_saved_message(page):
    """An inference failure shows SMPA's explanation; the saved user message stays in the conversation."""
    page.fill('#message-input', 'FAIL-INFERENCE from the browser')
    page.click('#message-send')
    page.wait_for_selector('#conversation-notice')
    assert 'Your message is saved' in page.inner_text('#conversation-notice')
    assert page.locator('.message-user .message-text').all_inner_texts()[-1] == 'FAIL-INFERENCE from the browser'
    assert page.locator('.message.pending').count() == 0


def test_previous_conversation_is_shown_read_only_and_can_be_continued(page, conversation_server):
    """After a new conversation, the previous one is shown from its transcript and can be reopened and continued."""
    first = conversation_server['browser_run']
    page.click('#conversation-start')
    page.wait_for_function(f"document.getElementById('session-id') && document.getElementById('session-id').textContent !== '{first}'"
                           " && document.getElementById('session-id').textContent !== ''", timeout=60000)
    page.wait_for_selector('#no-messages')
    page.select_option('#session-select', first)
    page.wait_for_selector('#conversation-resume')
    page.wait_for_function("document.querySelectorAll('#messages .message').length === 3")
    assert 'Recorded conversation' in page.inner_text('#conversation-heading') and page.is_disabled('#message-input')
    page.click('#conversation-resume')
    page.wait_for_function("document.getElementById('conversation-heading').textContent.startsWith('Active conversation')", timeout=60000)
    page.fill('#message-input', 'Continuing')
    page.press('#message-input', 'Enter')
    page.wait_for_function("document.querySelectorAll('#messages .message-assistant').length === 2")
    assert 'Continuing' in (run_directory(conversation_server, first) / 'conversation.md').read_text()


def test_graph_views_follow_turtle_file_changes(page, conversation_server):
    """Sense, Belief and Plan read their own files; external edits redraw the graph and keep the viewport.

    Before world_model/belief.ttl exists, Belief shows SMPA's current belief
    file (belief-state.ttl, with the stub's modelled triple) and says so.
    """
    run = run_directory(conversation_server, conversation_server['browser_run'])
    show_graph(page, 'sense')
    page.wait_for_selector('#graph-state[data-state="missing"]')
    assert 'world_model/sense.ttl' in page.inner_text('#graph-path') and page.is_visible('#graph-overlay')
    show_graph(page, 'belief')
    page.wait_for_selector('#graph-fallback')
    assert page.inner_text('#graph-path') == 'belief-state.ttl' and 'world_model/belief.ttl' in page.inner_text('#graph-fallback')
    wait_for(lambda: any('stub_speaker' in label for label in node_labels(page)), message='belief-state.ttl not drawn')
    folder = run / 'world_model'
    (folder / 'belief.ttl').write_text('@prefix ex: <http://example.org/b#> .\nex:cup ex:on ex:table ; ex:colour "red" .\n')
    (folder / 'sense.ttl').write_text('@prefix ex: <http://example.org/s#> .\nex:seen ex:shows ex:cup .\n')
    (folder / 'plan.ttl').write_text('@prefix ex: <http://example.org/p#> .\nex:step1 ex:next ex:step2 .\n')
    wait_for(lambda: node_labels(page) == ['ex:cup', 'ex:table'], message='world_model/belief.ttl not preferred')
    assert not page.is_visible('#graph-overlay') and page.locator('#graph-fallback').count() == 0
    for view, labels in (('sense', ['ex:cup', 'ex:seen']), ('plan', ['ex:step1', 'ex:step2']), ('belief', ['ex:cup', 'ex:table'])):
        show_graph(page, view)
        wait_for(lambda: node_labels(page) == labels, message=f'{view} graph not drawn')
        assert f'world_model/{view}.ttl' in page.inner_text('#graph-path')
    page.evaluate("() => { window.__rdf.zoom(1.6); window.__rdf.pan({x: 40, y: 25}); }")
    before = page.evaluate("({zoom: window.__rdf.zoom(), pan: window.__rdf.pan()})")
    revision = page.get_attribute('#graph-state', 'data-revision')
    (folder / 'belief.ttl').write_text('@prefix ex: <http://example.org/b#> .\nex:cup ex:on ex:table ; ex:colour "red" .\nex:table ex:in ex:room .\n')
    wait_for(lambda: node_labels(page) == ['ex:cup', 'ex:room', 'ex:table'], message='external change not shown')
    assert page.get_attribute('#graph-state', 'data-revision') != revision
    after = page.evaluate("({zoom: window.__rdf.zoom(), pan: window.__rdf.pan()})")
    assert after == before

    page.evaluate("() => { window.__rdf.getElementById('http://example.org/b#cup').emit('tap'); }")
    assert page.inner_text('#inspected-label') == 'ex:cup' and 'red' in page.inner_text('#inspected-literals')
    assert 'http://example.org/b#cup' in page.inner_text('#inspected-iri')
    page.evaluate("() => { window.__rdf.edges().filter((edge) => edge.data('label') === 'ex:in')[0].emit('tap'); }")
    assert page.inner_text('#inspected-label') == 'ex:in' and page.inner_text('#inspected-iri') == 'http://example.org/b#in'
    triple = page.inner_text('#inspected-triple')
    assert 'ex:table' in triple and 'ex:room' in triple

    (folder / 'belief.ttl').write_text('@prefix ex: <http://example.org/b#> .\nex:cup ex:on ')
    page.wait_for_selector('#graph-state[data-state="malformed"]')
    assert page.is_visible('#graph-stale') and node_labels(page) == ['ex:cup', 'ex:room', 'ex:table']
    (folder / 'belief.ttl').unlink()
    (folder / 'belief.ttl').mkdir()
    page.wait_for_selector('#graph-state[data-state="unavailable"]')
    assert page.is_visible('#graph-stale') and len(node_labels(page)) == 3
    (folder / 'belief.ttl').rmdir()
    page.wait_for_selector('#graph-fallback')
    wait_for(lambda: any('stub_speaker' in label for label in node_labels(page)), message='fallback not shown again')
    (folder / 'plan.ttl').unlink()
    show_graph(page, 'plan')
    page.wait_for_selector('#graph-state[data-state="missing"]')
    assert node_labels(page) == [] and 'No file yet' in page.inner_text('#graph-overlay')
    show_graph(page, 'belief')
    (folder / 'belief.ttl').write_text('# nothing yet\n')
    page.wait_for_selector('#graph-state[data-state="empty"]')
    assert 'no triples' in page.inner_text('#graph-overlay')
    page.screenshot(path=str(ARTIFACTS / 'dialogue-graph-empty.png'))


def test_mock_graphs_are_bannered_and_switch_back_to_real(page, conversation_server):
    """Mock graphs are shown only with the MOCK banner; switching back shows the real file again."""
    run = run_directory(conversation_server, conversation_server['browser_run'])
    before = sorted(path.name for path in (run / 'world_model').iterdir())
    page.click('[data-source="mock"]')
    page.wait_for_selector('#graph-mock', state='visible')
    wait_for(lambda: 'mock:place_room' in node_labels(page), message='mock belief not drawn')
    assert 'MOCK DATA' in page.inner_text('#graph-mock') and 'mock_graphs/belief.ttl' in page.inner_text('#graph-path')
    page.screenshot(path=str(ARTIFACTS / 'dialogue-mock.png'))
    page.click('[data-source="real"]')
    page.wait_for_selector('#graph-mock', state='hidden')
    page.wait_for_selector('#graph-state[data-state="empty"]')
    assert sorted(path.name for path in (run / 'world_model').iterdir()) == before
    assert (run / 'world_model' / 'belief.ttl').read_text() == '# nothing yet\n'


def test_stopping_the_server_from_the_dialogue(page):
    """The Stop button stops SMPA's server; the conversation becomes a recorded one."""
    page.click('#conversation-stop')
    page.wait_for_selector('#conversation-phase[data-phase="stopped"]')
    page.wait_for_selector('#conversation-resume')
    assert page.is_disabled('#message-input')
