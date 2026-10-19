import importlib.util
from pathlib import Path
from threading import Thread
from time import perf_counter
from types import SimpleNamespace
import sys
import wave

import numpy as np
import pytest
import rclpy
from rclpy.executors import MultiThreadedExecutor

from smpa_ros.backends.speech import FasterWhisperBackend, PiperBackend
from smpa_ros.speech.actions import SpeechServer
from smpa_ros.speech.demo import ListenerTalker
from test_speech_audio import InputStream, OutputStream


@pytest.mark.integration
def test_prerecorded_listener_talker(tmp_path, monkeypatch):
    """Real STT/TTS and ROS actions; only microphone/speaker devices are simulated."""
    root = Path(__file__).resolve().parents[1] / 'models/speech'
    stt_path = root / 'faster-whisper-base'
    voice_path = root / 'piper/en_US-lessac-medium.onnx'
    if (not (stt_path / 'model.bin').exists() or not voice_path.exists()
            or importlib.util.find_spec('faster_whisper') is None
            or importlib.util.find_spec('piper') is None):
        pytest.skip('Download registered speech models and install .venv speech dependencies first')
    from faster_whisper.audio import decode_audio

    stt = FasterWhisperBackend(stt_path, threshold=0.005)
    tts = PiperBackend(voice_path)
    recorded = tmp_path / 'prerecorded.wav'
    with wave.open(str(recorded), 'wb') as output:
        tts.voice.synthesize_wav('Hello robot.', output)
    samples = np.concatenate([decode_audio(str(recorded)), np.zeros(16000, dtype=np.float32)])
    outputs = []

    def output_stream(**kwargs):
        outputs.append(OutputStream(**kwargs))
        return outputs[-1]

    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(
        InputStream=lambda **kwargs: InputStream(samples=samples, **kwargs),
        RawOutputStream=output_stream,
    ))
    rclpy.init(args=[])
    listener = SpeechServer('listen', stt, tmp_path / 'audio.lock')
    listener.world_model_dir = tmp_path / 'world_model'
    talker = SpeechServer('talk', tts, tmp_path / 'audio.lock')
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(listener)
    executor.add_node(talker)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    demo = ListenerTalker()
    started = perf_counter()
    try:
        response = demo.turn()
        observations = list((listener.world_model_dir / 'audio').iterdir())
        assert len(observations) == 1
        assert (observations[0] / 'recording.wav').stat().st_size > 56
        saved = decode_audio(str(observations[0] / 'recording.wav'))
        np.testing.assert_array_equal(saved, samples[:len(saved)])
        transcript = (observations[0] / 'transcript.md').read_text()
        assert response.removeprefix('I heard ') in transcript
        assert 'recording.wav' in transcript and 'Status: complete' in transcript
        assert response.startswith('I heard ')
        assert 'hello' in response.lower() and 'robot' in response.lower()
        assert len(outputs) == 1 and outputs[0].audio and outputs[0].drained
        assert listener.lease is None and talker.lease is None
        print(f'Prerecorded speech turn: {response!r}; {perf_counter() - started:.2f}s')
    finally:
        demo.close()
        listener.stop()
        talker.stop()
        executor.shutdown()
        thread.join(timeout=5)
        for node in (listener, talker):
            node.server.destroy()
            node.destroy_node()
        rclpy.shutdown()
