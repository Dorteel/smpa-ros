"""Run the production worker with safe fixture stages for ROS lifecycle tests."""
import sys
import time
from pathlib import Path
import runner
from smpa_ros.run_control import run_worker

ORIGINAL_RUNNER = runner.Runner


def make_runner(arguments):
    """Bind ordinary Runner persistence to deterministic stages without ROS actions.

    Read only the temporary root from the control node's ordinary arguments.
    A request goal containing WAIT blocks in acquisition until SIGINT so the
    test can verify normal interruption and persisted cleanup.
    """
    root = Path(arguments[arguments.index('--world-model-root') + 1])

    def sense(directory, cycle):
        """Return fixed evidence or wait for the stop service's real SIGINT.

        Inspect the already persisted request goal solely for fixture behavior.
        Python's ordinary signal handling interrupts sleep without another loop.
        """
        if 'WAIT' in (directory / 'goal.md').read_text():
            time.sleep(60)
        return 'fixture observation'

    def model(prompt, **settings):
        """Return a fixture Markdown snapshot without inference or model loading.

        The production Model persistence path still writes its normal record.
        """
        return '# World Model\n\nFixture evidence.'

    def plan(prompt, **settings):
        """Select a fixture action whose execution callback never contacts ROS.

        The existing schema validation and Plan persistence remain active.
        """
        return '{"action":"look_left"}'

    def act(action, record):
        """Record a successful fixture result without executing physical movement.

        Only the injected callback is replaced; the real Act record is persisted.
        """
        record.update(goal_accepted=True, result_status=4, success=True)

    return ORIGINAL_RUNNER(root=root, sense=sense, model=model, plan=plan, act=act)


if __name__ == '__main__':
    runner.Runner = make_runner
    run_worker(int(sys.argv[1]), sys.argv[2], sys.argv[3:])
