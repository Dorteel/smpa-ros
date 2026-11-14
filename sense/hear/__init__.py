"""Auditory observations through the existing Listen action and audio store.

Faster-Whisper, microphone acquisition, cancellation and lossless persistence
remain in the reused speech infrastructure. Talk is output and is not called by
Hear. No additional process, executor, model or audio transfer is introduced.
"""
from speech.observation import AudioObservation
from speech.backends import FasterWhisperBackend


def observe(speech):
    """Acquire one auditory observation through the assigned Listen client.

    Request the established English, ten-second Listen operation. Its server
    saves independently timestamped audio and transcript evidence before returning
    recognition text. Propagate action errors and cancellation unchanged; do not
    synthesize a reply or write a second observation record.
    """
    from smpa_ros_interfaces.action import Listen
    return speech.perform(speech.listener, Listen.Goal(language='en', max_duration=10.0)).transcription
