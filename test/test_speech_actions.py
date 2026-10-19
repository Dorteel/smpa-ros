from threading import Event, Thread
from time import monotonic, sleep

from action_msgs.msg import GoalStatus
import pytest
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from rclpy.parameter import Parameter
from smpa_ros_interfaces.action import Listen, Talk

from smpa_ros.backends.speech import SpeechCanceled
from smpa_ros.speech.actions import SpeechServer
from smpa_ros.speech.demo import ListenerTalker


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

    def listen(self, language, max_duration, cancel, feedback):
        feedback('waiting', 0.0)
        feedback('recording', 0.01)
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
