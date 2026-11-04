"""CPU speech/audio backends. These classes have no ROS dependencies."""

from queue import Empty, Full, Queue
from time import monotonic


class SpeechCanceled(Exception):
    """Cooperative cancellation at an audio/model boundary."""


def check_cancel(cancel):
    if cancel.is_set():
        raise SpeechCanceled('Canceled')


class AudioBackend:
    """Keep a handle to the active PortAudio stream for immediate abort."""
    stream = None

    def stop(self):
        stream = self.stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                # A concurrent normal close/abort can already have stopped it.
                pass


class FasterWhisperBackend(AudioBackend):
    def __init__(self, model_path, device=None, silence_seconds=0.8, threshold=0.015):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(str(model_path), device='cpu', compute_type='int8',
                                 cpu_threads=4, local_files_only=True)
        self.device = device
        self.silence_seconds = silence_seconds
        self.threshold = threshold

    def listen(self, language, max_duration, cancel, feedback, on_audio=None):
        import numpy as np
        import sounddevice as sd

        sample_rate = 16000
        blocks = Queue(maxsize=8)
        errors = []
        frames = []
        heard_speech = False
        quiet_samples = total_samples = 0
        started = monotonic()
        check_cancel(cancel)
        feedback('waiting', 0.0)

        def receive(data, count, timestamp, status):
            if status:
                errors.append(str(status))
            try:
                blocks.put_nowait(data[:, 0].copy())
            except Full:
                errors.append('Microphone input queue overflow')

        try:
            with sd.InputStream(samplerate=sample_rate, channels=1, dtype='float32',
                                blocksize=1600, device=self.device, callback=receive) as stream:
                self.stream = stream
                while monotonic() - started < max_duration and total_samples < max_duration * sample_rate:
                    check_cancel(cancel)
                    if errors:
                        raise IOError(f'Microphone error: {errors[0]}')
                    try:
                        block = blocks.get(timeout=min(0.05, max(0, max_duration - (monotonic() - started))))
                    except Empty:
                        continue
                    block = block[:max(0, int(max_duration * sample_rate) - total_samples)]
                    frames.append(block)
                    total_samples += block.size
                    active = block.size > 0 and float(np.sqrt(np.mean(block * block))) >= self.threshold
                    if active:
                        heard_speech = True
                        quiet_samples = 0
                    elif heard_speech:
                        quiet_samples += block.size
                    feedback('recording' if heard_speech else 'waiting', monotonic() - started)
                    if heard_speech and quiet_samples >= self.silence_seconds * sample_rate:
                        break
        finally:
            self.stream = None
        check_cancel(cancel)
        if not heard_speech:
            raise TimeoutError('No speech detected before max_duration')
        # Capture is closed before the model starts, even on transcription errors.
        audio = np.concatenate(frames)
        if on_audio is not None:
            on_audio(audio)
        check_cancel(cancel)
        feedback('transcribing', monotonic() - started)
        return self.transcribe(audio, language, cancel)

    def transcribe(self, audio, language, cancel):
        check_cancel(cancel)
        segments, _ = self.model.transcribe(audio, language=language or None,
                                             beam_size=1, vad_filter=True)
        text = []
        for segment in segments:
            check_cancel(cancel)
            text.append(segment.text)
        check_cancel(cancel)
        result = ' '.join(text).strip()
        if not result:
            raise ValueError('No speech was recognized')
        return result


class PiperBackend(AudioBackend):
    def __init__(self, model_path, device=None):
        from piper import PiperVoice

        self.voice = PiperVoice.load(str(model_path), use_cuda=False)
        self.device = device

    def synthesize(self, text, cancel):
        check_cancel(cancel)
        chunks = []
        for chunk in self.voice.synthesize(text):
            check_cancel(cancel)
            if chunk.sample_channels != 1 or chunk.sample_width != 2:
                raise ValueError('Piper returned an unsupported audio format')
            chunks.append(chunk.audio_int16_bytes)
        check_cancel(cancel)
        audio = b''.join(chunks)
        if not audio:
            raise ValueError('Piper returned empty audio')
        return audio

    def talk(self, text, cancel, feedback):
        import sounddevice as sd

        feedback('synthesizing', 0.0)
        audio = self.synthesize(text, cancel)
        rate = self.voice.config.sample_rate
        chunk_bytes = max(2, rate // 20 * 2)
        try:
            with sd.RawOutputStream(samplerate=rate, channels=1, dtype='int16',
                                    blocksize=rate // 20, device=self.device) as stream:
                self.stream = stream
                check_cancel(cancel)
                feedback('playing', 0.0)
                for offset in range(0, len(audio), chunk_bytes):
                    check_cancel(cancel)
                    stream.write(audio[offset:offset + chunk_bytes])
                    feedback('playing', min(0.99, (offset + chunk_bytes) / len(audio)))
                # stop() drains queued output; only then is playback complete.
                stream.stop()
        finally:
            self.stream = None
        check_cancel(cancel)
        feedback('playing', 1.0)
