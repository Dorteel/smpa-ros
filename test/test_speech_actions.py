from threading import Event, Thread
from time import monotonic, sleep

from action_msgs.msg import GoalStatus
import pytest
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.parameter import Parameter
from smpa_ros_interfaces.action import Listen, Talk

from speech.backends import SpeechCanceled
from speech.actions import SpeechServer
from speech.demo import ListenerTalker


def wait(future, timeout=5):
    deadline = monotonic() + timeout
    while not future.done() and monotonic() < deadline:
        sleep(0.005)
    assert future.done(), 'ROS action request timed out'
    return future.result()


class FakeBackend:
    def __init__(self):
        self.started = Event()
        self.release = Event()
        self.stopped = Event()
        self.block = False
        self.error = None
        self.transcription = 'hello robot'
        self.spoken = []

    def stop(self):
        self.stopped.set()

    def operation(self, cancel):
        self.started.set()
        while self.block and not self.release.wait(0.005) and not cancel.is_set():
            pass
        if cancel.is_set():
            raise SpeechCanceled('Canceled')
        if self.error:
            raise self.error

    def listen(self, language, max_duration, cancel, feedback, on_audio=None):
        feedback('waiting', 0.0)
        feedback('recording', 0.01)
        import numpy as np
        if on_audio is not None:
            on_audio(np.full(1600, 0.2, dtype=np.float32))
        self.operation(cancel)
        feedback('transcribing', 0.02)
        return self.transcription

    def talk(self, text, cancel, feedback):
        feedback('synthesizing', 0.0)
        feedback('playing', 0.5)
        self.operation(cancel)
        self.spoken.append(text)
        feedback('playing', 1.0)


@pytest.fixture
def speech(tmp_path):
    rclpy.init(args=[])
    listener = SpeechServer('listen', FakeBackend(), tmp_path / 'audio.lock')
    talker = SpeechServer('talk', FakeBackend(), tmp_path / 'audio.lock')
    client = rclpy.create_node('speech_test_client')
    listen = ActionClient(client, Listen, '/speech/listen')
    talk = ActionClient(client, Talk, '/speech/talk')
    executor = MultiThreadedExecutor(num_threads=4)
    for node in (listener, talker, client):
        executor.add_node(node)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    assert listen.wait_for_server(timeout_sec=5)
    assert talk.wait_for_server(timeout_sec=5)
    try:
        yield listener, talker, listen, talk
    finally:
        listener.stop()
        talker.stop()
        executor.shutdown()
        thread.join(timeout=5)
        listen.destroy()
        talk.destroy()
        for node in (listener, talker):
            node.server.destroy()
            node.destroy_node()
        client.destroy_node()
        rclpy.shutdown()


@pytest.mark.parametrize('kind', ['listen', 'talk'])
def test_action_success_and_feedback(speech, kind):
    _, _, listen, talk = speech
    client = listen if kind == 'listen' else talk
    goal = Listen.Goal(language='en', max_duration=1.0) if kind == 'listen' else Talk.Goal(text='hello')
    feedback = []
    handle = wait(client.send_goal_async(goal, feedback_callback=lambda value: feedback.append(value.feedback)))
    assert handle.accepted
    response = wait(handle.get_result_async())
    assert response.status == GoalStatus.STATUS_SUCCEEDED and response.result.success
    deadline = monotonic() + 2
    while len(feedback) < 3 and monotonic() < deadline:
        sleep(0.005)
    if kind == 'listen':
        assert response.result.transcription == 'hello robot'
        assert [value.state for value in feedback] == ['waiting', 'recording', 'transcribing']
        assert feedback[-1].elapsed_seconds >= 0
    else:
        assert [value.state for value in feedback] == ['synthesizing', 'playing', 'playing']
        assert feedback[-1].progress == 1.0


@pytest.mark.parametrize('kind', ['listen', 'talk'])
def test_cancellation_and_half_duplex(speech, kind):
    listener, talker, listen, talk = speech
    server = listener if kind == 'listen' else talker
    client = listen if kind == 'listen' else talk
    goal = Listen.Goal(language='en', max_duration=2.0) if kind == 'listen' else Talk.Goal(text='hello')
    server.backend.block = True
    handle = wait(client.send_goal_async(goal))
    assert handle.accepted and server.backend.started.wait(2)
    # Reject both same-action overlap and cross-server microphone/playback overlap.
    assert not wait(listen.send_goal_async(Listen.Goal(language='en', max_duration=1.0))).accepted
    assert not wait(talk.send_goal_async(Talk.Goal(text='overlap'))).accepted
    canceled = wait(handle.cancel_goal_async())
    assert canceled.goals_canceling
    response = wait(handle.get_result_async())
    assert response.status == GoalStatus.STATUS_CANCELED
    assert not response.result.success
    assert server.backend.stopped.is_set()
    if kind == 'listen':
        session, = (listener.world_model_dir / 'observations' / 'audio').iterdir()
        assert (session / 'recording.wav').exists()
        assert 'Status: incomplete' in (session / 'transcript.md').read_text()
    server.backend.block = False
    next_goal = wait(talk.send_goal_async(Talk.Goal(text='audio is free')))
    assert next_goal.accepted
    assert wait(next_goal.get_result_async()).result.success


@pytest.mark.parametrize('error', [OSError('microphone failed'), TimeoutError('recording timed out')])
def test_listen_error_is_aborted(speech, error):
    listener, _, listen, _ = speech
    listener.backend.error = error
    handle = wait(listen.send_goal_async(Listen.Goal(language='en', max_duration=0.1)))
    response = wait(handle.get_result_async())
    assert response.status == GoalStatus.STATUS_ABORTED
    assert not response.result.success
    assert str(error) in response.result.error_message


def test_empty_speech_and_invalid_goals(speech):
    listener, _, listen, talk = speech
    assert not wait(listen.send_goal_async(Listen.Goal(max_duration=0.0))).accepted
    assert not wait(listen.send_goal_async(Listen.Goal(max_duration=31.0))).accepted
    assert not wait(talk.send_goal_async(Talk.Goal(text=' '))).accepted
    listener.backend.transcription = ''
    handle = wait(listen.send_goal_async(Listen.Goal(language='en', max_duration=1.0)))
    response = wait(handle.get_result_async())
    assert response.status == GoalStatus.STATUS_ABORTED
    assert 'No speech' in response.result.error_message


def test_demo_constructs_response_sequentially(speech):
    listener, talker, _, _ = speech
    demo = ListenerTalker()
    try:
        assert demo.turn() == 'I heard hello robot'
        assert talker.backend.spoken == ['I heard hello robot']
        assert listener.lease is None and talker.lease is None
        assert demo.active is None
    finally:
        demo.close()


def test_demo_timeout_cancels_listen_without_talking(speech):
    listener, talker, _, _ = speech
    listener.backend.block = True
    demo = ListenerTalker()
    demo.set_parameters([Parameter('action_timeout', value=0.1)])
    try:
        with pytest.raises(TimeoutError):
            demo.turn()
        assert listener.backend.stopped.is_set()
        assert listener.lease is None
        assert not talker.backend.spoken
        assert demo.active is None
    finally:
        demo.close()


def test_speech_models_registered():
    from pathlib import Path
    from ament_index_python.packages import get_package_share_directory
    import yaml

    registry = Path(get_package_share_directory('smpa_ros')) / 'models/model_library.yaml'
    models = yaml.safe_load(registry.read_text())['models']
    assert models['faster-whisper-base']['backend'] == 'faster_whisper'
    assert models['piper-lessac-medium']['backend'] == 'piper'
    assert all(models[name]['device'] == 'cpu' for name in ('faster-whisper-base', 'piper-lessac-medium'))


def test_listen_persists_separate_linked_observations(speech):
    import struct
    import numpy as np

    listener, _, listen, _ = speech
    for _ in range(2):
        handle = wait(listen.send_goal_async(Listen.Goal(language='en', max_duration=1.0)))
        response = wait(handle.get_result_async())
        assert response.result.success
    sessions = list((listener.world_model_dir / 'observations' / 'audio').iterdir())
    assert len(sessions) == 2
    for session in sessions:
        data = (session / 'recording.wav').read_bytes()
        assert data[:4] == b'RIFF' and data[8:12] == b'WAVE'
        assert struct.unpack_from('<HHI', data, 20) == (3, 1, 16000)
        assert struct.unpack_from('<I', data, 4)[0] == len(data) - 8
        np.testing.assert_array_equal(np.frombuffer(data, dtype='<f4', offset=56),
                                      np.full(1600, 0.2, dtype=np.float32))
        text = (session / 'transcript.md').read_text()
        assert 'hello robot' in text and 'Audio file: recording.wav' in text
        assert 'Status: complete' in text and 'Language: en' in text
        assert 'faster-whisper-base' in text


def test_transcription_failure_preserves_incomplete_recording(speech):
    listener, _, listen, _ = speech
    listener.backend.error = RuntimeError('transcription failed')
    handle = wait(listen.send_goal_async(Listen.Goal(language='en', max_duration=1.0)))
    response = wait(handle.get_result_async())
    assert response.status == GoalStatus.STATUS_ABORTED
    session, = (listener.world_model_dir / 'observations' / 'audio').iterdir()
    assert (session / 'recording.wav').stat().st_size > 56
    text = (session / 'transcript.md').read_text()
    assert 'Status: incomplete' in text and 'transcription failed' in text


def test_audio_storage_failure_aborts_action(speech):
    listener, _, listen, _ = speech
    (listener.world_model_dir / 'observations' / 'audio').rmdir()
    (listener.world_model_dir / 'observations' / 'audio').write_text('A file blocks directory creation')
    handle = wait(listen.send_goal_async(Listen.Goal(language='en', max_duration=1.0)))
    response = wait(handle.get_result_async())
    assert response.status == GoalStatus.STATUS_ABORTED and not response.result.success
    assert response.result.error_message


def test_transcript_storage_failure_aborts_and_preserves_audio(speech, monkeypatch):
    from speech.observation import AudioObservation
    original = AudioObservation.write_transcript

    def fail_complete(self, status, text='', error=''):
        if status == 'complete':
            raise OSError('disk full')
        original(self, status, text, error)

    monkeypatch.setattr(AudioObservation, 'write_transcript', fail_complete)
    listener, _, listen, _ = speech
    handle = wait(listen.send_goal_async(Listen.Goal(language='en', max_duration=1.0)))
    response = wait(handle.get_result_async())
    assert response.status == GoalStatus.STATUS_ABORTED
    assert 'disk full' in response.result.error_message
    session, = (listener.world_model_dir / 'observations' / 'audio').iterdir()
    assert (session / 'recording.wav').exists()
    assert 'Status: incomplete' in (session / 'transcript.md').read_text()


@pytest.mark.parametrize('kind', ['listen', 'talk'])
@pytest.mark.parametrize('state', ['idle', 'active', 'canceled'])
def test_speech_subprocess_sigint(tmp_path, assigned_test_run, kind, state):
    """Stop an actual speech-server subprocess through its process group.

    Substitute only the audio backend so this test never opens a microphone or
    speaker. Run the production entry function and executor in a fresh session,
    using the assigned temporary run and unique action endpoint. Exercise an
    idle server, an accepted blocking request, or a request explicitly canceled
    before shutdown. Require explicit cancellation to return CANCELED as usual.

    Once the child is ready, send SIGINT as the experiment does and require a zero exit
    within three seconds, a stopped backend, a closed ROS context and a released
    audio lease. On failure dump the child's threads before terminating only that
    test process group. Always destroy the parent ROS client and context.
    """
    import json
    import os
    from pathlib import Path
    import signal
    import subprocess
    import sys
    from rclpy.executors import SingleThreadedExecutor

    ready = tmp_path / 'ready'
    active = tmp_path / 'active'
    finished = tmp_path / 'finished'
    endpoint = '/shutdown_' + assigned_test_run.run_id.replace('-', '_') + '/' + kind
    script = '''
import faulthandler, json, runpy, signal, sys
from pathlib import Path
import rclpy
from speech import actions
faulthandler.register(signal.SIGUSR1, all_threads=True)
FakeBackend = runpy.run_path(sys.argv[1])['FakeBackend']
backend = FakeBackend()
backend.block = True
original_operation = backend.operation
def operation(cancel):
    """Mark that a real action callback reached the blocking fake audio backend."""
    Path(sys.argv[3]).write_text('active')
    return original_operation(cancel)
backend.operation = operation
original_server = actions.SpeechServer
server = None
def create_server(kind):
    """Create the production action server with only its audio device substituted."""
    global server
    server = original_server(kind, backend=backend)
    Path(sys.argv[2]).write_text('ready')
    return server
actions.SpeechServer = create_server
actions.run(sys.argv[5], args=sys.argv[6:])
Path(sys.argv[4]).write_text(json.dumps({'stopped': backend.stopped.is_set(),
                                      'lease_closed': server.lease is None,
                                      'context_closed': not rclpy.ok()}))
'''
    command = [sys.executable, '-c', script, str(Path(__file__).resolve()),
               str(ready), str(active), str(finished), kind, '--ros-args',
               '-p', f'world_model_dir:={assigned_test_run.directory}',
               '-p', f'run_id:={assigned_test_run.run_id}',
               '-p', f'audio_lock:={tmp_path / "audio.lock"}',
               '-r', f'/speech/{kind}:={endpoint}']
    process = None
    node = client = executor = None
    rclpy.init(args=[])
    try:
        with (tmp_path / 'child.log').open('w+') as output:
            process = subprocess.Popen(command, stdout=output, stderr=output, start_new_session=True)
            deadline = monotonic() + 15
            while not ready.exists() and process.poll() is None and monotonic() < deadline:
                sleep(0.01)
            assert ready.exists(), (tmp_path / 'child.log').read_text()
            # Let the idle executor enter its native wait before sending a signal.
            sleep(0.2)
            if state != 'idle':
                node = rclpy.create_node('shutdown_test_client')
                executor = SingleThreadedExecutor()
                executor.add_node(node)
                action_type = Listen if kind == 'listen' else Talk
                client = ActionClient(node, action_type, endpoint)
                assert client.wait_for_server(timeout_sec=5)
                goal = (Listen.Goal(language='en', max_duration=1.0) if kind == 'listen'
                        else Talk.Goal(text='test without audio playback'))
                future = client.send_goal_async(goal)
                executor.spin_until_future_complete(future, timeout_sec=5)
                assert future.done() and future.result().accepted
                handle = future.result()
                deadline = monotonic() + 5
                while not active.exists() and monotonic() < deadline:
                    sleep(0.01)
                assert active.exists(), 'Audio callback did not begin'
                if state == 'canceled':
                    future = handle.cancel_goal_async()
                    executor.spin_until_future_complete(future, timeout_sec=5)
                    assert future.done() and future.result().goals_canceling
                    future = handle.get_result_async()
                    executor.spin_until_future_complete(future, timeout_sec=5)
                    assert future.done() and future.result().status == GoalStatus.STATUS_CANCELED
            started = monotonic()
            os.killpg(process.pid, signal.SIGINT)
            try:
                code = process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.kill(process.pid, signal.SIGUSR1)
                sleep(0.1)
                pytest.fail('SIGINT did not stop speech within 3 seconds:\n' +
                            (tmp_path / 'child.log').read_text())
            latency = monotonic() - started
            print(f'{kind}/{state}: SIGINT shutdown {latency:.3f}s, exit={code}')
            assert code == 0, (tmp_path / 'child.log').read_text()
            assert json.loads(finished.read_text()) == dict(
                stopped=True, lease_closed=True, context_closed=True)
            log = (tmp_path / 'child.log').read_text()
            assert 'Traceback' not in log and 'InvalidHandle' not in log
            assert 'cannot use Destroyable' not in log
            assert 'exception was never retrieved' not in log
            assert 'unhandled exception' not in log and 'failed to publish' not in log.lower()
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        if executor is not None:
            executor.shutdown()
        if client is not None:
            client.destroy()
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()
