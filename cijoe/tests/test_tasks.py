from pathlib import Path

import jinja2
import pytest
import yaml
from cijoe.core.resources import Task

TASKS = sorted((Path(__file__).resolve().parent.parent / "tasks").glob("*.yaml"))


# cijoe renders a 'run' step command by command, a line each unless it ends
# in a backslash, so a Jinja block must close in the command it opens in
@pytest.mark.parametrize("path", TASKS, ids=[path.stem for path in TASKS])
def test_task_commands_are_templates_on_their_own(path):
    task = yaml.safe_load(path.read_text())
    assert not Task.dict_normalize(task)

    env = jinja2.Environment()
    for step in task["steps"]:
        for command in step.get("with", {}).get("commands", []):
            env.parse(command)
