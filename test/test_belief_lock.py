"""Exercise the belief writer's per-run synchronization with mocked inference."""
import subprocess
import sys
import time

import yaml

from smpa_ros.conversation import append_message, open_conversation


def test_concurrent_belief_writers_do_not_duplicate_source_or_lose_graph(tmp_path):
    """Serialize two stale Model workers while allowing one source to be processed.

    Both workers attach to the same conversation with the same metadata snapshot.
    Their inference returns fixed RDF without loading a model. After both complete,
    require one durable SDS entry, one graph containing the proposal, and two
    completed records in the same active cycle. No external service runs.
    """
    memory = open_conversation(tmp_path, configuration={'conversation': {'agent': 'fixture'}})
    append_message(memory.directory, 'User', 'A cup is visible.')
    (memory.directory / 'state' / 'cycle_000001').mkdir()
    go = tmp_path / 'go'
    code = '''
from copy import deepcopy
from pathlib import Path
from time import monotonic, sleep
import json, sys, yaml
from memory import MemoryManager
from smpa_ros.belief import model_messages
from smpa_ros.conversation import read_messages
from rdflib.namespace import RDF

directory, cycle, go, ready = Path(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
metadata = yaml.safe_load((directory / 'metadata.yaml').read_text())
memory = MemoryManager.__new__(MemoryManager)
memory.directory, memory.metadata = directory, metadata
memory._metadata_snapshot = deepcopy(metadata)
memory.cycle, memory.in_cycle, memory.counts = cycle, True, {}
memory.sequence, memory.previous_record = 0, None
messages = read_messages(directory)
def generate(history, **settings):
    payload = json.loads(history[1]['content'])
    if 'message' in payload:
        sleep(0.15)
        return {'response': json.dumps({'sds': payload['message']})}
    return {'response': json.dumps({'triples': [{'s': '#cup', 'p': str(RDF.type),
        'o': {'uri': 'urn:smpa:belief#Entity'}}]})}
ready.touch()
deadline = monotonic() + 5
while not go.exists() and monotonic() < deadline: sleep(0.01)
assert go.exists()
model_messages(memory, {'generate': generate, 'settings': {},
                       'agent': {'backend': 'transformers'}}, messages)
'''
    children = [subprocess.Popen([sys.executable, '-c', code, str(memory.directory), '1',
                                  str(go), str(tmp_path / f'{cycle}.ready')],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for cycle in (1, 2)]
    try:
        deadline = time.monotonic() + 8
        while not all((tmp_path / f'{cycle}.ready').exists() for cycle in (1, 2)):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        go.touch()
        for child in children:
            _, error = child.communicate(timeout=8)
            assert child.returncode == 0, error
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
    import json
    from smpa_ros.belief import read_graph
    assert yaml.safe_load((memory.directory / 'metadata.yaml').read_text())['sds_messages'] == 1
    assert (memory.directory / 'conversation-sds.md').read_text().count('## User — message 1') == 1
    assert len(read_graph(memory.directory)) > 0
    for name in ('model.json', 'model_002.json'):
        record = json.loads((memory.directory / 'state' / 'cycle_000001' / name).read_text())
        assert record['status'] == 'succeeded'
