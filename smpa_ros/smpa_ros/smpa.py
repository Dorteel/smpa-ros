"""Edit this program to choose goals, prompts, repetitions, and stage order."""
from runner import Runner

GOAL = '# Goal\n\nExplore the environment to discover previously unobserved objects and relationships.\n'


def run(runner, cycles, *, goal=GOAL):
    for experiment in range(1):
        runner.start(goal=goal)
        for cycle in range(cycles):
            with runner.cycle():
                observation = runner.sense(prompt=None)
                world = runner.model(observation, prompt='default')
                action = runner.plan(world, prompt='default')
                runner.act(action, prompt='default')
            if runner.experiment_complete:
                break
        runner.finish()


def main(args=None):
    with Runner(args) as runner:
        run(runner, runner.limit)


def run_actions(memory, cycles, invoke, *, notify=None):
    """Execute the experiment's explicit Sense, Model, Plan and Act sequence.

    The experiment owns the active run and cycle boundaries. The supplied
    invocation function sends an action goal and returns its successful result;
    it does not decide stage order. Carry each stage's persisted source record
    to the next and carry the latest world-model snapshot into the next cycle.
    Notify the lifecycle observer around each request when one is supplied.

    Complete a cycle only after Act succeeds. If a request fails, report the
    failed stage, leave the cycle incomplete and propagate the error. Refinement
    can terminate the loop when its persisted state becomes complete. Run setup,
    inference ownership, and finalization remain the caller's responsibility.
    """
    import json
    from smpa_ros_interfaces.action import Sense, Model, Plan, Act

    def request(stage, action_type, goal):
        """Invoke the selected stage once and publish its actual outcome.

        Notify immediately before sending the request, report a failure if the
        invocation raises, and publish completion only after a successful result.
        The supplied action client is responsible for bounded waits and cleanup.
        """
        if notify is not None:
            notify(stage, 'stage_started')
        try:
            result = invoke(stage, action_type, goal)
            if stage == 'act' and not result.success:
                raise RuntimeError('Act did not complete successfully')
        except Exception:
            if notify is not None:
                notify(stage, 'stage_failed')
            raise
        if notify is not None:
            notify(stage, 'stage_completed')
        return result

    run_id = memory.metadata['run_id']
    previous_snapshot = ''
    for cycle in range(cycles):
        memory.begin_cycle()
        try:
            sensed = request('sense', Sense, Sense.Goal(run_id=run_id, capability=''))
            modeled = request('model', Model, Model.Goal(
                run_id=run_id, capability='', observation=sensed.observation,
                source_record=sensed.record_path, previous_snapshot=previous_snapshot))
            previous_snapshot = modeled.snapshot_path
            planned = request('plan', Plan, Plan.Goal(
                run_id=run_id, capability='', world_model=modeled.world_model,
                source_record=modeled.record_path))
            acted = request('act', Act, Act.Goal(
                run_id=run_id, capability='', action_json=planned.action_json,
                source_record=planned.record_path))
            memory.complete_cycle()
        finally:
            memory.in_cycle = False
        refinement = memory.directory / 'state' / 'refinement.json'
        if refinement.exists() and json.loads(refinement.read_text()).get('status') == 'completed':
            break
