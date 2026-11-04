"""The same command run N times, each told which run it is: shards of a dataset, seeds, a sweep.

Template placeholders:
  {index}       this run's number, 0 to count-1
  {count}       how many runs there are
  {in:NAME}     a file or folder every run gets (the script, its data), from `inputs`
  {out:result}  an empty folder this run writes its results into

Each run's folder lands in the results as run-0001, run-0002, ...
"""

from __future__ import annotations

from pathlib import Path

from .. import scripts

MAX_RUNS = 10000


def plan_runs(template: list[str], count: int, inputs: dict | None = None, name: str = "runs",
              workdir: str = "", packages: list | None = None, timeout: float | None = None) -> dict:
    if not isinstance(template, list) or not template or not all(isinstance(a, str) for a in template):
        raise ValueError("template must be the command as a list, e.g. [\"python\", \"{in:script}\", \"{index}\"]")
    if not isinstance(count, int) or not 1 <= count <= MAX_RUNS:
        raise ValueError(f"count must be a whole number from 1 to {MAX_RUNS}")
    wd = Path(workdir or f"./macmesh-out/{name}").expanduser().resolve()
    shared = {k: str(Path(v).expanduser().resolve()) for k, v in (inputs or {}).items()}
    tasks = []
    for i in range(count):
        argv = [a.replace("{index}", str(i)).replace("{count}", str(count)) for a in template]
        task = {"id": f"r{i + 1:05d}", "argv": argv, "inputs": dict(shared),
                "outputs": {"result": f"results/run-{i + 1:04d}"}, "output_dirs": ["result"],
                "cost": 1.0, "unit": "runs"}
        if timeout:
            task["timeout"] = float(timeout)
        tasks.append(task)
    return scripts.use_packages({"name": name, "workdir": str(wd), "tasks": tasks, "merge": []}, packages)
