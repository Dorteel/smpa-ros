"""Lightweight stand-ins for SMPA's ROS 2 participants, for the Dialogue backend tests. No inference runs here.

Concepts:
    Every type comes from SMPA's installed ``smpa_ros_interfaces`` package;
    nothing is redefined. The stand-ins answer on SMPA's real endpoint names.

    ``/smpa_control`` mimics SMPA's run control: ``/smpa/get_status``
    (GetSmpaStatus) reports the robot run given by ``--run-id`` as running,
    ``/smpa/start`` (StartSmpa) and ``/smpa/stop`` (StopSmpa) switch that
    state, and ``/smpa/events`` (SmpaEvent) announces a stage event twice a
    second.

    ``/llm`` mimics SMPA's inference server without a model: ``ListModels``
    returns ["stub-a", "stub-b"], ``GetModelStatus`` reports its state and
    active model, ``InferenceStatus`` is published latched, and ``LoadModel``
    loads (after half a second) any listed alias.

    The stage stand-ins serve SMPA's four cognitive actions
    ``/smpa/sense``, ``/smpa/model``, ``/smpa/plan`` and ``/smpa/act``
    (types Sense, Model, Plan, Act) and persist records in the run's
    ``state/cycle_000001/`` with the fields SMPA's stages write
    (``smpa_ros/sense.py``, ``model.py``, ``plan.py``, ``act.py``): Sense an
    observation with one PNG evidence file, Model a world-model snapshot
    ``world_model/iteration_001[_k].md``, Plan an action decision, Act an
    execution with feedback. They are TEST STAND-INS: their outputs are
    canned and say so. Like SMPA's nodes they abort a goal for an unknown
    or non-running run and record why. A test can script one failure by
    writing ``scenario.json`` ({"fail": "<stage>"}) into the memory root;
    for Act the failure is ``success: false`` with a failed record.
    ``/fixture_dialogue/calls`` (std_srvs Trigger) returns every received
    goal as JSON.

    Usage: ``python cognitive_nodes.py --memory DIR --run-id UUID``; prints
    ``COGNITIVE READY``.
"""
import argparse
from datetime import datetime
import json
import struct
import threading
import time
import zlib
from pathlib import Path

import rclpy
from rclpy.action import ActionServer
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from smpa_ros_interfaces.action import Act, LoadModel, Model, Plan, Sense
from smpa_ros_interfaces.msg import InferenceStatus, SmpaEvent
from smpa_ros_interfaces.srv import GetModelStatus, GetSmpaStatus, ListModels, StartSmpa, StopSmpa
from std_srvs.srv import Trigger


def png(colour):
    """Return a tiny one-colour 8×8 PNG image."""
    row = b'\x00' + bytes(colour) * 8
    data = zlib.compress(row * 8)
    chunk = lambda kind, body: struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 8, 8, 8, 2, 0, 0, 0)) + chunk(b'IDAT', data) + chunk(b'IEND', b'')


class Stand:
    """Create the stand-in nodes, keep the run state, and record every received stage goal."""

    def __init__(self, memory, run_id):
        """Create the run-control, inference and stage stand-in nodes and their endpoints."""
        self.memory = Path(memory)
        self.run_id = run_id
        self.state = 'running'
        self.lock = threading.Lock()
        self.calls = {stage: [] for stage in ('sense', 'model', 'plan', 'act', 'start', 'stop', 'load')}
        self.sequence = 0
        self.counts = {}
        self.llm_state, self.active_model = 'ready', 'stub-a'
        group = ReentrantCallbackGroup()
        self.control = rclpy.create_node('smpa_control')
        self.control.create_service(GetSmpaStatus, '/smpa/get_status', self.status, callback_group=group)
        self.control.create_service(StartSmpa, '/smpa/start', self.start, callback_group=group)
        self.control.create_service(StopSmpa, '/smpa/stop', self.stop, callback_group=group)
        self.events = self.control.create_publisher(SmpaEvent, '/smpa/events', 100)
        self.control.create_timer(0.5, lambda: self.events.publish(SmpaEvent(run_id=self.run_id, stage='model',
                                                                              event='stage_started')))
        self.llm = rclpy.create_node('llm')
        self.llm.create_service(ListModels, '/llm/list_models', self.list_models, callback_group=group)
        self.llm.create_service(GetModelStatus, '/llm/get_model_status', self.model_status, callback_group=group)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE)
        self.inference = self.llm.create_publisher(InferenceStatus, '/llm/inference_status', latched)
        self.llm.create_timer(0.5, lambda: self.inference.publish(InferenceStatus(state=self.llm_state,
                                                                                  active_model=self.active_model)))
        self.stages = rclpy.create_node('stages', namespace='/fixture_dialogue')
        self.servers = [
            ActionServer(self.llm, LoadModel, '/llm/load_model', self.load, callback_group=group),
            ActionServer(self.stages, Sense, '/smpa/sense', self.sense, callback_group=group),
            ActionServer(self.stages, Model, '/smpa/model', self.model, callback_group=group),
            ActionServer(self.stages, Plan, '/smpa/plan', self.plan, callback_group=group),
            ActionServer(self.stages, Act, '/smpa/act', self.act, callback_group=group)]
        self.stages.create_service(Trigger, '/fixture_dialogue/calls', self.report, callback_group=group)
        self.nodes = [self.control, self.llm, self.stages]

    # ----- run control and inference ------------------------------------------------------------

    def status(self, request, response):
        """Report the run state, the run id while one exists, and the stage being worked on."""
        response.state, response.run_id = self.state, self.run_id if self.state in ('running', 'stopping', 'completed') else ''
        response.active_stage = 'model' if self.state == 'running' else ''
        return response

    def start(self, request, response):
        """Accept a nonempty goal when no run is active; the robot run becomes running again."""
        self.note('start', dict(goal=request.goal))
        if request.goal.strip() and self.state != 'running':
            self.state = 'running'
            response.accepted, response.run_id = True, self.run_id
        return response

    def stop(self, request, response):
        """Accept a stop request for the running run, which then counts as completed."""
        self.note('stop', {})
        response.accepted = self.state == 'running'
        if response.accepted:
            self.state = 'completed'
        return response

    def list_models(self, request, response):
        """List two stub model aliases; nothing is loaded."""
        response.model_ids = ['stub-a', 'stub-b']
        return response

    def model_status(self, request, response):
        """Report the stand-in server's state and active model."""
        response.state, response.active_model = self.llm_state, self.active_model
        return response

    def load(self, handle):
        """Pretend to load a listed alias after half a second; an unknown alias fails with a reason."""
        alias = handle.request.model_id
        self.note('load', dict(model_id=alias))
        self.llm_state = 'loading'
        time.sleep(0.5)
        handle.succeed()
        if alias not in ('stub-a', 'stub-b'):
            self.llm_state = 'error'
            return LoadModel.Result(success=False, error_message=f'Unknown registered model: {alias}')
        self.llm_state, self.active_model = 'ready', alias
        return LoadModel.Result(success=True)

    # ----- stages -------------------------------------------------------------------------------

    def note(self, stage, goal):
        """Record one received goal."""
        with self.lock:
            self.calls[stage].append(goal)

    def run(self, run_id):
        """Return the run directory of a running run, or raise like SMPA's run lock and reservation do."""
        if run_id != self.run_id or self.state != 'running':
            raise RuntimeError('Stage calls require an active run')
        found = [path for path in (self.memory / 'runs').glob(f'*_{run_id}') if path.is_dir()]
        if len(found) != 1:
            raise ValueError('run_id must identify exactly one existing run')
        return found[0]

    def scripted_failure(self, stage):
        """Tell whether the test scripted a failure for this stage (scenario.json), consuming it."""
        scenario = self.memory / 'scenario.json'
        if scenario.exists() and json.loads(scenario.read_text()).get('fail') == stage:
            scenario.unlink()
            return True
        return False

    def reserve(self, stage, prompt, source):
        """Allocate the next record name and provenance in cycle 1, like SMPA's reserve_invocation."""
        with self.lock:
            self.sequence += 1
            self.counts[stage] = self.counts.get(stage, 0) + 1
            count = self.counts[stage]
            name = f'{stage}.json' if count == 1 else f'{stage}_{count:03d}.json'
            previous = getattr(self, 'previous', None)
            self.previous = f'state/cycle_000001/{name}'
            return name, dict(sequence=self.sequence, invocation=count, started_at=datetime.now().astimezone().isoformat(),
                              prompt_name=prompt, source_record=source or None, previous_record=previous)

    def write(self, directory, name, record):
        """Write one record into the run's first cycle."""
        target = directory / 'state' / 'cycle_000001' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        return str(target.relative_to(directory))

    def execute(self, handle, stage, work, result_type):
        """Run one stage goal: resolve the run, reserve a record, do the work, or record the failure and abort."""
        request = handle.request
        self.note(stage, {key: getattr(request, key) for key in request.get_fields_and_field_types()})
        try:
            directory = self.run(request.run_id)
            name, provenance = self.reserve(stage, request.prompt_name or 'default', getattr(request, 'source_record', ''))
        except Exception:  # noqa: BLE001 - SMPA aborts without a record when the run cannot be resolved
            handle.abort()
            return result_type.Result()
        try:
            if self.scripted_failure(stage):
                raise RuntimeError(f'Scripted {stage} failure (test stand-in)')
            result = work(directory, name, provenance, request)
        except Exception as error:  # noqa: BLE001 - recorded like SMPA's nodes, then aborted
            self.write(directory, name, dict(stage=stage, cycle=1, status='failed',
                                             error=f'{type(error).__name__}: {error}', **provenance))
            handle.abort()
            return result_type.Result()
        handle.succeed()
        return result

    def sense(self, handle):
        """Stand-in Sense: save one evidence image and record a canned observation."""
        def work(directory, name, provenance, request):
            """Write the evidence file and the Sense record."""
            evidence = directory / 'observations' / 'vision' / f'stand_in_{provenance["sequence"]:03d}_sense_output.png'
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_bytes(png((40, 160, 90)))
            observation = json.dumps(dict(vision='Stand-in observation: a green test card (no camera, no model).'))
            path = self.write(directory, name, dict(stage='sense', cycle=1, **provenance, output=observation,
                                                    status='generated',
                                                    source_observations=[str(evidence.relative_to(directory))]))
            return Sense.Result(observation=observation, record_path=path)
        return self.execute(handle, 'sense', work, Sense)

    def model(self, handle):
        """Stand-in Model: publish a canned world-model snapshot linked to its Sense record."""
        def work(directory, name, provenance, request):
            """Check the source record, write the snapshot exclusively and the Model record."""
            if request.source_record and not (directory / request.source_record).is_file():
                raise ValueError('Model source and previous snapshot must exist within the run')
            snapshot = directory / 'world_model' / f"iteration_001{name[len('model'):-len('.json')]}.md"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            text = f'# Stand-in world model {provenance["invocation"]}\n\nA green test card is visible. (test stand-in)\n'
            with snapshot.open('x') as output:
                output.write(text)
            path = self.write(directory, name, dict(stage='model', cycle=1, input=request.observation, **provenance,
                                                    source_observations=[request.source_record] if request.source_record else [],
                                                    previous_iteration=request.previous_snapshot or None,
                                                    iteration=str(snapshot.relative_to(directory)),
                                                    model_identifier='stand-in', output=text, status='generated'))
            return Model.Result(world_model=text, record_path=path, snapshot_path=str(snapshot.relative_to(directory)))
        return self.execute(handle, 'model', work, Model)

    def plan(self, handle):
        """Stand-in Plan: decide a canned Observe action with parameters."""
        def work(directory, name, provenance, request):
            """Write the Plan record and return the decision."""
            decision = dict(action='observe', target='green card', question='What colour is the card?')
            path = self.write(directory, name, dict(stage='plan', cycle=1, input=request.world_model, goal='Fixture goal',
                                                    model_identifier='stand-in', **provenance,
                                                    available_actions={'observe': {'label': 'Observe'}, 'turn_left': {'label': 'Turn left'}},
                                                    output=json.dumps(decision), status='generated', selected_action='observe',
                                                    parameters={key: value for key, value in decision.items() if key != 'action'}))
            return Plan.Result(action_json=json.dumps(decision), record_path=path)
        return self.execute(handle, 'plan', work, Plan)

    def act(self, handle):
        """Stand-in Act: record a canned execution with feedback; a scripted failure reports success false."""
        request = handle.request
        self.note('act', {key: getattr(request, key) for key in request.get_fields_and_field_types()})
        try:
            directory = self.run(request.run_id)
            name, provenance = self.reserve('act', request.prompt_name or 'default', request.source_record)
        except Exception:  # noqa: BLE001
            handle.abort()
            return Act.Result()
        decision = json.loads(request.action_json)
        failed = self.scripted_failure('act')
        record = dict(stage='act', cycle=1, requested_action=decision['action'], **provenance,
                      parameters={key: value for key, value in decision.items() if key != 'action'},
                      goal_accepted=not failed, result_status=6 if failed else 4, success=not failed,
                      feedback=[dict(state='executing (test stand-in)')],
                      result=None if failed else dict(note='stand-in execution, nothing moved'),
                      status='failed' if failed else 'succeeded')
        if failed:
            record['error'] = 'RuntimeError: Scripted act failure (test stand-in)'
        path = self.write(directory, name, record)
        handle.succeed()
        return Act.Result(success=not failed, record_path=path)

    def report(self, request, response):
        """Return every received goal as JSON."""
        with self.lock:
            response.message = json.dumps(self.calls)
        response.success = True
        return response


def main():
    """Run the stand-ins until interrupted; print ``COGNITIVE READY`` once every endpoint exists."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--memory', required=True)
    parser.add_argument('--run-id', required=True)
    options = parser.parse_args()
    rclpy.init()
    stand = Stand(options.memory, options.run_id)
    executor = MultiThreadedExecutor(num_threads=8)
    for node in stand.nodes:
        executor.add_node(node)
    print('COGNITIVE READY', flush=True)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    for server in stand.servers:
        server.destroy()
    for node in stand.nodes:
        node.destroy_node()


if __name__ == '__main__':
    main()
