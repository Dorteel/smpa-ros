#!/usr/bin/env python3
"""Use a prerecorded microphone device only for production-launch acceptance tests.

This interpreter prefix reexecutes the existing repository virtual environment,
then substitutes PortAudio input with the same callback fixture used by speech
regressions. Recognition, audio persistence, ROS actions, experiment control and inference
are production code. It is never selected by the normal launcher.
"""
import os
from pathlib import Path
import runpy
import sys


def main():
    """Run the requested ROS executable with an explicitly supplied audio fixture.

    Require SMPA_TEST_AUDIO so this helper cannot silently become a live or empty
    input device. Preserve the real Faster-Whisper acquisition/transcription code
    by substituting only sounddevice.InputStream. Reexec into .venv if necessary,
    keeping the same process identity and ROS arguments; no second loop starts.
    """
    python = Path(__file__).resolve().parents[1] / '.venv/bin/python'
    if sys.argv[1:2] != ['--venv']:
        os.execv(str(python), [str(python), __file__, '--venv', *sys.argv[1:]])
    del sys.argv[1]
    import numpy as np
    import sounddevice
    from faster_whisper.audio import decode_audio
    from test_speech_audio import InputStream
    samples = np.concatenate([decode_audio(os.environ['SMPA_TEST_AUDIO']),
                              np.zeros(16000, dtype=np.float32)])

    def input_stream(**settings):
        """Feed exact prerecorded samples through the normal PortAudio callback API."""
        return InputStream(samples=samples, **settings)

    sounddevice.InputStream = input_stream
    executable = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_path(executable, run_name='__main__')


if __name__ == '__main__':
    main()
