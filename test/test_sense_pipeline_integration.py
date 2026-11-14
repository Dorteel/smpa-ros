"""Exercise the permanent all-modality launcher with real local inference."""
import gc
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from threading import Thread
from time import monotonic, sleep
import wave

from PIL import Image as PILImage
import pytest
import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image
from smpa_ros.conversation import exchange, open_conversation

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
def test_permanent_all_senses_pipeline(tmp_path):
    """Run the production launcher and native stages with controlled sensor devices.

    Persist a real conversation message, synthesize a prerecorded microphone
    fixture, and publish the repository test image on the configured camera topic
    in this test's isolated ROS domain. Only the microphone device is substituted:
    local VLM, Whisper, audio persistence, Read and all cognitive stages are real.
    Use the committed Observe-only profile so no physical goals are possible.

    Require a successful attributed Sense record, committed cursor, successful
    Model/Plan/Act and one cognitive model load. Capture startup and shutdown
    diagnostics and ensure every logged child PID exits. On failure interrupt
    only the test-owned launch group; always stop the camera and ROS context.
    Optional local weights and a prepared production workspace are prerequisites.
    """
    if not (ROOT / 'models/vlm/qwen3_vl_2b/config.json').is_file():
        pytest.skip('The configured local VLM is unavailable')
    from speech.backends import PiperBackend
    voice_path = ROOT / 'models/speech/piper/en_US-lessac-medium.onnx'
    if not voice_path.is_file() or not (ROOT / 'models/speech/faster-whisper-base/model.bin').is_file():
        pytest.skip('The existing registered speech weights are unavailable')
    artifacts = Path(os.environ.get('SMPA_ACCEPTANCE_OUTPUT', tmp_path))
    artifacts.mkdir(parents=True, exist_ok=True)
    source = open_conversation(artifacts / 'conversations')

    def acknowledge(history, **settings):
        """Keep conversation persistence real while avoiding a second chat model."""
        return 'Acknowledged for robot observation.'

    exchange(source, 'I think there is a red object; this is an unverified user claim.', acknowledge, {})
    voice = PiperBackend(voice_path)
    recording = artifacts / 'microphone.wav'
    with wave.open(str(recording), 'wb') as output:
        voice.voice.synthesize_wav('Please observe the visible objects.', output)
    del voice
    gc.collect()
    rclpy.init(args=[])
    camera = rclpy.create_node('sense_acceptance_camera')
    executor = SingleThreadedExecutor()
    executor.add_node(camera)
    photo = PILImage.open(ROOT / 'images/test.png').convert('RGB')
    message = Image(width=photo.width, height=photo.height, encoding='rgb8',
                    step=photo.width * 3, data=photo.tobytes())
    publisher = camera.create_publisher(Image, '/tiago/camera/color/image_raw', qos_profile_sensor_data)
    camera.create_timer(.5, lambda: publisher.publish(message))
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    environment = {key: value for key, value in os.environ.items()
                   if key not in ('AMENT_PREFIX_PATH', 'CMAKE_PREFIX_PATH', 'COLCON_PREFIX_PATH',
                                  'PYTHONPATH', 'LD_LIBRARY_PATH')}
    environment.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
                       SMPA_TEST_AUDIO=str(recording), PYTHONDONTWRITEBYTECODE='1')
    command = [str(ROOT / 'run_smpa.sh'), '--all-senses', str(source.directory),
               f'config:={ROOT}/config/sense_observe.yaml',
               f'python:={ROOT}/test/sense_audio_python.py',
               f'world_model_root:={artifacts}/robots', 'run_name:=sense_2b_acceptance',
               'max_cycles:=1', 'timeout:=180']
    (artifacts / 'command.json').write_text(json.dumps(command, indent=2))
    process = None
    log = artifacts / 'production-launch.log'
    started = monotonic()
    peak_rss = {}
    try:
        with log.open('w') as output:
            process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=output,
                                       stderr=output, start_new_session=True)
            deadline = monotonic() + 360
            while process.poll() is None and monotonic() < deadline:
                for pid in re.findall(r'process started with pid \[(\d+)\]', log.read_text()):
                    try:
                        status = Path(f'/proc/{pid}/status').read_text()
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    found = re.search(r'^VmRSS:\s+(\d+)', status, re.MULTILINE)
                    if found:
                        peak_rss[pid] = max(peak_rss.get(pid, 0), int(found.group(1)))
                sleep(.2)
            code = process.wait(timeout=1)
        text = log.read_text()
        assert code == 0, text[-12000:]
        runs = list((artifacts / 'robots/runs').iterdir())
        directory = max(runs, key=lambda path: path.stat().st_mtime_ns)
        records = {stage: json.loads((directory / f'state/cycle_000001/{stage}.json').read_text())
                   for stage in ('sense', 'model', 'plan', 'act')}
        observed = json.loads(records['sense']['output'])
        assert {'vision', 'speech', 'read'} <= set(observed)
        assert observed['speech'].strip() and len(observed['read']['observations']) == 1
        assert records['sense']['read_cursor']['message_index'] == 1
        assert records['plan']['selected_action'] == 'observe'
        assert records['act']['status'] == 'succeeded'
        assert text.count('Loading vision model on ') == 1
        assert 'Using supplied backend: TransformersVLM' in text
        assert 'SIGTERM' not in text and 'InvalidHandle' not in text and 'Traceback' not in text
        child_pids = [int(pid) for pid in re.findall(r'process started with pid \[(\d+)\]', text)]
        assert len(child_pids) >= 3
        assert not [pid for pid in child_pids if Path(f'/proc/{pid}').exists()]
        from sense.read import observe
        assert observe(directory, source.directory)['observations'] == []
        result = dict(run=str(directory), elapsed_seconds=monotonic()-started,
                      modalities=list(observed), vlm_loads=1, child_pids=child_pids, peak_rss_kib=peak_rss,
                      selected_action='observe', audio_device='prerecorded fixture',
                      camera='controlled image publisher in isolated test domain')
        (artifacts / 'result.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result))
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
        executor.shutdown()
        thread.join(timeout=5)
        camera.destroy_node()
        rclpy.shutdown()
        source.finalize('completed')
