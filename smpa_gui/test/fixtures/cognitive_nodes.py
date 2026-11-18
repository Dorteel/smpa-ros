"""Lightweight stand-ins for SMPA's ROS 2 nodes, for the Dialogue backend tests. No inference runs here.

Concepts:
    Every message type comes from the installed ``smpa_ros_interfaces``
    package (SMPA's real interfaces); nothing is redefined.

    ``/smpa_control`` mimics SMPA's run control: ``/smpa/get_status``
    (GetSmpaStatus) reports a running run with the id given by ``--run-id``,
    and ``/smpa/events`` (SmpaEvent) announces a stage event twice a second.

    ``/llm`` mimics SMPA's inference server without a model:
    ``/llm/list_models`` returns ["stub-a", "stub-b"], ``/llm/get_model_status``
    returns ready with stub-a, ``/llm/inference_status`` is published once,
    latched, and ``/llm/generate`` (GenerateText) answers "stub: <prompt>".
    Like the real server it rejects a goal while another runs. A prompt
    containing "slow" takes two seconds, "fail" aborts the goal.

    ``/fixture_dialogue/*`` are TEST STAND-INS for the four cognitive stages.
    The real stage interfaces are still being defined by Codex; these use
    existing types only so the Dialogue coordinator's ROS calls can be
    tested. They are not a proposed contract.
        sense  GenerateText:       prompt is the user message; returns "obs-<n>".
                                   A message containing "slow" takes two seconds.
        model  GenerateStructured: prompt is "<belief context>|<observation>";
                                   writes belief-state.ttl into that run of the
                                   memory directory (``--memory``), then returns
                                   "inv-<n>". A message containing "fail-model"
                                   aborts without writing.
        plan   GenerateText:       returns "reply to: <message>". A message
                                   containing "fail-plan" aborts the first time.
        act    Talk:               succeeds; a response containing "fail-act"
                                   reports success false with a reason.
    ``/fixture_dialogue/calls`` (std_srvs Trigger) returns, as JSON, every
    goal each stand-in received, so tests can check what was sent.

    Usage: ``python cognitive_nodes.py --memory DIR [--run-id ID] [--no-control]``;
    prints ``COGNITIVE READY``.
"""
import argparse
import json
import threading
import time
from pathlib import Path

import rclpy
from rclpy.action import ActionServer, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from smpa_ros_interfaces.action import GenerateStructured, GenerateText, Talk
from smpa_ros_interfaces.msg import InferenceStatus, SmpaEvent
from smpa_ros_interfaces.srv import GetModelStatus, GetSmpaStatus, ListModels
from std_srvs.srv import Trigger


class Stand:
    """Create the stand-in nodes and record every goal the stage stand-ins receive."""

    def __init__(self, memory, run_id, control):
        """Create the run-control, inference and stage stand-in nodes and their endpoints."""
        self.memory = Path(memory)
        self.run_id = run_id
        self.lock = threading.Lock()
        self.calls = {stage: [] for stage in ('sense', 'model', 'plan', 'act')}
        self.busy = False
        self.plan_failed_once = set()
        group = ReentrantCallbackGroup()
        self.nodes = []
        if control:
            self.control = rclpy.create_node('smpa_control')
            self.control.create_service(GetSmpaStatus, '/smpa/get_status', self.status, callback_group=group)
            self.events = self.control.create_publisher(SmpaEvent, '/smpa/events', 100)
            self.control.create_timer(0.5, lambda: self.events.publish(
                SmpaEvent(run_id=self.run_id, stage='model', event='stage_started')))
            self.nodes.append(self.control)
        self.llm = rclpy.create_node('llm')
        self.llm.create_service(ListModels, '/llm/list_models', self.list_models, callback_group=group)
        self.llm.create_service(GetModelStatus, '/llm/get_model_status', self.model_status, callback_group=group)
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL, reliability=ReliabilityPolicy.RELIABLE)
        self.inference = self.llm.create_publisher(InferenceStatus, '/llm/inference_status', latched)
        self.inference.publish(InferenceStatus(state='ready', active_model='stub-a'))
        self.llm.create_timer(0.5, lambda: self.inference.publish(InferenceStatus(state='ready', active_model='stub-a')))
        self.generate_server = ActionServer(self.llm, GenerateText, '/llm/generate', self.generate,
                                            goal_callback=self.reserve, callback_group=group)
        self.stages = rclpy.create_node('stages', namespace='/fixture_dialogue')
        self.servers = [
            ActionServer(self.stages, GenerateText, '/fixture_dialogue/sense', self.sense, callback_group=group),
            ActionServer(self.stages, GenerateStructured, '/fixture_dialogue/model', self.model, callback_group=group),
            ActionServer(self.stages, GenerateText, '/fixture_dialogue/plan', self.plan, callback_group=group),
            ActionServer(self.stages, Talk, '/fixture_dialogue/act', self.act, callback_group=group)]
        self.stages.create_service(Trigger, '/fixture_dialogue/calls', self.report, callback_group=group)
        self.nodes += [self.llm, self.stages]

    def status(self, request, response):
        """Report a running robot run with the configured run id."""
        response.state, response.run_id, response.active_stage = 'running', self.run_id, 'model'
        return response

    def list_models(self, request, response):
        """List two stub model aliases; nothing is loaded."""
        response.model_ids = ['stub-a', 'stub-b']
        return response

    def model_status(self, request, response):
        """Report a ready server with stub-a active."""
        response.state, response.active_model = 'ready', 'stub-a'
        return response

    def reserve(self, goal):
        """Accept one generation at a time and reject overlapping goals, as SMPA's server does."""
        with self.lock:
            if self.busy:
                return GoalResponse.REJECT
            self.busy = True
            return GoalResponse.ACCEPT

    def generate(self, handle):
        """Answer "stub: <prompt>", slowly for "slow", aborting for "fail"; always free the slot."""
        try:
            prompt = handle.request.prompt
            if 'slow' in prompt:
                time.sleep(2.0)
            if 'fail' in prompt:
                handle.abort()
                return GenerateText.Result()
            handle.publish_feedback(GenerateText.Feedback(partial_text='stub'))
            handle.succeed()
            return GenerateText.Result(text='stub: ' + prompt)
        finally:
            with self.lock:
                self.busy = False

    def note(self, stage, goal):
        """Record one received goal and return how many this stage has received."""
        with self.lock:
            self.calls[stage].append(goal)
            return len(self.calls[stage])

    def sense(self, handle):
        """Stand-in Sense: return an observation id for the message."""
        message = handle.request.prompt
        number = self.note('sense', dict(prompt=message))
        if 'slow' in message:
            time.sleep(2.0)
        handle.succeed()
        return GenerateText.Result(text=f'obs-{number}')

    def model(self, handle):
        """Stand-in Model: commit a belief file for the belief context, then return an invocation id."""
        context, _, observation = handle.request.prompt.partition('|')
        number = self.note('model', dict(prompt=handle.request.prompt, json_schema=handle.request.json_schema))
        message = handle.request.json_schema
        if 'fail-model' in message:
            handle.abort()
            return GenerateStructured.Result()
        runs = [path for path in (self.memory / 'runs').glob(f'*{context}') if path.is_dir()]
        if len(runs) != 1:
            handle.abort()
            return GenerateStructured.Result()
        belief = runs[0] / 'belief-state.ttl'
        lines = belief.read_text(encoding='utf-8').splitlines() if belief.exists() else ['@prefix ex: <http://example.org/> .']
        lines.append(f'ex:{observation.replace("-", "_")} ex:seenIn ex:turn_{number} .')
        temporary = belief.with_suffix('.tmp')
        temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        temporary.replace(belief)
        handle.succeed()
        return GenerateStructured.Result(json_output=f'inv-{number}')

    def plan(self, handle):
        """Stand-in Plan: reply to the message, failing once for "fail-plan"."""
        message = handle.request.prompt
        self.note('plan', dict(prompt=message))
        if 'fail-plan' in message and message not in self.plan_failed_once:
            self.plan_failed_once.add(message)
            handle.abort()
            return GenerateText.Result()
        handle.succeed()
        return GenerateText.Result(text=f'reply to: {message}')

    def act(self, handle):
        """Stand-in Act: deliver the response, or report that delivery failed for "fail-act"."""
        text = handle.request.text
        self.note('act', dict(text=text))
        handle.publish_feedback(Talk.Feedback(state='speaking', progress=0.5))
        handle.succeed()
        if 'fail-act' in text:
            return Talk.Result(success=False, error_message='speaker unplugged')
        return Talk.Result(success=True)

    def report(self, request, response):
        """Return every received stage goal as JSON."""
        with self.lock:
            response.message = json.dumps(self.calls)
        response.success = True
        return response


def main():
    """Run the stand-ins until interrupted; print ``COGNITIVE READY`` once every endpoint exists."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--memory', required=True)
    parser.add_argument('--run-id', default='robot-run')
    parser.add_argument('--no-control', action='store_true')
    options = parser.parse_args()
    rclpy.init()
    stand = Stand(options.memory, options.run_id, not options.no_control)
    executor = MultiThreadedExecutor(num_threads=6)
    for node in stand.nodes:
        executor.add_node(node)
    print('COGNITIVE READY', flush=True)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    for server in stand.servers + [stand.generate_server]:
        server.destroy()
    for node in stand.nodes:
        node.destroy_node()


if __name__ == '__main__':
    main()
