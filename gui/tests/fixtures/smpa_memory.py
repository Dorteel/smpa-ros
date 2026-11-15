"""Write an SMPA memory directory with known sessions, in SMPA's existing on-disk formats.

Concepts:
    Each fixture session copies the layout of a real run inspected on
    2026-11-15 (named in its docstring), with invented but plausible values.
    The files follow SMPA's formats exactly; nothing here extends them. The
    GUI tests read these sessions to check the SMPA tab against known data.

    With ``--demo`` the directory additionally gets the GUI's own marker file
    ``.smpa-gui-demo``, which makes the GUI label everything as demonstration
    data. The marker is a GUI convention, never written by SMPA.

    Usage: ``python tests/fixtures/smpa_memory.py --output /tmp/smpa-demo --demo``
"""
import argparse
import io
import json
import math
from pathlib import Path
import struct

import yaml

ROBOT = '11111111-1111-4111-8111-111111111111'
CONVERSATION = '22222222-2222-4222-8222-222222222222'
LEGACY_LAYOUT = '33333333-3333-4333-8333-333333333333'
REFINEMENT = '44444444-4444-4444-8444-444444444444'
ACTIVE = '55555555-5555-4555-8555-555555555555'
STALE = '66666666-6666-4666-8666-666666666666'
EMPTY = '77777777-7777-4777-8777-777777777777'
BROKEN = '88888888-8888-4888-8888-888888888888'
CAMERA_TOPIC = '/fixture/camera/image_raw'


def write(path, content):
    """Write text or bytes to a file, creating its folders."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding='utf-8')


def run_directory(root, started, name, identity):
    """Return a run directory named the way MemoryManager names it."""
    stamp = started.replace(':', '').replace('-', '_', 2).replace('T', '_').replace('.', '_')
    return root / 'runs' / f'{stamp[:10].replace("_", "-")}_{stamp[11:]}_{name}_{identity}'


def metadata(directory, identity, name, started, status, cycles, configuration, completed=None):
    """Write metadata.yaml with the keys MemoryManager writes."""
    write(directory / 'metadata.yaml', yaml.safe_dump(dict(
        run_id=identity, run_name=name, started_at=started, completed_at=completed, status=status,
        completed_cycles=cycles, models={}, configuration=configuration), sort_keys=False))
    for folder in ('observations/vision', 'observations/audio', 'world_model', 'state'):
        (directory / folder).mkdir(parents=True, exist_ok=True)


def png(colour, camera=None):
    """Make a small PNG; with ``camera`` metadata it carries SMPA's ``smpa.camera`` text chunk."""
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    image = Image.new('RGB', (64, 48), colour)
    info = PngInfo()
    if camera is not None:
        info.add_itxt('smpa.camera', json.dumps(camera))
    buffer = io.BytesIO()
    image.save(buffer, format='PNG', pnginfo=info)
    return buffer.getvalue()


def wav(seconds=0.5):
    """Make a float32 mono 16 kHz WAV with the exact header AudioObservation writes."""
    samples = int(16000 * seconds)
    data = b''.join(struct.pack('<f', 0.2 * math.sin(2 * math.pi * 440 * index / 16000)) for index in range(samples))
    header = struct.pack('<4sI4s4sIHHIIHH4sII4sI', b'RIFF', 48 + len(data), b'WAVE', b'fmt ', 16, 3, 1, 16000, 64000,
                         4, 32, b'fact', 4, samples, b'data', len(data))
    return header + data


def transcript(timestamp, identity, text, status='complete'):
    """Write the transcript.md text AudioObservation writes."""
    return (f'# Audio Observation\n\n- Timestamp: {timestamp}\n- Run ID: {identity}\n- Source: ROS 2 Listen action\n'
            f'- Audio file: recording.wav\n- Audio format: 16000 Hz, mono, IEEE float32 PCM (lossless)\n'
            f'- Language: en\n- Recognition model: faster-whisper-base\n- Status: {status}\n\n## Transcript\n\n{text}\n')


def record(directory, cycle, name, content):
    """Write one stage record as JSON."""
    write(directory / 'state' / f'cycle_{cycle:06d}' / name, json.dumps(content, ensure_ascii=False, indent=2))


def robot_run(root, conversation_directory):
    """A finished all-senses robot run, like ``run_smpa.sh --all-senses`` produces.

    Mirrors ``2026-11-14_141524_..._json_fix_test`` plus the Hear and Read
    record shapes of ``sense/hear`` and ``sense/read``. Cycle 1 succeeds with
    vision, speech and two Read messages; cycle 2 fails after the camera
    frame was saved, so the PNG exists without a description.
    """
    started = '2026-11-15T10:00:00.000000+01:00'
    directory = run_directory(root, started, 'fixture_all_senses', ROBOT)
    metadata(directory, ROBOT, 'fixture_all_senses', started, 'completed', 1, dict(
        pipeline=dict(camera_topic=CAMERA_TOPIC, prompt='describe_scene',
                      sensing=dict(hear=True, vision=True, read=dict(enabled=True, session=str(conversation_directory)))),
        service='all', max_cycles=2), completed='2026-11-15T10:01:30.000000+01:00')
    vision = directory / 'observations/vision'
    camera = dict(topic=CAMERA_TOPIC, received_at='2026-11-15T09:00:05.250000+00:00', stamp=dict(sec=12, nanosec=0),
                  frame_id='fixture_camera', encoding='rgb8', width=64, height=48, step=192, is_bigendian=0)
    write(vision / '2026-11-15_09-00-05_250000Z_sense_output.png', png((200, 30, 30), camera))
    write(vision / '2026-11-15_09-00-05_250000Z_sense_output.md',
          '{"scene": "A red test card fills the view."}')
    write(vision / '2026-11-15_09-01-10_500000Z_sense_output.png',
          png((30, 30, 200), dict(camera, received_at='2026-11-15T09:01:10.500000+00:00')))
    audio = directory / 'observations/audio/2026-11-15T090008_000000Z_ab12cd34'
    write(audio / 'recording.wav', wav())
    write(audio / 'transcript.md', transcript('2026-11-15T09:00:08+00:00', ROBOT, 'Please look at the red card.'))
    read = dict(modality='read', epistemic_status='attributed_user_statements_not_verified_physical_facts',
                session_id=CONVERSATION, session_path=str(conversation_directory), observed_at='2026-11-15T09:00:20+00:00',
                observations=[dict(modality='read', role='user', epistemic_status='unverified_user_statement',
                                   content=text, message_index=index, timestamp=when,
                                   source_record=str(conversation_directory / f'state/cycle_{cycle:06d}/sense.json'),
                                   source=str(conversation_directory / 'conversation.md') + f'#message-{index}',
                                   provenance=dict(sequence=sequence))
                              for text, index, when, cycle, sequence in (
                                  ('There is a red card on the table.', 1, '2026-11-15T09:59:00.000000+01:00', 1, 1),
                                  ('The card belongs to Ana.', 3, '2026-11-15T09:59:30.000000+01:00', 2, 3))],
                cursor=dict(session_id=CONVERSATION, session_path=str(conversation_directory), message_index=3,
                            prefix_sha256='0' * 64))
    output = dict(vision='{"scene": "A red test card fills the view."}', speech='Please look at the red card.', read=read)
    record(directory, 1, 'sense.json', dict(
        stage='sense', cycle=1, sequence=1, invocation=1, started_at='2026-11-15T10:00:04.000000+01:00',
        prompt_name='describe_scene', source_record=None, previous_record=None, output=json.dumps(output),
        status='generated', read_cursor=read['cursor'],
        source_observations=['observations/audio/2026-11-15T090008_000000Z_ab12cd34/recording.wav',
                             'observations/audio/2026-11-15T090008_000000Z_ab12cd34/transcript.md',
                             'observations/vision/2026-11-15_09-00-05_250000Z_sense_output.md',
                             'observations/vision/2026-11-15_09-00-05_250000Z_sense_output.png']))
    record(directory, 2, 'sense.json', dict(
        stage='sense', cycle=2, sequence=5, invocation=1, started_at='2026-11-15T10:01:09.000000+01:00',
        prompt_name='describe_scene', status='failed', error='TimeoutError: /sense response timed out after 120s',
        source_observations=['observations/vision/2026-11-15_09-01-10_500000Z_sense_output.png']))
    return directory


def conversation_run(root):
    """A conversation session, like ``smpa_ros.conversation_web`` produces.

    Its Sense records each hold one user message; Act records hold replies.
    The transcript uses the length-delimited format of ``conversation.py``.
    """
    started = '2026-11-15T09:58:00.000000+01:00'
    directory = run_directory(root, started, 'conversation', CONVERSATION)
    metadata(directory, CONVERSATION, 'conversation', started, 'running', 2, dict(kind='conversation', pipeline={}))
    messages = [('User', 'There is a red card on the table.'), ('Assistant', 'Noted.'),
                ('User', 'The card belongs to Ana.'), ('Assistant', 'Understood.')]
    body = b'# Conversation\n\n'
    for number, (role, text) in enumerate(messages, 1):
        data = text.encode()
        body += f'<!-- smpa-message {role} {len(data)} -->\n<a id="message-{number}"></a>\n## {role}\n\n'.encode() + data + b'\n\n'
    write(directory / 'conversation.md', body)
    for cycle, (sequence, when, text) in enumerate(((1, '2026-11-15T09:59:00.000000+01:00', messages[0][1]),
                                                     (3, '2026-11-15T09:59:30.000000+01:00', messages[2][1])), 1):
        record(directory, cycle, 'sense.json', dict(sequence=sequence, invocation=1, started_at=when,
                                                    prompt_name='conversation', source_record=None, previous_record=None,
                                                    stage='sense', status='generated', output=text))
        record(directory, cycle, 'act.json', dict(sequence=sequence + 1, stage='act', status='succeeded',
                                                  output=messages[cycle * 2 - 1][1]))
    return directory


def legacy_layout_run(root):
    """An October run with ``vision/`` at the top level and local-time file names, like ``..._session_c4c754bb``."""
    started = '2026-10-23T16:27:06.431836+02:00'
    directory = root / 'runs' / f'2026-10-23T162706_431836+0200_session_{LEGACY_LAYOUT}'
    write(directory / 'metadata.yaml', yaml.safe_dump(dict(
        run_id=LEGACY_LAYOUT, run_name='session', started_at=started, completed_at='2026-10-23T16:27:42.655763+02:00',
        status='completed', completed_cycles=1, configuration=dict(
            pipeline=dict(camera_topic=CAMERA_TOPIC, model='smolvlm2', prompt='describe_scene'), service='vision')),
        sort_keys=False))
    write(directory / 'vision/2026-10-23_16-27-41_221845_sense_output.md', 'A room with a mannequin.')
    record(directory, 1, 'sense.json', dict(stage='sense', cycle=1, output='{"vision": "A room with a mannequin."}'))
    return directory


def refinement_run(root):
    """A scene-refinement run with a frozen image and a focused answer, like ``..._focused_final_consolidation``."""
    started = '2026-11-06T16:06:10.337905+01:00'
    directory = run_directory(root, started, 'focused', REFINEMENT)
    metadata(directory, REFINEMENT, 'focused', started, 'completed', 2,
             dict(pipeline=dict(camera_topic=CAMERA_TOPIC, enabled_actions=['observe']), service='vision'),
             completed='2026-11-06T16:12:05.213000+01:00')
    write(directory / 'observations/vision/frozen_image.png', png((30, 160, 30)))
    write(directory / 'observations/vision/observe_000001_001.json', json.dumps(dict(
        target='entity_1', question='What is the color of entity_1?',
        output='{"status": "answered", "answer": "Green."}', image_sha256='58' * 32,
        model_identifier='Qwen/Qwen3-VL-2B-Instruct')))
    record(directory, 1, 'sense.json', dict(stage='sense', cycle=1, sequence=1, started_at='2026-11-06T16:06:22.160805+01:00',
                                            status='generated', output='{"vision": "A green figure."}'))
    record(directory, 2, 'sense.json', dict(stage='sense', cycle=2, sequence=5, started_at='2026-11-06T16:10:58.068759+01:00',
                                            status='generated', output=json.dumps(dict(
                                                initial_observation='{"vision": "A green figure."}',
                                                focused_observations=[dict(target='entity_1', output='Green.',
                                                                           source_record='observations/vision/observe_000001_001.json')]))))
    return directory


def lifecycle_runs(root):
    """Runs for the states the GUI must handle: running (active or stale), empty, and damaged."""
    for identity, name, status in ((ACTIVE, 'fixture_active', 'running'), (STALE, 'fixture_stale', 'running'),
                                   (EMPTY, 'fixture_empty', 'completed')):
        started = {'fixture_active': '2026-11-15T11:00:00.000000+01:00', 'fixture_stale': '2026-11-15T08:00:00.000000+01:00',
                   'fixture_empty': '2026-11-15T07:00:00.000000+01:00'}[name]
        metadata(run_directory(root, started, name, identity), identity, name, started, status, 0,
                 dict(pipeline=dict(camera_topic=CAMERA_TOPIC, sensing=dict(hear=True, vision=True)), service='all'),
                 completed=None if status == 'running' else '2026-11-15T07:00:05.000000+01:00')
    broken = root / 'runs' / f'2026-11-15_060000_000000+0100_broken_{BROKEN}'
    write(broken / 'metadata.yaml', 'run_id: [unterminated\n')


def legacy_archive(root):
    """The flat ``memory/legacy`` archive from before run directories existed."""
    write(root / 'legacy/2026-10-19_20-14-59_034492Z_sense_output.md', 'An archived description.')
    write(root / 'legacy/audio/2026-10-19T233406_470379Z_6nne9aaj/transcript.md',
          transcript('2026-10-19T23:34:06.470379+00:00', 'legacy', 'Hello').replace('- Run ID: legacy\n', ''))


def build(root, demo=False):
    """Write every fixture session under ``root`` and return the root.

    ``demo`` adds the GUI's demonstration marker. An existing directory is
    extended, so call this on an empty directory.
    """
    root = Path(root)
    (root / 'runs').mkdir(parents=True, exist_ok=True)
    conversation = conversation_run(root)
    robot_run(root, conversation)
    legacy_layout_run(root)
    refinement_run(root)
    lifecycle_runs(root)
    legacy_archive(root)
    if demo:
        write(root / '.smpa-gui-demo', 'Demonstration data generated by gui/tests/fixtures/smpa_memory.py. '
                                       'Not recorded by SMPA.\n')
    return root


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--demo', action='store_true', help='mark the directory as GUI demonstration data')
    options = parser.parse_args()
    if options.output.exists() and any(options.output.iterdir()):
        parser.error('the output directory must be empty or absent')
    print(build(options.output, options.demo))
