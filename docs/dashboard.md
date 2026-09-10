# DeepQueue Dashboard

## Start

```bash
deepqueue init
deepqueue daemon start
deepqueue web --port 8765
```

Open `http://127.0.0.1:8765`. The HTTP service runs in the foreground independently
of the scheduler. Both use the same `--home` or `DEEPQUEUE_HOME`. Closing the browser
or stopping the HTTP service does not stop scheduling or experiment workers.
The compiled React dashboard is included in the wheel; Node is only needed for
frontend development. The default operational state remains on a local filesystem.

For a queue hosted on another machine, forward its loopback port to your laptop:

```bash
ssh -N -L 8765:127.0.0.1:8765 USER@QUEUE_HOST
```

For public access, set the HTTPS origin in **通用设置 > 公网接入** and deploy an
HTTPS reverse proxy. The first setup creates an administrator token and signs in
that browser. API requests then require a token or administrator session. Public
binding requires a configured public URL and administrator credential; loopback
behind a reverse proxy remains the deployment default. The service checks Host
headers, rejects cross-origin writes, and requires `X-DeepQueue: 1` on API mutations.
See [public deployment](public-deployment.md) for the complete setup. Job environment values and stored
SSH credential references are omitted from dashboard responses. SSH passwords
and key passphrases entered in the server form go into the existing encrypted vault.

## Codex Workspace

**Codex 工作区** connects to each server's shared app-server through its saved SSH
credentials (or the local Unix socket). It supports task search and history, new
and continued conversations, model/effort selection, streaming replies and tool
output, interactive requests, and interruption. Experiment details link directly
to the source task in this view. Closing the browser leaves remote tasks running.
See [connection setup and CLI/API](codex-workspace.md).

Cards, navigation, controls and dialogs share a liquid-glass theme with rounded
corners, translucent surfaces and soft highlights over a blue/lavender background.
The shared material is defined in `frontend/src/glass.css`; dense logs and forms
retain readable surfaces. Opaque fallbacks cover browsers without backdrop blur
and users who prefer reduced transparency. The Codex view uses a collapsible
project/task directory and fills the available viewport; on narrow screens the
directory opens as a drawer, preserving space for the conversation and composer.

## Views

All views share a compact toolbar, icon navigation rail, and a full-height content
area with internal scrolling. On mobile, the toolbar opens the navigation drawer.
Servers are the top-level navigation entries. Each server has its own **运行队列**,
**实验批次**, **Codex 工作区**, and **Agent 归档**. Server selection is encoded in
the URL and remembered across visits; switching servers clears the previous batch,
task and job selection. **服务器资源** and **通用设置** are global management views.
The bottom sidebar button expands or collapses the full navigation labels.

Workspace counts, batches and archives include only the selected server. Submission
defaults to that server and rejects batch members targeting another server. Existing
mixed-server batches appear separately within each participating workspace; pause,
resume, priority, cancellation and report downloads operate only on that server's
members. Individual experiment pauses remain independent of batch pauses.

CLI batch commands also accept a server scope, for example:

```bash
deepqueue batch list --server gpu-a
deepqueue batch pause experiment-sweep --server gpu-a
deepqueue batch priority experiment-sweep 80 --server gpu-a
deepqueue batch report experiment-sweep --server gpu-a
```

Without `--server`, local administrator batch commands keep their whole-batch scope.
Remote clients use their configured target server, and server credentials cannot
select another execution server.

Connection status, scheduler start/stop, the latest heartbeat, and administrator
logout live in **通用设置 > 系统运行**. The workspace header stays focused on the
current view's actions.

The submission dialog defaults to **Agent 提交**. Its server, project directory,
and experiment request produce a copyable prompt for the current Codex conversation
to use the `deepqueue` skill. Copying does not create a queue record. The batch JSON
and single-experiment forms remain available as the other two tabs.

All submission tabs include **结束 / 继续改进** and a round limit (default one
additional round). Manual improve-mode submission requires a real source conversation
ID. The copied agent request carries the same selection. Details show the current
mode, round, parent and child links. Selecting **结束** releases completed holds and
stops unstarted descendants; already running training finishes without another round.
The detail panel also edits the maximum improvement rounds while waiting/running.
Preflight code/config issues appear beside resource estimates and are passed to the
launch agent for small parameter adaptations rather than a manual approval gate.
GPU resource tiles distinguish queue reservations and improvement holds from usage.

**通用设置** selects resource estimation, launch adaptation, and archive/improvement
models and reasoning efforts globally. Save once; ordinary submissions inherit these
choices without specifying them again. Settings persist across page and service restarts,
and the daemon reloads them before the next scheduling cycle. Already admitted agents
keep their original configuration. The model's advertised effort levels populate the
effort menu; archive model and effort can independently follow the source conversation.
**启动自动修复** sets the maximum number of automatic OOM/device-mapping retries,
defaulting to three. Zero disables runtime retries. This limit is independent of
the experiment's scientific improvement rounds and takes effect without a restart.

In **服务器资源**, each server's **接入配置** creates/revokes server-specific
submission tokens, downloads a skill naming the configured public URL and fixed
server, and configures its source-task callback socket. The queue icon opens just
that server's jobs. Tokens are shown once and are not included in skill downloads.
The server API enforces these bindings for jobs, batches and controls; insufficient
resources never cause a job to move to another server.

The submission dialog keeps optional **本次 Agent 设置** collapsed. Explicit overrides
apply only to that experiment/batch and its improvement descendants. Choices come from the configured Codex
app-server; **自定义模型 ID** accepts another provider model. **使用会话当前模型**
for archiving resolves the source model at callback time. Explicit archive models
override that conversation's model, including subsequent turns. Unspecified fields
use queue defaults; batch manifest choices are preserved unless a stage is selected
in the form. The copied agent request carries these selections into the skill.
Job details show the effective choices and agent records retain the resolved model
and reasoning effort. Unrecorded effort on old agent histories remains unknown.

**启动适配** shows the new agent's actual command/config and allocated logical GPU
indexes. OOM and GPU-mapping failures trigger automatic preparation and another
execution within the same experiment. **运行记录** retains each attempt's actual
command, adaptation rationale and result. The log selector can reopen an older
attempt's output and tmux command. Exhausted retries end the experiment and return
the failure summary to its source task.
The queue's **实时日志** shortcut and the detail panel's **实时日志 · tmux** button
open the latest training output and scroll to its end. While a run is active,
output refreshes every three seconds and follows new lines. Scrolling up pauses
following; **回到最新** resumes it. The log view also has a copy button for the
tmux connection command, including SSH when needed, and an expandable command.
Output remains available after exit. Original commands, prepared commands and
per-run files remain available in the execution record.

Agent-submitted experiments show **来源对话** and return progress in their detail
panel. Completion summaries render as Markdown with tables and code blocks, and
the source link opens the recorded conversation. Deferred attempts display
**等待对话**; a busy source does not consume model retry attempts. Agent history
retains call errors and links. Manual archive retries can create another summary
in the source conversation. Improvement submissions are deduplicated by parent.

- **Run queue:** filter by server, batch, state, or experiment text; change priority;
  pause, resume, or cancel jobs; open the experiment detail panel.
- **Batches:** inspect completion counts, open a batch's experiments, change the
  priority of its unstarted members, pause/resume/cancel the batch, and download
  the aggregate JSON report.
- **Server resources:** inspect actual CPU, RAM, GPU memory, utilization, process
  counts, probe failures, and snapshot times. Pause command admission per server
  or register a new SSH server after verifying its host-key fingerprint. Click a
  server heading to collapse or expand its resources; the browser remembers this
  choice. Edit its display name, concurrency and SSH connection using the pencil.
  Drag the handle or use the arrows to persist the shared order in the sidebar,
  resource list and server selectors.
- **Agent archives:** open completed-job summaries, findings, resource assessments,
  next-step suggestions, and preserved Codex thread deep links. Failed analysis
  attempts retain their error and can be retried from the detail panel.

Server display names can include Chinese; the stable **服务器标识** continues to
identify existing jobs and CLI/skill submissions. Editing metadata retains SSH
credentials by default. Connection changes require idle experiments/agents, and a
host or port change requires confirming its new fingerprint. Administrator CLI
clients can make the same changes locally or with `--url`:

```sh
deepqueue server update gpu-a --display-name '训练服务器 A' --max-running 4
deepqueue server order gpu-b gpu-a local
deepqueue --url https://queue.example.com server update gpu-a --file server-update.json
```

The order must contain every server identifier exactly once. The JSON update accepts
`display_name`, `max_running`, `host`, `port`, `username`, and `fingerprint`. To replace
credentials, set `authentication` to `password` with `password`, or to `key` with
`key_file` and optional `passphrase`. Use a private file or `--file -` for secrets.

The experiment detail panel includes its immutable command, parameters, dependencies,
current resource request, allocated GPUs, events, live log tail, execution metrics,
and configured artifact files. Log reads are bounded to the last 64 KiB by default;
each artifact read is bounded to its first 32 KiB, with truncation indicated.
The page refreshes queue state every three seconds; the scheduler's separate
`poll_seconds` setting controls resource admission and completion reconciliation.

The order column shows the current candidate order per server, incorporating
priority and aging. It is not a guaranteed start sequence: dependencies, resource
availability, and backfill still apply. Priority arrows change the value by ten;
the numeric field accepts any integer from -100 to 100. Running experiments cannot
be reordered or paused through these controls. Batch and individual pause flags
are independent, and a paused server continues reconciling already-running work.

Submission supports the same batch JSON and matrix syntax as the CLI. Preview is
required before batch submission and does not insert jobs. Single-job submission
defaults to Codex estimation; selecting explicit resources is a deliberate opt-out
from estimation. Finish mode only displays suggestions; improve mode authorizes the
original agent to make a targeted change and submit a priority-100 child within the limit.

## Development and Verification

```bash
uv sync
npm --prefix frontend ci
npm --prefix frontend run build
uv run pytest -q
npm --prefix frontend test
```

The browser suite starts an isolated queue and HTTP service on port 18765. It checks
priority changes, nested pause flags, submission preview, actual CPU execution,
exactly one execution record, log output, artifact inspection, and desktop/mobile
layout. Its archive rendering fixture explicitly identifies `ui-test-fixture`;
this is not a successful Luna inference. Temporary queue artifacts are retained
under `/tmp/deepqueue-browser-*`. Screenshots and failure traces are written under
`frontend/test-results` and are excluded from distributions.

For frontend development, run the Python HTTP service on 8765 and `npm --prefix
frontend run dev`; Vite proxies `/api` to that service. Production uses the built
assets from `src/deepqueue/web_static`. Serving a new build requires no daemon restart.

Schema version 6 adds automatic launch recovery, attempt history and agent/run binding.
Stop an older scheduler before upgrading, run `deepqueue init`, then restart it.
Migration is transactional and preserves job IDs, original submission fingerprints,
resource reservations, execution records, and agent links.

Schema version 7 adds persistent batch pauses per server. Existing global batch
pauses remain effective until that server is resumed or the whole batch is resumed.
Global pause/resume commands reset all per-server overrides. The same upgrade
procedure applies; existing experiment and batch membership is preserved.
