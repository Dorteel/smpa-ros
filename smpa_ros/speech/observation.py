"""Lossless microphone observations, independent of ROS and model inference."""
from datetime import datetime, timezone
from pathlib import Path
import os
import struct
from tempfile import mkdtemp


class AudioObservation:
    def __init__(self, root: Path, audio, language: str, model: str):
        self.timestamp = datetime.now(timezone.utc)
        parent = root / 'audio'
        parent.mkdir(parents=True, exist_ok=True)
        # Exclusive directory creation also prevents collisions within a microsecond.
        self.path = Path(mkdtemp(prefix=self.timestamp.strftime('%Y-%m-%dT%H%M%S_%fZ_'), dir=parent))
        self.language = language or 'auto'
        self.model = model
        self.write_transcript('incomplete', '', 'Transcription pending')
        # IEEE float WAV (format 3), mono, 16 kHz, little-endian float32.
        # No quantization: these are the exact float32 samples submitted to Whisper.
        data = audio.astype('<f4', copy=False).tobytes()
        with (self.path / 'recording.wav').open('xb') as output:
            output.write(struct.pack('<4sI4s4sIHHIIHH4sII4sI',
                                     b'RIFF', 48 + len(data), b'WAVE', b'fmt ', 16,
                                     3, 1, 16000, 64000, 4, 32,
                                     b'fact', 4, len(audio), b'data', len(data)))
            output.write(data)
            output.flush()
            os.fsync(output.fileno())

    def write_transcript(self, status: str, text: str = '', error: str = ''):
        content = (f'# Audio Observation\n\n- Timestamp: {self.timestamp.isoformat()}\n'
                   f'- Source: ROS 2 Listen action\n- Audio file: recording.wav\n'
                   f'- Audio format: 16000 Hz, mono, IEEE float32 PCM (lossless)\n'
                   f'- Language: {self.language}\n- Recognition model: {self.model}\n'
                   f'- Status: {status}\n')
        if error:
            content += f'- Error: {error}\n'
        content += f'\n## Transcript\n\n{text}\n'
        pending = self.path / 'transcript.md.tmp'
        with pending.open('w', encoding='utf-8') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        pending.replace(self.path / 'transcript.md')
