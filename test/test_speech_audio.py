from threading import Event, Thread
from time import sleep
from types import SimpleNamespace
import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

from smpa_ros.backends.speech import FasterWhisperBackend, PiperBackend, SpeechCanceled


class InputStream:
    """Feed prerecorded samples through the same callback API as PortAudio."""
    def __init__(self, samples=None, callback=None, **kwargs):
        self.samples = samples
        self.callback = callback
        self.closed = Event()
        self.aborted = Event()
        self.thread = None

    def __enter__(self):
        if self.samples is not None:
            def feed():
                for offset in range(0, len(self.samples), 1600):
                    if self.closed.wait(0.005) or self.aborted.is_set():
                        break
                    block = self.samples[offset:offset + 1600, None]
                    self.callback(block, len(block), None, None)
            self.thread = Thread(target=feed)
            self.thread.start()
        return self

    def abort(self):
        self.aborted.set()

    def __exit__(self, *args):
        self.closed.set()
        if self.thread is not None:
            self.thread.join(timeout=2)


class OutputStream:
    def __init__(self, **kwargs):
        self.audio = bytearray()
        self.aborted = Event()
        self.closed = Event()
        self.drained = False

    def __enter__(self):
        return self

    def write(self, audio):
        if self.aborted.is_set():
            raise IOError('Playback aborted')
        self.audio.extend(audio)
        sleep(0.002)

    def stop(self):
        self.drained = True

    def abort(self):
        self.aborted.set()

    def __exit__(self, *args):
        self.closed.set()


def recorder():
    backend = FasterWhisperBackend.__new__(FasterWhisperBackend)
    backend.device = None
    backend.threshold = 0.01
    backend.silence_seconds = 0.2
    backend.model = MagicMock()
    backend.model.transcribe.return_value = (iter([SimpleNamespace(text='hello robot')]), None)
    return backend


def test_recording_stops_after_silence(monkeypatch):
    backend = recorder()
    samples = np.concatenate([np.ones(1600, dtype=np.float32) * 0.2, np.zeros(6400, dtype=np.float32)])
    streams = []

    def create(**kwargs):
        streams.append(InputStream(samples=samples, **kwargs))
        return streams[-1]

    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(InputStream=create))
    feedback = []
    assert backend.listen('en', 1.0, Event(), lambda *args: feedback.append(args)) == 'hello robot'
    assert streams[0].closed.is_set() and backend.stream is None
    assert [entry[0] for entry in feedback][0] == 'waiting'
    assert feedback[-1][0] == 'transcribing'
    audio = backend.model.transcribe.call_args.args[0]
    assert len(audio) == 4800  # One speech block plus two silence blocks.


def test_microphone_timeout_without_audio(monkeypatch):
    backend = recorder()
    streams = []

    def create(**kwargs):
        streams.append(InputStream(**kwargs))
        return streams[-1]

    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(InputStream=create))
    with pytest.raises(TimeoutError, match='No speech'):
        backend.listen('en', 0.04, Event(), lambda *_: None)
    backend.model.transcribe.assert_not_called()
    assert streams[0].closed.is_set()


def test_capture_cancel_aborts_stream(monkeypatch):
    backend = recorder()
    opened = Event()
    stream = InputStream()

    def create(**kwargs):
        opened.set()
        return stream

    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(InputStream=create))
    cancel = Event()
    errors = []

    def record():
        try:
            backend.listen('en', 2.0, cancel, lambda *_: None)
        except Exception as error:
            errors.append(error)

    thread = Thread(target=record)
    thread.start()
    assert opened.wait(1)
    # Wait until capture has registered its stream, then mimic the action callback.
    for _ in range(100):
        if backend.stream is not None:
            break
        sleep(0.001)
    cancel.set()
    backend.stop()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert stream.aborted.is_set() and stream.closed.is_set()
    assert isinstance(errors[0], SpeechCanceled)


def test_playback_cancel_aborts_stream(monkeypatch):
    backend = PiperBackend.__new__(PiperBackend)
    backend.device = None
    backend.voice = SimpleNamespace(config=SimpleNamespace(sample_rate=16000),
                                    synthesize=lambda _: iter([SimpleNamespace(
                                        sample_channels=1, sample_width=2,
                                        audio_int16_bytes=b'\0' * 32000)]))
    stream = OutputStream()
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(RawOutputStream=lambda **_: stream))
    cancel = Event()

    def feedback(state, progress):
        if state == 'playing' and progress > 0:
            cancel.set()
            backend.stop()

    with pytest.raises(SpeechCanceled):
        backend.talk('hello', cancel, feedback)
    assert stream.aborted.is_set() and stream.closed.is_set()
    assert not stream.drained
    assert len(stream.audio) < 32000
