"""CPU speech/audio backends. These classes have no ROS dependencies."""

from queue import Empty, Full, Queue
from time import monotonic


class SpeechCanceled(Exception):
    """Cooperative cancellation at an audio/model boundary."""


def check_cancel(cancel):
    """Stop an operation when its shared cancellation event has been set.

    Raise SpeechCanceled before further capture, synthesis or inference work.
    Otherwise return without changing the event or the caller's state.
    """
    if cancel.is_set():
        raise SpeechCanceled('Canceled')


class AudioBackend:
    """Keep a handle to the active PortAudio stream for immediate abort."""
    stream = None

    def stop(self):
        """Abort the active audio stream so cancellation can release the device.

        If no stream is active, do nothing. Ignore an abort error because a
        concurrent normal close may already have released the stream.
        """
        stream = self.stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                # A concurrent normal close/abort can already have stopped it.
                pass


class FasterWhisperBackend(AudioBackend):
    def __init__(self, model_path, device=None, silence_seconds=0.8, threshold=0.015):
        """Load one local CPU Whisper model and retain audio capture settings.

        Use the existing int8 precision and four threads without downloading.
        Device selects the microphone; silence duration and amplitude threshold
        determine when captured speech ends. Keep the model for later requests.
        """
        from faster_whisper import WhisperModel

        self.model = WhisperModel(str(model_path), device='cpu', compute_type='int8',
                                 cpu_threads=4, local_files_only=True)
        self.device = device
        self.silence_seconds = silence_seconds
        self.threshold = threshold

    def listen(self, language, max_duration, cancel, feedback, on_audio=None):
        """Capture bounded microphone speech, preserve its audio, then transcribe it.

        Capture mono 16 kHz blocks while reporting waiting and recording states.
        Stop after trailing silence or the duration limit, checking cancellation
        and capture errors throughout. Always close the stream before inference.
        Reject silence; otherwise call on_audio when supplied, then transcribe
        with the retained model. Cancellation and errors propagate to the caller.
        """
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
            """Queue one mono capture block without blocking the audio callback.

            Copy the first channel and record device status or queue overflow
            for the capture loop to report. Audio callback timing is unchanged.
            """
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
        """Recognize captured audio with the already loaded Whisper model.

        Use the requested language or automatic detection, the existing greedy
        beam and VAD settings. Check cancellation between segments and join
        their text. Reject an empty transcript; return recognized text otherwise.
        """
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
        """Load one local Piper voice on CPU and retain the output-device choice.

        Reuse this voice for subsequent Talk requests. No download, playback,
        CUDA model or background process is started during construction.
        """
        from piper import PiperVoice

        self.voice = PiperVoice.load(str(model_path), use_cuda=False)
        self.device = device

    def synthesize(self, text, cancel):
        """Synthesize one request as mono signed 16-bit PCM bytes.

        Check cancellation before and between chunks from the retained voice.
        Reject unsupported chunk formats and empty audio. Return joined bytes
        without starting playback or changing the saved voice.
        """
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
        """Synthesize and play text while preserving cooperative cancellation.

        Report synthesis, then stream short PCM chunks to the selected output
        device. Check cancellation between writes and report playback progress.
        Drain successful playback before completion, always release the active
        stream reference, and propagate synthesis, playback or cancellation errors.
        """
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
