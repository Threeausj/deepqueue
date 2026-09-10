# CLI Reference

```bash
deepqueue init
deepqueue doctor
deepqueue models
deepqueue server list
deepqueue server probe local
deepqueue daemon start
deepqueue daemon status
deepqueue batch preview /absolute/path/experiments.json --from-agent
deepqueue batch submit /absolute/path/experiments.json --from-agent
deepqueue batch show round-01
deepqueue batch report round-01
deepqueue job show JOB_ID
deepqueue job logs JOB_ID --raw
deepqueue job links JOB_ID
deepqueue job tmux JOB_ID
deepqueue job mode JOB_ID finish
deepqueue job submit --file /absolute/path/job.json --from-agent --completion-mode improve --max-improvement-rounds 1
deepqueue job submit --file /absolute/path/child.json --from-agent --parent-job-id PARENT_ID
```

Batch format:

```json
{
  "name": "round-01",
  "defaults": {
    "server": "gpu-server",
    "cwd": "/srv/project",
    "context_files": ["train.py", "config.json"],
    "intent": "Compare batch sizes with the same fixed validation split"
  },
  "experiments": [
    {
      "key": "batch-16",
      "command": "python train.py --batch-size 16 --output runs/round-01-b16",
      "artifact_files": ["runs/round-01-b16/metrics.json"]
    },
    {
      "key": "batch-32",
      "command": "python train.py --batch-size 32 --output runs/round-01-b32",
      "artifact_files": ["runs/round-01-b32/metrics.json"]
    },
    {
      "key": "evaluate-16",
      "command": "python evaluate.py --checkpoint runs/round-01-b16/best.pth",
      "depends_on": ["batch-16"]
    }
  ]
}
```

`deepqueue schema` prints the single-job schema. A single-job JSON accepts:
`server`, `command`, `cwd`, `title`, `group`, `intent`, `priority` (-100 to 100),
`env`, `context_files`, `artifact_files`, `resources`, `skip_estimate`, `archive`,
`timeout_seconds` (0 means no time limit), `depends_on`, `parameters`, and `idempotency_key`.
It also accepts `source_thread_id`, the real Codex conversation to resume after
completion. Prefer `--from-agent` on job submit and batch preview/submit to bind
the current `CODEX_THREAD_ID`. Outside the agent environment, explicitly provide
`--source-thread-id ACTUAL_ID`. Conflicting IDs are rejected. Source binding
requires `archive: true` and is immutable after submission. CLI invocations without
these options and web submissions never inherit a process's conversation ID.
Batch defaults are overridden by each experiment's fields; arrays and nested
objects are replaced, not recursively merged, except `agent_models` and
`agent_efforts`, which merge
by stage. A null stage resets it to the queue default.

Global defaults are managed under the dashboard's General Settings. Normal
submissions omit model and effort overrides and use those defaults at agent start.
CLI equivalents:

```bash
deepqueue config set agent_models '{"estimate":"gpt-5.6-luna","launch":"gpt-5.6-luna","archive":"source-thread"}'
deepqueue config set agent_efforts '{"estimate":"low","launch":"medium","archive":"source-thread"}'
```

The daemon reloads `model`, `agent_models`, `agent_effort` and `agent_efforts`
before the next scheduling cycle. Already admitted agents retain their settings.
Other configuration changes, including app-server connections, require a restart.
`deepqueue models` includes each model's supported `efforts` and `default_effort`.
Per-job `agent_efforts` uses the same three phase keys as models; submission flags
are `--estimate-effort`, `--launch-effort`, and `--archive-effort`. Values are
provider-advertised effort IDs, such as low, medium, high, xhigh or max.
Archive effort `source-thread` follows the source conversation's current effort.
Model and effort inheritance are independent. Explicit archive effort changes
apply to that and subsequent source turns. Agent records retain the resolved
`effort`; null on older records means it was not recorded.

Per-stage Codex model overrides:

```json
{"agent_models":{"estimate":"gpt-5.6-luna","launch":"gpt-5.6-luna","archive":"source-thread"}}
```

The keys are `estimate`, `launch`, and `archive` (summary and improvement share a
source turn). `source-thread` is only valid for archive and requires a real source
conversation. It resolves the current conversation model when returning, without
overriding it. A concrete archive model ID explicitly changes that conversation's
model, including subsequent turns. New estimate/launch agents use concrete IDs.
Submission and batch preview/submit accept `--estimate-model MODEL`,
`--launch-model MODEL`, and `--archive-model MODEL`; supplied flags override that
stage on every experiment while preserving other stages. `deepqueue models` lists
choices from the configured app-server connections without starting model inference.
Custom provider IDs are accepted; unavailable models remain visible agent failures.

Set queue defaults with:

```bash
deepqueue config set agent_models '{"estimate":"gpt-5.6-luna","launch":"gpt-5.6-luna","archive":"source-thread"}'
```

Precedence: job stage selection, queue stage default, then `config.model` for new
agents or the current source model for bound archives. Omitted/null stages inherit
the default. An unbound legacy archive uses `config.model` when its queue default
is `source-thread`. Agent defaults apply on the next scheduling cycle. Improvement
children inherit their parent's stage selections; explicit child stages override
them, and null resets a child stage to the queue default. The archive agent record
stores the selected model resolved at dispatch, including the current source model.

Completion fields: `completion_mode` (`finish` default or `improve`),
`max_improvement_rounds` (1 default, 1-100), `parent_job_id` (completed parent for
an authorized improvement). `improvement_round` is assigned by the queue, never by
the agent. Submission CLI flags override corresponding JSON fields on every job.
Children inherit server, origin and round limit, use priority 100, and have one
idempotent slot per parent. The final allowed child uses finish mode. Improve mode
requires a source conversation. `job mode ID finish` stops further continuation.

### Adjust Job Stages

```bash
deepqueue job mode JOB_ID improve --max-improvement-rounds 2
deepqueue job update JOB_ID --archive-model source-thread --archive-effort high
deepqueue job update JOB_ID --intent 'Compare best/final validation accuracy against the baseline'
deepqueue job update JOB_ID --baseline-job-id BASELINE_ID
deepqueue job update JOB_ID --launch-max-retries 3
deepqueue job update JOB_ID --command 'python train.py --batch-size 8' --resources /absolute/resources.json
deepqueue job update JOB_ID --file /absolute/job-patch.json
deepqueue job mode JOB_ID finish
```

The local CLI and server-bound remote CLI use the same transactional controls.
Remote clients keep their configured public URL and execution server. HTTP clients
can POST a partial settings object to `/api/jobs/JOB_ID/settings`; the existing
`/control` mode action also accepts `max_improvement_rounds`.

Patch fields: `completion_mode`, `max_improvement_rounds`, `agent_models`,
`agent_efforts`, `launch_max_retries`, `baseline_job_id`, `intent`, `command`,
`resources`, `context_files`, `artifact_files`, `parameters`. Omitted fields remain
unchanged. Stage maps merge by stage; a stage set to null restores global inheritance.
Lists, parameters and the resource object replace their previous values (resource
defaults fill omitted resource fields). Top-level null is allowed only for
`baseline_job_id` (automatic comparison) and `launch_max_retries` (global limit).
CLI flags override corresponding patch-file fields. Shell commands remain literal.

Command/resources/context/parameters can change only in `pending`, `queued` or
legacy `needs_review`, before an estimate or execution is admitted. These changes
discard the previous estimate and run preflight again, except for jobs already
using `skip_estimate`. Existing run records and submission idempotency fingerprints
remain intact. Models/efforts and retry budgets affect future calls/attempts.
Artifact lists, baseline selection and intent cannot change during an active archive.
An explicit baseline must be an executed job on the same server.

Mode and round-limit changes apply while waiting/running or after execution exits
before archive starts. A sent/completed archive is not replayed by a mode update;
an intentional fresh callback uses `job retry-agent ID --phase archive`. Updating
the round limit on a parent with a child returns that child ID for further controls.
`finish` releases completed resource holds and cancels unstarted descendants; an
already running child finishes its current training. No control preempts other jobs.

Preflight estimates are advisory. `warnings`, `confidence` and `code_issues` are
passed to the launch agent; they do not require approval. Explicit resources are
preserved even if the model recommends more. After repeated inference failures,
explicit resources provide the fallback; otherwise estimation fails visibly and
can be retried. Startup adaptation is limited to the smallest supported parameter
or per-run configuration changes. GPU mapping, observed OOM, and recognized CLI or
distributed initialization errors use the bounded automatic runtime retry budget.

`launch_agent` and `tmux` default to true. The launch agent runs after reservation
and returns the actual command, environment additions and small per-run files under
`$DEEPQUEUE_RUN_DIR`. The worker sets GPU visibility by UUID; logical device indexes
start at zero. `launch_agent: false` or `tmux: false` are explicit opt-outs for
isolated diagnostics or a user-requested exception. The target server needs tmux.
`job tmux ID` reports the retained log window and an attach command; SSH passwords
are entered interactively by SSH, never included in the command.

Parameter matrix example:

```json
{
  "name": "lr-sweep",
  "defaults": {"server": "gpu-server", "cwd": "/srv/project"},
  "matrix": {"lr": [0.001, 0.0003], "seed": [1, 2]},
  "experiments": [
    {
      "key": "train-{index}",
      "argv": ["python", "train.py", "--lr", "{lr}", "--seed", "{seed}",
               "--output", "runs/{batch}/{key}"],
      "artifact_files": ["runs/{batch}/{key}/metrics.json"]
    },
    {
      "key": "evaluate-{index}",
      "argv": ["python", "evaluate.py", "--checkpoint",
               "runs/{batch}/train-{index}/best.pth"],
      "depends_on": ["train-{index}"]
    }
  ]
}
```

This creates four training jobs and four matching evaluations. Axis arrays form a
Cartesian product. Axes are sorted by name, their values keep array order, and
`{index}` is the one-based combination number. `{batch}` is the batch name and
`{key}` is the expanded key; it cannot be used while rendering the key itself.
Placeholders allow only names, with no attribute access or formatting operations.
Use doubled braces for literal braces in templated text.

`argv` values are shell-quoted as individual arguments. `command` stays literal;
matrix values can instead enter a fixed command through `env`. Do not interpolate
shell programs passed to `bash -c` or `sh -c`. An experiment's command form replaces
the inherited form, but specifying both `argv` and `command` on one item is an error.
Without `matrix`, neither command form performs template substitution.

Matrices permit up to 16 axes and 10000 expanded jobs. Values must be distinct
scalar strings, finite numbers, or booleans. Expanded keys must be unique and at
most 64 characters. Resolved values are saved in `spec.parameters` and batch reports.
`batch preview` validates the expanded specifications, registered server names,
dependency references, cycles, and idempotency conflicts without inserting jobs.
It does not verify project files or available hardware.

Resources example:

```json
{
  "gpu_count": 2,
  "gpu_memory_mib": 20000,
  "gpu_type": "4090",
  "gpu_ids": ["4", "5"],
  "cpu_cores": 8,
  "ram_mib": 32768,
  "estimated_seconds": 14400
}
```

An explicit resource request is retained. The agent may flag it as insufficient
but cannot silently lower it. Whole GPUs are reserved exclusively. CPU and RAM
reservations are admission estimates, not OS limits. CUDA visibility is cooperative,
so code must not override it or escape into another service/container.

SSH setup:

```bash
deepqueue server fingerprint 192.0.2.10
deepqueue server trust 192.0.2.10 --fingerprint SHA256:VERIFIED_FINGERPRINT
deepqueue server add gpu-server --host 192.0.2.10 --user researcher --key /home/me/.ssh/id_ed25519
deepqueue server probe gpu-server
```

System known_hosts entries are respected. Unknown or changed host keys are rejected.
Compare fingerprints through the user's trusted server inventory before pinning.
Use `--password` for a hidden prompt and encrypted local storage, or
`--password-env VARIABLE_NAME` for a credential provided to the daemon environment.
`--passphrase` prompts for an encrypted key's passphrase. Do not put passwords in
command lines, manifests, or chat messages.

Queue controls:

```bash
deepqueue job approve JOB_ID --resources /absolute/path/resources.json
deepqueue job retry-agent JOB_ID --phase estimate
deepqueue job retry-agent JOB_ID --phase archive
deepqueue job retry-agent JOB_ID --phase launch
deepqueue server pause gpu-server
deepqueue server resume gpu-server
deepqueue job cancel JOB_ID
deepqueue batch cancel round-01
deepqueue daemon stop
```

`job resolve JOB_ID --outcome failed --note 'Observed process exit on server'`
releases retained reservations of a lost run. Use only after evidence establishes
that its processes stopped. It does not terminate a remote process.

Dashboard and persistent queue controls:

```bash
deepqueue web --port 8765
deepqueue job pause JOB_ID
deepqueue job resume JOB_ID
deepqueue job priority JOB_ID 50
deepqueue batch pause round-01
deepqueue batch resume round-01
deepqueue batch priority round-01 20
```

Pause applies to unstarted work; active estimates and command runs continue.
A batch hold also blocks new estimates. Resuming the batch leaves independently
paused members held. Completed-job archives are not held. Priority updates skip
active/terminal batch members and preserve original submission fingerprints.
Dashboard and CLI operations use the same transactional gates. The web service
binds loopback only; use an SSH port forward to reach a remote queue host.

Source-conversation return configuration:

```bash
deepqueue config show
deepqueue config set return_agent_socket auto
deepqueue config set return_agent_socket /absolute/path/to/app-server-control.sock
```

`auto` resolves `$CODEX_HOME/app-server-control/app-server-control.sock` or the
same path under `~/.codex`. It connects to the running app-server over a local
WebSocket; it does not restart Codex. `agent_socket` separately selects the socket
for resource estimates, launch preparation and unbound legacy archives (default `null`, using
`agent_command`). Set `return_agent_socket` to JSON `null` only for a standalone
stdio deployment where `agent_command` can resume the source's persisted thread.
It must share the source's Codex home and avoid another active writer.
Restart the DeepQueue daemon after changing connection settings. The default new-agent model
is `gpt-5.6-luna`. Source resume inherits its cwd, developer instructions, and approval
settings. Callback `turn/start` omits the model when following the source model, and
passes a model only for an explicit archive selection. The resolved model is recorded.

`job show` includes agent `delivery_id`, `request_sent`, and `turn_id` fields.
A deferred agent has `status: deferred`; the job retains `archive_status: pending`.
After a restart, the delivery marker is matched against persisted source turns.
A completed matching reply is reused; an active matching turn is allowed to finish.
An unacknowledged delivery absent from history stops automatic retransmission.
`job retry-agent ID --phase archive` explicitly starts a new delivery after inspection.
