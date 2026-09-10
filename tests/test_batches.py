import copy
import hashlib
import json
import shlex
import subprocess
import sys
import time

import pytest

from deepqueue.batches import compile_batch
from deepqueue.models import JobSpec
from deepqueue.transport import Transport


def manifest():
    return {
        "name": "sweep",
        "defaults": {"server": "local", "cwd": "/tmp/project"},
        "matrix": {"lr": [0.01, 0.1], "seed": [7, 11]},
        "experiments": [
            {
                "key": "eval-{index}",
                "argv": ["python", "eval.py", "runs/{batch}/train-{index}"],
                "depends_on": ["train-{index}"],
            },
            {
                "key": "train-{index}",
                "argv": [
                    "python",
                    "train.py",
                    "--lr",
                    "{lr}",
                    "--seed",
                    "{seed}",
                    "--output",
                    "runs/{batch}/{key}",
                ],
                "artifact_files": ["runs/{batch}/{key}/metrics.json"],
            },
        ],
    }


def test_matrix_expands_each_template_with_stable_parameters_and_dependencies():
    source = manifest()
    unchanged = copy.deepcopy(source)
    plan = compile_batch(source)
    assert source == unchanged
    assert len(plan.experiments) == 8
    for index, (lr, seed) in enumerate([(0.01, 7), (0.01, 11), (0.1, 7), (0.1, 11)], start=1):
        train, evaluate = plan.experiments[f"train-{index}"], plan.experiments[f"eval-{index}"]
        assert train.parameters == {"lr": lr, "seed": seed}
        assert shlex.split(train.command) == [
            "python",
            "train.py",
            "--lr",
            str(lr),
            "--seed",
            str(seed),
            "--output",
            f"runs/sweep/train-{index}",
        ]
        assert train.artifact_files == [f"runs/sweep/train-{index}/metrics.json"]
        assert evaluate.depends_on == [f"train-{index}"]
        assert plan.order.index(f"train-{index}") < plan.order.index(f"eval-{index}")
    source["matrix"] = {"seed": [7, 11], "lr": [0.01, 0.1]}
    reordered = compile_batch(source)
    assert reordered.digest == plan.digest
    assert reordered.experiments == plan.experiments


def test_preview_is_read_only_and_submission_preserves_its_plan(db):
    source = manifest()
    preview = db.preview_batch(source)
    assert preview["experiment_count"] == 8
    assert preview["estimation_count"] == 8
    assert not preview["existing"]
    assert not db.jobs() and not db.batches()
    with db.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    result = db.submit_batch(source)
    for experiment in preview["experiments"]:
        key = experiment["key"]
        submitted = db.job(result["jobs"][key])
        assert submitted["spec"]["command"] == experiment["command"]
        assert submitted["spec"]["parameters"] == experiment["parameters"]
        assert submitted["spec"]["depends_on"] == [
            result["jobs"][dep] for dep in experiment["depends_on"]
        ]
    assert db.preview_batch(source)["existing"]
    assert db.submit_batch(source)["existing"]
    assert len(db.jobs()) == 8


def test_matrix_argv_values_are_literal_even_for_shell_metacharacters(db):
    value = "$(touch should-not-exist); 'quotes' & spaces\nand newlines"
    source = {
        "name": "quoted",
        "defaults": {
            "server": "local",
            "cwd": str(db.home),
            "resources": {"ram_mib": 128},
            "skip_estimate": True,
            "archive": False,
        },
        "matrix": {"value": [value]},
        "experiments": [
            {
                "key": "job-{index}",
                "argv": [sys.executable, "-c", "import sys; print(sys.argv[1])", "{value}"],
            }
        ],
    }
    result = db.submit_batch(source)
    job = db.job(result["jobs"]["job-1"])
    from deepqueue.models import Server

    worker = Transport(db.home, Server.model_validate(db.server("local")["config"]))
    run_id = db.reserve(job["id"], [])
    worker.call(
        "launch",
        run_id=run_id,
        spec={
            "cwd": str(db.home),
            "command": job["spec"]["command"],
            "env": {},
            "gpu_uuids": [],
            "timeout_seconds": 5,
        },
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = worker.call("inspect", run_id=run_id)
        if state["status"] == "succeeded":
            break
        time.sleep(0.1)
    assert state["status"] == "succeeded"
    assert worker.call("logs", run_id=run_id)["text"] == value + "\n"
    assert not (db.home / "should-not-exist").exists()
    db.finish(job["id"], state)


@pytest.mark.parametrize(
    "matrix,match",
    [
        (None, "object"),
        ({"lr": []}, "nonempty"),
        ({"lr": [0.1, 0.1]}, "duplicate"),
        ({"index": [1]}, "reserved"),
        ({"lr.value": [0.1]}, "axis name"),
        ({"lr": [[1, 2]]}, "strings, numbers"),
        ({"lr": [None]}, "strings, numbers"),
        ({"lr": [float("nan")]}, "finite"),
        ({"lr": [float("inf")]}, "finite"),
        ({"lr": ["nul\x00value"]}, "NUL"),
    ],
)
def test_invalid_matrix_rejected_without_partial_submission(db, matrix, match):
    source = manifest()
    source["matrix"] = matrix
    with pytest.raises(ValueError, match=match):
        db.submit_batch(source)
    assert not db.jobs() and not db.batches()


def test_expansion_limit_checked_before_materializing_combinations(db):
    source = manifest()
    source["matrix"] = {"lr": list(range(101)), "seed": list(range(100))}
    with pytest.raises(ValueError, match="exceeds 10000"):
        db.preview_batch(source)
    assert not db.jobs()


@pytest.mark.parametrize(
    "key,argv,match",
    [
        ("constant", ["true"], "unique"),
        ("{unknown}", ["true"], "placeholder"),
        ("job-{index}", ["python", "{lr.real}"], "placeholder"),
        ("job-{index}", ["python", "{lr!r}"], "placeholder"),
        ("job-{index}", ["python", "{seed:02d}"], "placeholder"),
        ("job-{index}", [], "argv"),
        ("job-{index}", [""], "argv"),
        ("job-{index}", ["python", 3], "argv"),
    ],
)
def test_template_errors_are_reported_before_insertion(db, key, argv, match):
    source = manifest()
    source["experiments"] = [{"key": key, "argv": argv}]
    with pytest.raises(ValueError, match=match):
        db.submit_batch(source)
    assert not db.jobs()


def test_literal_commands_and_legacy_braces_are_preserved():
    literal = "python -c \"print({'loss': 0.5})\""
    source = {
        "name": "literal",
        "defaults": {"server": "local", "cwd": "/tmp"},
        "experiments": [{"key": "a", "command": literal}],
    }
    assert compile_batch(source).experiments["a"].command == literal
    source["matrix"] = {"lr": [0.01]}
    source["experiments"][0]["env"] = {"LR": "{lr}"}
    spec = compile_batch(source).experiments["a"]
    assert spec.command == literal and spec.env == {"LR": "0.01"}
    source["experiments"][0]["command"] = 'printf "%s" "${key}"'
    assert compile_batch(source).experiments["a"].command == 'printf "%s" "${key}"'
    source["experiments"][0]["command"] = "python train.py --lr {lr}"
    with pytest.raises(ValueError, match="literal shell script"):
        compile_batch(source)


def test_resources_and_argv_override_inherited_shell_command():
    source = manifest()
    source["defaults"]["command"] = "true"
    source["matrix"] = {"gpus": [1, 2]}
    source["experiments"] = [
        {
            "key": "train-{index}",
            "argv": ["python", "train.py"],
            "resources": {"gpu_count": "{gpus}", "gpu_memory_mib": 20000},
        }
    ]
    plan = compile_batch(source)
    assert [spec.resources.gpu_count for spec in plan.experiments.values()] == [1, 2]
    assert all(spec.command == "python train.py" for spec in plan.experiments.values())
    source["experiments"][0]["command"] = "false"
    with pytest.raises(ValueError, match="choose argv or command"):
        compile_batch(source)


def test_argv_without_matrix_keeps_braces_and_empty_arguments_literal():
    tokens = ["python", "-c", "print({'value': 3})", "", "{not-a-placeholder}"]
    source = {
        "name": "literal-argv",
        "defaults": {"server": "local", "cwd": "/tmp"},
        "experiments": [{"key": "a", "argv": tokens}],
    }
    assert shlex.split(compile_batch(source).experiments["a"].command) == tokens


@pytest.mark.parametrize("value", [[], None, "not-an-object"])
def test_non_object_manifest_rejected(db, value):
    with pytest.raises(ValueError, match="JSON object"):
        db.preview_batch(value)
    assert not db.jobs()


def test_metadata_conflicts_cannot_mislabel_a_matrix_run():
    source = manifest()
    source["defaults"]["parameters"] = {"lr": 0.5}
    with pytest.raises(ValueError, match="conflict"):
        compile_batch(source)


def test_external_dependencies_and_preview_validation(db):
    existing = db.submit(JobSpec(server="local", cwd="/tmp", command="true"))
    source = manifest()
    source["experiments"][1]["depends_on"] = [existing["id"]]
    db.preview_batch(source)
    result = db.submit_batch(source)
    assert db.job(result["jobs"]["train-1"])["spec"]["depends_on"] == [existing["id"]]
    source["name"] = "missing-dependency"
    source["experiments"][1]["depends_on"] = ["absent-job"]
    with pytest.raises(ValueError, match="Unknown dependency"):
        db.preview_batch(source)
    source["experiments"][1]["depends_on"] = ["eval-{index}"]
    with pytest.raises(ValueError, match="cycle"):
        db.preview_batch(source)


def test_batch_idempotency_collision_does_not_create_dangling_ids(db):
    source = {
        "name": "reserved",
        "defaults": {"server": "local", "cwd": "/tmp"},
        "experiments": [{"key": "a", "command": "true"}],
    }
    existing = db.submit(compile_batch(source).experiments["a"])
    for method in (db.preview_batch, db.submit_batch):
        with pytest.raises(ValueError, match="already belongs"):
            method(source)
    assert not db.batches()
    assert [job["id"] for job in db.jobs()] == [existing["id"]]


def test_legacy_job_idempotency_fingerprint_still_matches(db):
    spec = JobSpec(server="local", cwd="/tmp", command="true", idempotency_key="legacy")
    old = spec.model_dump()
    old.pop("parameters")
    old.pop("source_thread_id")
    for key in (
        "completion_mode",
        "max_improvement_rounds",
        "improvement_round",
        "parent_job_id",
        "baseline_job_id",
        "launch_max_retries",
        "launch_agent",
        "tmux",
        "agent_models",
        "agent_efforts",
    ):
        old.pop(key)
    fingerprint = hashlib.sha256(
        json.dumps(old, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    job = db.submit(spec)
    with db.connection(write=True) as con:
        con.execute(
            "UPDATE jobs SET spec=?,spec_hash=? WHERE id=?",
            (json.dumps(old), fingerprint, job["id"]),
        )
    assert db.submit(JobSpec.model_validate(old))["id"] == job["id"]


def test_cli_preview_accepts_stdin_and_reports_parameters(db):
    command = [sys.executable, "-m", "deepqueue", "--home", str(db.home), "batch", "preview", "-"]
    result = subprocess.run(
        command, input=json.dumps(manifest()), capture_output=True, text=True, check=True
    )
    preview = json.loads(result.stdout)
    assert preview["experiment_count"] == 8
    assert preview["experiments"][0]["parameters"] == {"lr": 0.01, "seed": 7}
    assert not db.jobs()


def test_matrix_report_preserves_parameters_after_submission(db):
    db.submit_batch(manifest())
    result = subprocess.run(
        [sys.executable, "-m", "deepqueue", "--home", str(db.home), "batch", "report", "sweep"],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(result.stdout)
    assert report["counts"] == {"pending": 8}
    assert report["experiments"][0]["parameters"] == {"lr": 0.01, "seed": 7}
