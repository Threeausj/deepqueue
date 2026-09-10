# Operating DeepQueue

For a public queue coordinating execution servers over SSH, use the
[public deployment guide](public-deployment.md). It covers HTTPS authentication,
server-bound clients/skills, and forwarding each source Codex app-server. The local
operator commands below remain available on the queue host with an explicit `--home`.

## Install and Start

```bash
uv sync
uv tool install --editable .
deepqueue init
deepqueue skill install
deepqueue doctor
deepqueue daemon start
```

The CLI can also run through `uv run deepqueue`. The packaged skill installs to
`~/.agents/skills/deepqueue`; pass `--path` for another destination. A repository
copy is included at `.agents/skills/deepqueue`, and its source lives in
`src/deepqueue/skill`. Installation refuses to overwrite an existing skill.

`doctor` checks the app-server handshake, model catalog, and skill discovery. It
does not run a model request or prove upstream inference authorization.

The default agent is the local official Codex app-server using `gpt-5.6-luna`.
The service must run as the user with the intended Codex login and configuration.
If a custom provider returns HTTP 401/403, correct that provider's authorization
for the `deepqueue` client. The application does not change credentials, substitute
models, or impersonate another client. A different authorized Codex profile can
be selected through `agent_command` using that installation's supported options.

```bash
deepqueue config show
deepqueue config set model gpt-5.6-luna
deepqueue config set poll_seconds 10
deepqueue daemon stop
deepqueue daemon start
```

Model/effort defaults and `launch_max_retries` reload automatically; other
configuration changes require a daemon restart. Stopping the daemon leaves existing
workers running. `daemon start` is a detached local process, not a boot-time service.

The [dashboard](dashboard.md) is served with `deepqueue web --port 8765` and shares
the same queue state as the CLI.

## Automatic Launch Recovery

Preflight is advisory: it estimates approximate GPU/CPU/RAM needs and records
confidence, warnings and suspected `code_issues` for the launch agent. It never
edits the project or holds a valid estimate for manual approval. An explicit user
resource request remains the execution contract, even if the estimate recommends
more capacity. Repeated inference failures fall back to that explicit request;
without one, the job fails visibly rather than guessing its GPU count.
`min_confidence` remains accepted for older configurations; it no longer blocks admission.

After reservation the launch agent adapts physical-to-logical GPU bindings and
distributed launch options. It does not reopen an approved resource review because
historical memory measurements are missing. If a process fails with a recognized
CUDA OOM, the next launch agent reads that attempt's logs and can reduce per-device
batch size or apply supported accumulation, mixed precision or checkpointing.
Device-mapping failures trigger equivalent replanning against the actual cards.
Recognized malformed/unsupported CLI options and distributed-initialization errors
also trigger bounded replanning. Launch preparation assumes a working submission
and prefers the smallest supported parameter change, preserving existing entrypoints
and avoiding project rewrites. It passes preflight issues as diagnostic leads.
Dataset splits, labels, architecture and epoch targets remain fixed; actual
hyperparameter changes are recorded in each run and included in the final archive.

```bash
deepqueue config set launch_max_retries 3
```

The default is three runtime retries, with a supported range of 0-10. The dashboard's
General Settings exposes the same control. Each retry uses a fresh immutable run ID
and tmux window while retaining the original job ID, server and source task. GPU
reservations transfer only after the previous training process tree has stopped. Cancellation
prevents another attempt; lost workers retain their reservations. Exhausted retries
produce a terminal failure and archive instead of waiting for human review.

This feature requires `launch_agent=true` and a GPU allocation. CPU-only commands
retain their original launch plan. Exit code 137 or an arbitrary error alone is not
treated as evidence of CUDA OOM. The worker detects foreign GPU use within its own
process tree and incomplete GPU use after 120 seconds of observed GPU activity.
Elastic/DDP ranks in separate sessions are tracked by ancestry. The worker acts as
a Linux child subreaper and cleans up adopted training descendants before recording
exit. This does not provide hardware isolation from external processes.

GPU attribution requires NVIDIA process IDs to be visible in the worker's `/proc`.
Run the SSH worker on the GPU host, or use a matching host PID namespace for a
container worker. Inaccessible NVIDIA PIDs are reported as unavailable attribution;
the worker does not invent per-job GPU measurements or use incomplete attribution
to terminate a run. OOM/device errors in training logs still trigger automatic repair.

Stop a schema-5 daemon before upgrading to schema 6, run `deepqueue init`, then
restart the web service and daemon. Existing experiments and historical runs remain
intact. Previously reviewed-but-blocked launches can be resumed with
`deepqueue job retry-agent JOB_ID --phase launch`.

## Adjust an Experiment's Next Stages

```bash
deepqueue job mode JOB_ID improve --max-improvement-rounds 2
deepqueue job update JOB_ID --archive-model source-thread --archive-effort high
deepqueue job update JOB_ID --intent 'Compare primary validation metrics with the previous run'
deepqueue job update JOB_ID --baseline-job-id BASELINE_ID
deepqueue job update JOB_ID --launch-max-retries 2
deepqueue job update JOB_ID --command 'python train.py --batch-size 8' --resources /absolute/resources.json
deepqueue job update JOB_ID --file /absolute/patch.json
deepqueue job mode JOB_ID finish
```

`job update` accepts partial completion, round-limit, stage-model/effort, retry-budget,
feedback-intent, baseline, artifact and preparation settings. Stage maps merge;
null inside a stage map restores global defaults. Command/resources/context/parameters
can change only before estimation or launch and invalidate the previous estimate.
The original submission fingerprint and admitted execution records are retained.
Artifact lists, baseline and intent cannot change while an archive is running.
Other stage selections apply to future calls; the admitted agent keeps its settings.

The default improvement limit is one, excluding the original run. Switching a
running finish-mode job to improve affects its upcoming summary and child submission.
The exit-to-archive-pending window also accepts this switch. An already active or
completed archive is not silently replayed; an intentional new callback uses
`job retry-agent ID --phase archive`. Adjust an existing child's round limit directly.
Switching an ancestor to finish stops continuation through its descendants, releases
finished resource holds and lets already running training finish. Every update is
transactional and leaves an event in the job history.

All controls work with the public queue's URL/server-bound CLI. The HTTP equivalent
is `POST /api/jobs/JOB_ID/settings` with a partial JSON object; the existing mode
control accepts the optional round limit as well. Source/server/cwd cannot be reassigned.

Archives receive the explicit `baseline_job_id`, otherwise the parent, otherwise
the previous completed experiment on the same server/project before this execution
started. Saved historical archive evidence takes precedence over mutable result
paths; when it is missing, only the old run log is read, and old artifact metrics
are marked unavailable. Feedback reports initial/best/final accuracy/loss, matched
metric deltas, comparability limitations, and targeted next experiments grounded in
those observations. A zero exit code is distinct from an improvement in model quality.

## Pause and Prioritize

```bash
deepqueue job pause JOB_ID
deepqueue job resume JOB_ID
deepqueue job priority JOB_ID 50
deepqueue batch pause round-01
deepqueue batch resume round-01
deepqueue batch priority round-01 20
```

These controls survive restarts. Pausing a job or batch blocks new resource
reservations and new estimate agents. Estimates and command runs already admitted
continue, as does result archiving. Individual pause flags remain set when their
batch resumes. Server pause prevents new command reservations but still permits
resource estimation and reconciliation of existing runs.

Pause checks run inside the same transaction that admits a command or estimate,
so an old scheduler snapshot cannot override a newly committed pause. The `paused`
flag is distinct from execution `status`; `pause_sources` lists effective holds on
unstarted jobs. A standalone job sharing a batch's group label is not a member.

Priorities range from -100 to 100 and affect both scheduling and new agent admission.
Only unstarted jobs can be reprioritized; batch operations skip active and terminal
members. Changes are recorded in events, while original specs and idempotency
fingerprints remain unchanged. Resource and dependency constraints still apply.
Batch cancellation is atomic, and running-job reservations remain until completion
is established. Pause does not send SIGSTOP or free the GPUs of a running process.

An older schema-1 daemon must be stopped before schema-2 migration. Run `deepqueue
init` and restart the daemon; commands reject migration while an old daemon owns
the queue. Backfilled membership is taken from recorded batch IDs, not group labels.

## Submit a Round

Start from `examples/experiments.json`. Set the actual server and absolute project
directory, provide code/config paths, and keep output directories distinct.

```bash
deepqueue batch preview /absolute/path/round-01.json
deepqueue batch submit /absolute/path/round-01.json
deepqueue batch list
deepqueue batch show round-01
deepqueue batch report round-01
```

Submit all independent experiments together. The queue estimates them with bounded
agent concurrency and runs as many as fit. Within a batch, `depends_on` references
experiment keys. A same-name, same-content manifest is idempotent; changing an
existing batch under the same name is rejected. Invalid manifests roll back fully.

For one command:

```bash
deepqueue job submit --server local --cwd /srv/project \
  --title baseline --command 'python train.py --output runs/baseline' \
  --context train.py --context config.json \
  --artifact runs/baseline/metrics.json \
  --intent 'Fixed train/validation split, mixed precision, batch size 16' \
  --idempotency-key round-01-baseline
```

Raw shell commands are run by Bash in the target directory. Put environment
activation into the foreground command, for example `source .../activate && python
train.py`, or use the target environment's absolute interpreter. Do not set CUDA
visibility inside the command. Pin GPUs through resource fields instead.

The `--resources` option reads a JSON file. Explicit resources are assessed by
Codex but cannot be silently decreased. `--skip-estimate` is available only with
explicit resources. `--no-archive` disables the final model call for an isolated
diagnostic or a job that does not need a summary.

## Parameter Matrices

Use `examples/parameter-sweep.json` to define an entire parameter search in one
manifest. Top-level `matrix` maps each parameter name to an ordered array of values.
Every experiment template is expanded for every Cartesian-product combination.
For example, 2 learning rates x 2 batch sizes x 3 seeds produce 12 training runs;
an evaluation template in the same manifest produces 12 dependent evaluations.

```json
{
  "name": "lr-sweep",
  "defaults": {"server": "local", "cwd": "/srv/project"},
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

Placeholders accept an axis name, `{batch}` for the batch name, `{index}` for the
one-based combination index, and `{key}` for the expanded experiment key. `{key}`
is available after rendering the key itself. Axis names are sorted alphabetically
before expansion, while each value array preserves its order. Reordering JSON
object keys therefore does not change which combination an index identifies.
Use `{{` and `}}` for literal braces in templated text. Attribute access, indexing,
conversion flags, format specifiers, and unknown placeholders are rejected.

Templates apply to string values in the experiment fields, including `argv`,
`env`, `resources`, titles, paths, and dependencies. Object keys are not templated.
`command` always remains a literal shell script. Use `argv` for dynamic command
arguments; DeepQueue quotes each token using `shlex.join`. Values containing spaces,
quotes, or shell metacharacters are passed literally. For environment activation,
use an absolute interpreter, `conda run` argv, or a fixed shell command with matrix
values supplied through `env`. Do not construct interpolated shell scripts inside
`sh -c` or `bash -c` argv; those programs intentionally interpret their arguments.

`argv` also works without a matrix, with no template substitution. An experiment's
`argv` replaces an inherited `command`, and its `command` replaces an inherited
`argv`. Setting both on the same experiment is rejected. Existing command manifests
without `matrix` retain their literal braces and previous behavior.

The resolved scalar parameter values are stored in each job's `spec.parameters`,
included in agent evidence, and returned by `batch report`. Matrix parameters
cannot be contradicted by manually supplied `parameters` metadata. Explicit GPU
IDs, resource requests, dataset choices, and command outputs retain their normal
contracts after expansion.

`batch preview FILE` uses the same expansion and input validation as submission.
It checks registered servers, existing external dependencies, duplicate keys,
dependency cycles, resource-field types, and batch-name conflicts. It returns the
resolved job specifications and dependency order. It does not modify the queue,
probe servers, check filesystem contents, estimate GPU demand, or invoke Codex.
Read the resolved commands before submitting a new matrix. A same-name submission
with the same source manifest stays idempotent; a changed manifest needs a new name.

Limits are 16 axes and 10000 expanded experiments. Each axis must be a nonempty
array of distinct strings, finite numbers, or booleans. Built-in placeholder names
are reserved. Keys must be unique across the expanded batch and at most 64
characters long; `train-{index}` is convenient for large parameter sets.

## Add SSH Servers

```bash
deepqueue server fingerprint 192.0.2.10 --port 22
deepqueue server trust 192.0.2.10 --fingerprint SHA256:VERIFIED_FINGERPRINT
deepqueue server add gpu-a --host 192.0.2.10 --user researcher \
  --key /home/me/.ssh/id_ed25519 --max-running 4 --gpu 0 --gpu 1
deepqueue server probe gpu-a
```

Verify a new fingerprint against the server's trusted inventory. System known_hosts
is read automatically; unknown or changed host keys are rejected. Repeating `--gpu`
sets a server allowlist. Omit it to consider all physical GPUs on that server.

Password and encrypted-key options:

```bash
deepqueue server add gpu-b --host 192.0.2.11 --user researcher --password
deepqueue server add gpu-c --host 192.0.2.12 --user researcher \
  --key /home/me/.ssh/encrypted-key --passphrase
deepqueue server add gpu-d --host 192.0.2.13 --user researcher \
  --password-env GPU_D_SSH_PASSWORD
```

Passwords and key passphrases entered through the hidden prompts are encrypted
using a local Fernet key; the DB contains only references. The queue home and
credential files are private to the service user. The key resides on the same
host, so encryption protects copied database/credential records rather than a
compromised account. Environment references must be set in the daemon environment.

The remote helper is automatically installed under `.local/share/deepqueue-worker`
in the SSH user's home. No remote pip packages are required. Registration itself
does not run any experiment. Pausing a server prevents new starts while allowing
existing jobs to finish:

```bash
deepqueue server pause gpu-a
deepqueue server resume gpu-a
```

## Source Conversation Returns

The normal agent workflow is `batch preview FILE --from-agent`, followed by
`batch submit FILE --from-agent`. Both bind the environment's real `CODEX_THREAD_ID`
to every expanded job. Single jobs use `job submit --file FILE --from-agent`.
An explicit `--source-thread-id ACTUAL_ID` is supported outside an agent environment.
Conflicting IDs and `archive: false` with a source ID are rejected. Unbound CLI and
web submissions never infer an origin from inherited process environment variables.

The immutable `spec.source_thread_id` is included in queue records and source links.
When a command reaches succeeded, failed, cancelled, or blocked, the archive worker
connects to the owning Codex app-server and calls `thread/resume` for that ID, then
`turn/start` with execution evidence. It does not start a replacement conversation.
An active source is deferred. Callbacks to one source are serialized across this
queue, and a busy source also imposes a shared retry delay on its other jobs.
Execution reservations have already been released before a terminal-job callback.

The summary is a Chinese Markdown reply in the original conversation, using its
original context. Its exact text is copied into `analysis.summary`, tagged with
`format: markdown`, and exported into the usual archive files. The frontend renders
the reply and shows the source link, return status, and every agent attempt.
Failed or unavailable evidence is reported as missing; it cannot prevent the
recorded execution outcome from reaching the source conversation.

Configuration (restart only DeepQueue after changing it):

```bash
deepqueue config set return_agent_socket auto
deepqueue config set agent_socket auto
```

When a server's Codex workspace connection is enabled and no explicit server
callback socket is set, feedback connects directly to that server via SSH. It uses
the saved credentials and shared app-server proxy independently of the browser.
See [Codex workspace operations](codex-workspace.md). Global defaults below apply
when there is no server connection or explicit callback override.

`return_agent_socket` defaults to `auto`, resolving
`$CODEX_HOME/app-server-control/app-server-control.sock` or the equivalent under
`~/.codex`. It uses WebSocket over the local Unix socket with compression disabled.
`agent_socket` defaults to `null`, using `agent_command` over stdio for estimates
launch preparation and legacy archives; `auto` connects these phases to the running app-server.
Either socket setting accepts an absolute path. Explicit JSON `null` for returns
selects standalone stdio and requires the source's persisted Codex home without
another active writer. No transport fallback creates a fresh conversation.

Resume inherits cwd, developer instructions, permissions and the source's model.
The callback normally omits model overrides and records the model returned by resume.
Each job can select `agent_models.estimate`, `.launch`, and `.archive`. Archive and
authorized improvement share one source turn. Select `archive: "source-thread"` to
follow the source's current model at callback time, regardless of the queue default.
A concrete archive model ID overrides it for that and subsequent source turns.
Omitted stages use `config.agent_models`, then the default `model` for new agents
or the source model for bound archives. `deepqueue models` lists the configured
app-server catalogs without inference. Submission flags are `--estimate-model`,
`--launch-model`, and `--archive-model`. Batch defaults and improvement children
inherit model choices by stage; an explicit null resets a stage to the queue default.

```bash
deepqueue job submit --file /absolute/job.json --from-agent \
  --estimate-model gpt-5.6-luna --launch-model gpt-5.6-luna --archive-model source-thread
deepqueue config set agent_models '{"estimate":"gpt-5.6-luna","launch":"gpt-5.6-luna","archive":"source-thread"}'
```

Global models and reasoning efforts can be set once in the dashboard's General Settings.
The daemon reloads `model`, `agent_models`, `agent_effort`, `agent_efforts`, and
`launch_max_retries` before
the next scheduling cycle; already admitted agents keep their original settings.
Normal submissions omit both stage override objects. Per-job `agent_efforts` follows
the same stage inheritance as models, with `source-thread` available for archive.
`deepqueue models` lists supported reasoning efforts; CLI override flags are
`--estimate-effort`, `--launch-effort`, and `--archive-effort`.

```bash
deepqueue config set agent_efforts '{"estimate":"low","launch":"medium","archive":"source-thread"}'
```

App-server connection changes still require a daemon restart. Schema 5 records the
resolved effort on each agent attempt; older records have no recorded effort.
`finish` asks for an effectiveness summary. `improve` authorizes targeted model
changes, validation and one high-priority child through the skill, within the
configured round limit. Shared interactive approvals belong to the Desktop client.
App-server does not provide an atomic idle-only turn start: an external user turn
can still race the final idle check. Queue callbacks are serialized transactionally.

Delivery IDs and dispatch intent are durable before sending a turn. Recovery checks
the marker in source history and reuses an already completed answer. Busy waits and
scheduler restarts do not consume model-failure retries. If a sent message cannot
be found in history, automatic retransmission stops with a visible error. Inspect
the source, then explicitly use `job retry-agent ID --phase archive` to request a
new delivery. An explicit retry can add another summary; the unique parent-child
constraint prevents another improvement child for the same parent.
Source history must be retained and accessible; cross-host conversations require
the queue to connect to the app-server owning that history.

The installed protocol contract is generated with `codex app-server generate-json-schema`.
See the [official app-server documentation](https://developers.openai.com/codex/app-server/).

## Inspect and Recover

```bash
deepqueue job show JOB_ID
deepqueue job logs JOB_ID --raw
deepqueue job links JOB_ID
deepqueue job cancel JOB_ID
deepqueue batch cancel round-01
deepqueue job approve JOB_ID --resources /absolute/path/resources.json
deepqueue job retry-agent JOB_ID --phase estimate
deepqueue job retry-agent JOB_ID --phase archive
```

The `reason` field explains resource waits, dependency failures, or model errors.
Agent failures are retried with a bounded delay. Exhausted estimation falls back
to submitted resources or ends as `failed`; exhausted preparation/archive calls
remain visible failures. `needs_review` is retained for legacy records and explicit
operator controls. Agent retry starts another analysis, not an executed training
command. Recognized GPU/OOM/startup-configuration failures retry automatically within
the launch budget; other executed failures require a new job or authorized improvement.

For `lost`, inspect the process and worker records on its server. Retained leases
prevent the queue from assigning its GPUs elsewhere. Only after verifying that
the command stopped, record an explicit resolution:

```bash
deepqueue job resolve JOB_ID --outcome failed --note 'Verified process stopped after server reboot'
```

This operation records an outcome and releases leases; it does not kill a process.

## Files and Service

State layout:

```text
DEEPQUEUE_HOME/
  config.json                  daemon and Codex configuration
  queue.sqlite3                servers, jobs, runs, leases, agent sessions, events
  known_hosts                  explicitly trusted SSH hosts
  secrets.key / secrets/       encrypted credential storage
  scheduler.lock / heartbeat.json
  scheduler.log
  agents/AGENT_ID/              prompt, schema, evidence, events, output, stderr
  archives/JOB_ID.md            result summary, next steps, task links
  archives/JOB_ID.json          structured archival record
```

Server-side `worker_root/runs/RUN_ID/` contains the immutable command spec,
supervisor identity, metrics, output log, and durable exit record. Full logs stay
on the execution server; CLI and agent evidence use bounded tails. Code/artifact
evidence is limited to 16 files per category, 32 KiB per file, inside the job cwd.

For boot-time operation, adapt `deploy/deepqueue.service` with the service user's
paths and install it through the machine's normal systemd workflow. `KillMode=process`
is intentional so restarting the scheduler does not terminate local experiments.
Do not start a second independent queue against the same reserved GPU pool.

## Verification

```bash
uv run pytest -q
uv run ruff check .
uv run python scripts/matrix_smoke.py --home /tmp/deepqueue-matrix-NEW_UNIQUE_DIRECTORY
uv run python scripts/live_smoke.py --home /tmp/deepqueue-luna-NEW_UNIQUE_DIRECTORY
uv run python scripts/return_smoke.py --home /tmp/deepqueue-return-NEW_UNIQUE_DIRECTORY --via-agent
```

Unit and integration tests cover atomic batches, dependencies, GPU reservations,
remote SSH password/key authentication, SFTP deployment, worker cancellation,
timeouts, persistent launches, and app-server protocol handling. The separate
live smoke test makes real Luna requests for two small CPU experiments. It writes
`verification.json` only when both commands and both agent phases complete.
The matrix smoke test separately runs six CPU-only jobs without making model
requests, verifies their parameter values, and checks the two-job concurrency limit.
The return smoke test creates its own conversation, has Luna read the skill and
submit a CPU fixture using its real conversation ID, then verifies the completion
reply in the same source history and in the queue archive.
