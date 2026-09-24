from pathlib import Path

import yaml

RESOURCES = Path(__file__).resolve().parents[1] / "resources"


def jobs():
    for path in sorted(RESOURCES.glob("*.yml")):
        yield from (yaml.safe_load(path.read_text()) or {}).get("resources", {}).get("jobs", {}).items()


def test_every_job_is_manual_standard_single_run_bounded_and_not_retried():
    found = dict(jobs())
    assert "osha_answer_eval" in found and len(found) >= 10
    for name, job in found.items():
        assert job["max_concurrent_runs"] == 1 and job["performance_target"] == "STANDARD", name
        assert job.get("timeout_seconds", 0) > 0, name
        assert not {"schedule", "trigger", "continuous"} & set(job), name
        for task in job["tasks"]:
            assert task["max_retries"] == 0, (name, task["task_key"])
