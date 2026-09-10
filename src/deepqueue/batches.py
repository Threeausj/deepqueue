from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
import shlex
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from string import Formatter

from .models import AgentEfforts, AgentModels, JobSpec

MAX_EXPERIMENTS = 10000
BUILTINS = {"batch", "index", "key"}
FORMATTER = Formatter()


@dataclass
class BatchPlan:
    name: str
    digest: str
    experiments: dict[str, JobSpec]
    order: list[str]


def render(value, context):
    if isinstance(value, str):
        pieces = []
        for literal, field, format_spec, conversion in FORMATTER.parse(value):
            pieces.append(literal)
            if field is None:
                continue
            if field not in context or format_spec or conversion:
                raise ValueError(
                    f"Invalid matrix placeholder {{{field}}}; use an axis name, "
                    "{batch}, {index}, or {key} without format specifiers"
                )
            item = context[field]
            pieces.append(str(item).lower() if isinstance(item, bool) else str(item))
        return "".join(pieces)
    if isinstance(value, dict):
        return {key: render(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, context) for item in value]
    return value


def matrix_rows(manifest, template_count):
    if "matrix" not in manifest:
        return [{}]
    matrix = manifest["matrix"]
    if not isinstance(matrix, dict) or len(matrix) > 16:
        raise ValueError("matrix must be an object with at most 16 parameter axes")
    count = template_count
    for name, values in matrix.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
            or name in BUILTINS
        ):
            raise ValueError(f"Invalid or reserved matrix axis name: {name}")
        if not isinstance(values, list) or not values:
            raise ValueError(f"Matrix axis {name} must be a nonempty array")
        for value in values:
            if type(value) not in (str, int, float, bool):
                raise ValueError(f"Matrix axis {name} accepts strings, numbers, and booleans only")
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"Matrix axis {name} requires finite numbers")
            if isinstance(value, str) and "\x00" in value:
                raise ValueError(f"Matrix axis {name} cannot contain NUL")
        if len({json.dumps(value) for value in values}) != len(values):
            raise ValueError(f"Matrix axis {name} contains duplicate values")
        count *= len(values)
        if count > MAX_EXPERIMENTS:
            raise ValueError(f"Expanded batch exceeds {MAX_EXPERIMENTS} experiments")
    # Sorted axes keep {index} stable when JSON object keys are reordered.
    names = sorted(matrix)
    return [
        dict(zip(names, values, strict=True))
        for values in itertools.product(*(matrix[name] for name in names))
    ]


def compile_batch(manifest) -> BatchPlan:
    if not isinstance(manifest, dict):
        raise ValueError("Batch manifest must be a JSON object")
    if set(manifest) - {"name", "defaults", "experiments", "matrix"}:
        raise ValueError("Batch accepts name, defaults, experiments, and matrix only")
    name, templates = manifest.get("name"), manifest.get("experiments")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("Batch name must be a nonempty string of at most 120 characters")
    if not isinstance(templates, list) or not 1 <= len(templates) <= MAX_EXPERIMENTS:
        raise ValueError(f"Batch must contain 1 to {MAX_EXPERIMENTS} experiments")
    defaults = manifest.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("Batch defaults must be an object")
    allowed = set(JobSpec.model_fields) | {"argv"}
    if unknown := set(defaults) - allowed:
        raise ValueError("Unknown batch default fields: " + ", ".join(sorted(unknown)))
    for template in templates:
        if not isinstance(template, dict) or not isinstance(template.get("key"), str):
            raise ValueError("Each experiment must be an object with a string key")
        if unknown := set(template) - allowed - {"key"}:
            raise ValueError("Unknown experiment fields: " + ", ".join(sorted(unknown)))
    rows = matrix_rows(manifest, len(templates))
    specs = {}
    for index, parameters in enumerate(rows, start=1):
        for template in templates:
            context = {**parameters, "batch": name, "index": index}
            key = render(template["key"], context) if "matrix" in manifest else template["key"]
            if not key or len(key) > 64:
                raise ValueError("Expanded experiment keys must be 1 to 64 characters")
            if key in specs:
                raise ValueError(f"Experiment keys must be unique after expansion: {key}")
            context["key"] = key
            values = {**defaults, **template}
            values.pop("key")
            for field, model in (("agent_models", AgentModels), ("agent_efforts", AgentEfforts)):
                if field in defaults and field in template:
                    values[field] = {
                        **model.model_validate(defaults[field]).model_dump(exclude_none=True),
                        **model.model_validate(template[field]).model_dump(exclude_unset=True),
                    }
            # Either command form on the experiment replaces the inherited command form.
            if "argv" in template and "command" not in template:
                values.pop("command", None)
            elif "command" in template and "argv" not in template:
                values.pop("argv", None)
            if "argv" in values and "command" in values:
                raise ValueError(f"Experiment {key} must choose argv or command, not both")
            if "matrix" in manifest:
                values = {
                    field: value if field == "command" else render(value, context)
                    for field, value in values.items()
                }
                command = values.get("command")
                # Shell ${NAME} references are not matrix placeholders.
                misplaced = r"(?<![${])\{(?:" + "|".join(map(re.escape, context)) + r")\}(?!\})"
                if isinstance(command, str) and re.search(misplaced, command):
                    raise ValueError(
                        f"Experiment {key}: use argv or env for matrix parameters; "
                        "command is a literal shell script"
                    )
            if "argv" in values:
                argv = values.pop("argv")
                if (
                    not isinstance(argv, list)
                    or not argv
                    or not argv[0]
                    or any(not isinstance(arg, str) or "\x00" in arg for arg in argv)
                ):
                    raise ValueError(f"Experiment {key}: argv must be a nonempty array of strings")
                values["command"] = shlex.join(argv)
            declared = values.get("parameters", {})
            if not isinstance(declared, dict):
                raise ValueError(f"Experiment {key}: parameters must be an object")
            for axis, value in parameters.items():
                if axis in declared and (
                    type(declared[axis]) is not type(value) or declared[axis] != value
                ):
                    raise ValueError(
                        f"Experiment {key}: parameters conflict with matrix axis {axis}"
                    )
            values.update(
                {
                    "group": name,
                    "idempotency_key": f"batch:{name}:{key}",
                    "parameters": {**declared, **parameters},
                }
            )
            values.setdefault("title", key)
            try:
                specs[key] = JobSpec.model_validate(values)
            except ValueError as exc:
                raise ValueError(f"Experiment {key}: {exc}") from exc
    graph = {key: [dep for dep in spec.depends_on if dep in specs] for key, spec in specs.items()}
    try:
        order = list(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise ValueError("Experiment dependencies contain a cycle") from exc
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    return BatchPlan(name, digest, specs, order)
