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
        runner.finish()


def main(args=None):
    with Runner(args) as runner:
        run(runner, runner.limit)
