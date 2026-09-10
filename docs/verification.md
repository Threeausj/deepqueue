# Verification: 2026-09-10

## Repository preparation — 2026-09-10

- Full backend suite: **257 passed** with two upstream test-client deprecation warnings.
- `ruff check .` and the complete frontend formatting check passed.
- The README's relative links and its two-experiment JSON example were validated.
  The example was compiled in memory with a fixture source ID; it was not submitted.
- The latest frontend build passed. The source-conversation layout and live-log
  workflows both passed after the log-button spacing adjustment, including desktop
  and mobile screenshots. The broader 14-workflow dashboard regression passed
  before that spacing-only change.
- The README screenshot contains browser-fixture data. Queue databases, local
  credentials, environment files and generated test results are excluded from Git.
- Docker packaging is provided, but Docker is unavailable in this environment.
  Container entrypoint tests run real isolated web/scheduler processes; a Docker
  image build and `scripts/docker_smoke.py` still need a Docker-capable host.

The entries below are historical development records. Their temporary paths and
operational-service observations describe those checks, not a new deployment.

## Server Codex Workspace — 2026-09-09

- Backend: **218 passed**, including per-server connection isolation, shared
  fan-out, request generation guards, send deduplication, history compatibility,
  and actual loopback SSH WebSocket transport using password and encrypted-key
  authentication. SSH continues to use verified host keys and the existing vault.
- Browser: **10 passed** for the complete dashboard; the gateway flow also passed
  again after the live-protocol compatibility correction. It exercises configuration,
  streaming replies, pending request recovery after reload, user input, interruption,
  reconnect, new tasks, and a 390 px layout. Existing 320 px dashboard checks pass.
- Real local **gpt-5.6-luna** gateway test:
  `/tmp/deepqueue-gateway-luna-20260909-02/verification.json`.
  Task `01a0861e-1dc7-7d70-b884-e03884cb330a` received a message through the same HTTP
  API used by the browser. A real CPU fixture then completed while the gateway was
  disconnected; the scheduler independently returned feedback to that same task
  through the configured server Codex connection. The deliberately invalid global
  callback socket was not used. Archive model remained Luna. The gateway reconnected
  and read both turns. Metrics (0.77 accuracy / 0.23 loss) are explicitly synthetic;
  there was no GPU training or claimed model-quality improvement.
- Local runtime: Codex Desktop/CLI **0.153.4**. Newly created web tasks explicitly
  use legacy history; before the first message, their unmaterialized history is
  handled without an invalid resume call. Existing paginated tasks unsupported by
  this runtime return a clear compatibility error without rewriting their history.
- Production scheduler and web service remained stopped. Tests used isolated state
  directories. Host setup, public reverse proxy deployment and native Desktop deep-link
  navigation to remote tasks were not exercised against production servers.

See [workspace setup and API](codex-workspace.md). Earlier entries below record the
previous implementation stages and their verification environments.

## Advisory Preflight, Feedback and Runtime Controls

- Full backend regression, `uv run pytest -q`: **205 passed**. Coverage includes mode changes during training
  and between exit/archive dispatch, round inheritance, atomic rejected edits,
  cancellation of follow-up work, preparation retry, remote scoped controls,
  preserved submission idempotency, advisory estimates and explicit-resource fallback.
- The final archive-disconnect policy guard passed **58 focused tests** across
  `test_workflow.py`, `test_continuation.py` and `test_return.py`. A previously sent
  callback requires an explicit new delivery before changing from finish to improve.
- All **9 Playwright workflows** passed, including changing the round limit and
  completion mode, reloading persisted settings and checking the 320px detail layout.
- Ruff lint/format, frontend formatting/build, skill validation and `uv build` passed.
  The installed `deepqueue job update --help` exposes the new controls. The wheel
  includes updated CLI, scheduler, frontend and skill, with no runtime database or secrets.
- `scripts/launch_smoke.py` ran real local **gpt-5.6-luna** preflight and launch
  planning. With synthetic physical GPUs 0,1,3,7, the only command change was device
  mapping to logical 0,1,2,3; no wrapper, environment additions or code changes were
  generated. No GPU training was executed by this diagnostic.
  Evidence: `/tmp/deepqueue-advisory-luna-20260909-01/verification.json`.
- `scripts/feedback_smoke.py` executed two real CPU fixtures and archived each with
  local Luna. The second run reused `metrics.json`; comparison retained the first
  archive's saved evidence. Luna reported final validation accuracy **0.72 → 0.77**
  (**+5 percentage points**) and loss **0.28 → 0.23**, with initial/best/final trends.
  It correctly stated that these are synthetic outputs rather than measured model
  improvements. Evidence: `/tmp/deepqueue-feedback-luna-20260909-01/verification.json`.

These changes retain database schema 6. Job controls update mutable settings and
audit events without changing admitted run records or original submission hashes.
Development verification used isolated queue homes; the operational scheduler
remained stopped.

## Earlier Verification: 2026-09-07

## Passed

- `uv run pytest`: 189 tests passed. Two upstream test-client deprecation
  warnings remain; there were no test failures.
- `uv run ruff check .`: all checks passed.
- `npm --prefix frontend run build` and `npm --prefix frontend run format:check` passed.
- `npm --prefix frontend test`: all eight Playwright workflows passed.
- `uv build`: source distribution and wheel built successfully. The wheel includes
  the standalone worker, CLI entry point, and installable skill. Runtime state and
  credentials are excluded from both distributions.
- The skill-creator validator accepted the packaged DeepQueue skill.
- The installed local `codex-cli 0.153.4` completed the app-server handshake.
  `model/list` returned `gpt-5.6-luna`, and `skills/list` discovered DeepQueue.
- Real loopback SSH tests passed with a password and an encrypted private key,
  including SFTP helper deployment, execution, persistent status, and log retrieval.
  Unknown hosts and mismatched fingerprints were rejected.
- Recovery tests covered a restart before dispatch and after dispatch. Each command
  ran once, retained its run ID, and produced an archive through the protocol test
  adapter. Cancellation, timeouts, dependency blocking, and GPU lease conflicts
  were also verified.
- On the current host, an explicit request for 8 GPUs / 22000 MiB per GPU remained
  `queued` with `Waiting for 8 eligible idle GPUs; 0 available`. Its `run_id` was
  null. The diagnostic job was cancelled after the check.

## Automatic Launch Recovery

Schema 6 records each automatic repair as a separate execution attempt and links
its launch agent to that run. Tests cover repeated OOM, non-contiguous GPU mapping,
replacement of newly occupied GPUs, atomic reservation transfer and conflict
rollback, cancellation, stale results, scheduler restarts, bounded retries, and
archiving only the final outcome. Worker tests inject GPU telemetry and verify
binding failures across separate rank sessions without stopping unrelated jobs.
Success, launcher failure, cancellation and timeout all reap adopted rank processes;
the tests also verify restoration of the previous subreaper state.
Unrelated NCCL errors, timeout and exit 137 alone do not imply OOM.

`/tmp/deepqueue-auto-recovery-live-06/verification.json` records real local Luna
(`gpt-5.6-luna`, medium) mapping physical 0,1,3,7 to logical 0,1,2,3. The first
CPU diagnostic execution emits an OOM with batch 8; a new launch agent reduces
batch to 2, the second execution succeeds, and Luna completes the archive.
Both executions retained separate tmux windows and GPU reservations were released
at the end. Job: `1a92ad14c4e04259bfdf4d8d4e5a03ea`.
Telemetry and OOM are synthetic; this does not measure a real GPU OOM or model quality.

Earlier diagnostics exposed generated executable whitespace and slash escaping.
Validation now restores a malformed executable only when the corrected absolute
path exactly matches the known original. Other malformed paths are rejected, with
the failure supplied to the next agent. The shell command tail remains unchanged.
The raw app-server events and normalized plan remain available for inspection.

```bash
uv run scripts/recovery_smoke.py --home /tmp/new-auto-recovery-check
```

Browser tests verify the global retry limit, hot reload, attempt history, old and
current log selection, and 320px layouts. Live desktop/mobile screenshots under
`/tmp/deepqueue-live-auto-recovery-{desktop,mobile}.png` were inspected with no
console errors or horizontal overflow. The operational queue was backed up and
upgraded to schema 6; both existing job specifications and archives were preserved.
Its saved Terra/xhigh launch selection was retained, and the retry limit defaults to 3.

The previously blocked real job `6505c54f912340b99f9ccbf224325a38` was resumed on
its original server and source task. Its launch agent added Accelerate `--multi_gpu`
and started run `42812a27469c47ae9ca933e599cf9b69` on physical GPUs 1,2,3,4.
Four training processes were observed, with about 12.6 GiB allocated per GPU.
The run completed all three epochs in 565 seconds, exited with code 0 and released
its reservation. The saved CSV contains epochs 0,1,2; this is a short sanity run,
not evidence of a model improvement. Source archival encountered upstream auth and
temporary Codex configuration errors; after configuration syntax recovered, the
same source task was retried without rerunning training.
Its Terra/xhigh turn `01a07b56-0f2a-7ad3-81b8-2696e1a73b05` completed and the
summary is saved in the queue with `archive_status=completed`. The summary
distinguishes DDP execution success from model quality and identifies learning-rate
and distributed-validation concerns for a later experiment.

This development container exposes NVIDIA host PIDs that are absent from its
`/proc`, so the completed run's empty per-process GPU samples are not a measurement
of zero GPU memory. The new worker reports unavailable attribution explicitly.
For automatic usage auditing, an SSH worker must see NVIDIA PIDs in its own PID
namespace. Log-based OOM recovery remains available when attribution is unavailable.

## Public Queue and Server Binding

Public deployment now has an HTTPS origin, administrator session login, revocable
server-scoped bearer credentials, remote CLI profiles, and server-specific skill
downloads. Tests verify authentication, host/origin boundaries, cookie revocation,
credential redaction, no cross-origin redirects, atomic rejection of mixed-server
batches, and denial of other servers' job reads/controls. Real HTTP client tests
exercise preview, source binding, idempotent submission, pause, priority, links,
and batch reports without opening a local queue.

An isolated resource test kept a high-priority job on a fully occupied server
while another server allocated its own GPU. No physical GPU training was run.
Browser checks cover initial public setup, the one-time administrator credential,
server token creation/revocation, skill download, callback settings, logout/login,
session persistence, and desktop/320px layouts. Screenshots were inspected.

`/tmp/deepqueue-public-live-01/verification.json` records real local Luna using the
exported deployment skill to submit through an authenticated HTTP client. A private
loopback SSH server accepted password authentication, deployed the worker over
SFTP, and executed the synthetic CPU command exactly once. Estimation, launch, and
source archive completed with Luna. The per-server callback socket overrode an
intentionally unavailable global socket, and source history exactly matched the
archived Markdown. Source: `01a07a87-7ba7-7340-acfd-195ca4437d37`.
Job: `7b68b7ffc7014b4c8ed5bcddf7e90881`. The diagnostic web/SSH services stopped afterward.

```bash
uv run scripts/public_smoke.py --home /tmp/new-public-queue-check
```

This test uses the source checkout's SSH fixture and development dependencies.
HTTP was loopback-only; public DNS, a deployed TLS proxy, and an SSH-forwarded Codex
socket were not tested on an external host. Deployment templates and connection
instructions are in [public deployment](public-deployment.md). The operational
queue remains local until a real public URL is configured; existing experiment
history and global agent preferences were preserved.

## General Settings and Reasoning Effort

The dashboard's General Settings page saves models and reasoning efforts separately
for estimation, launch adaptation, and archive/improvement. Ordinary agent, single,
and batch submissions omit overrides and inherit the saved defaults. Browser checks
verified save/reload, model-supported effort menus, undo, optional submission overrides,
and desktop/320px layouts. A running fixture daemon picked up the changes with the
same PID. In-flight agents retain their admitted settings.

Backend coverage includes per-stage precedence, source-conversation effort inheritance,
explicit archive overrides, accepted-disconnect recovery without duplicate delivery,
parent-child inheritance, old submission fingerprints, concurrent configuration writes,
and schema 4 to 5 migration. Existing history has no invented reasoning effort.

`/tmp/deepqueue-global-settings-live-01/verification.json` records real local Luna
execution with no job model or effort overrides. The estimate, launch, and archive
agents used the global `low`, `medium`, and `high` efforts respectively. The CPU
command ran once, and the archived Markdown exactly matches the reply in the source
task `01a07a0f-ff64-78f0-a09b-8bf632672389`. Job: `907bf11ea89d4877b19c289471adf57f`.
This is a synthetic integration check, not evidence about model quality.

The operational service was upgraded to schema 5 and restarted on port 8765. Its
existing completed experiment and archive were preserved. Live browser checks at
1440, 768, and 320 pixels found no console errors, horizontal overflow, or clipped
setting fields. Screenshots `/tmp/deepqueue-live-global-settings-{desktop,tablet,mobile}.png`
were inspected. The live catalog advertised Luna efforts `low`, `medium`, `high`,
`xhigh`, and `max`, with `medium` as its default. Model catalog reads do not run inference.

The initial submission wait timed out after the skill had submitted the job. The
source subsequently completed; verification resumed that existing pending job without
submitting another. The diagnostic prompt now reflects the manifest's actual
`skip_estimate` value, and its submission wait allows 240 seconds.

```bash
uv run scripts/return_smoke.py --home /tmp/new-global-settings-check --via-agent --agent-sandbox danger-full-access --global-agent-defaults
```

## Stage Model Selection

Jobs, batch defaults, submission CLI flags, and the dashboard now select estimate,
launch, and archive/improvement models independently. Tests cover queue defaults,
per-stage overrides, model ID validation, parent-child inheritance, explicit resets,
old submission fingerprints, and source-model inheritance after a model change.
Archive protocol tests verify concrete overrides and recovery of a completed reply
without a second delivery, even when queue defaults change after dispatch.
The browser checks single and batch persistence, custom model IDs, agent handoff
text, the source-model option, preview invalidation, and 320px/mobile layouts.

`/tmp/deepqueue-stage-models-live-01/verification.json` records a real local Luna
skill submission with explicit Luna selections for all three phases. Estimation,
launch adaptation and archive completed; the CPU command executed exactly once.
Source and archive task: `01a079e0-8b84-7122-a71d-9c4d6dba2bac`.
Job: `e2d8240e66734c41ad00395389c84066`.
This was a synthetic file-writing integration check with no GPU training.

```bash
uv run scripts/return_smoke.py --home /tmp/new-stage-model-check --via-agent --agent-sandbox danger-full-access --explicit-stage-models
```

`deepqueue models` also retrieved the installed app-server catalog for both configured
connections without inference. Browser tests use labelled model catalog fixtures.

## Launch and Improvement Iteration

Schema 4 adds launch preparation, completion modes, bounded parent-child improvement
and resource holds. Regression coverage includes source model inheritance, atomic
GPU hold transfer, continuation limits, cancellation during preparation, late agent
results, launch retries, unchanged CPU commands, retained tmux logs, and migrations
from schema 1/2/3. A browser test covers the improve-mode handoff and custom round limit.

Real local Luna verification artifacts:

- `/tmp/deepqueue-launch-return-live-03/verification.json`: skill-driven submission,
  a new launch task, one CPU execution, and the exact Markdown reply in the original
  conversation `01a077c4-3e04-7bd1-b573-6a38076d2a6c`.
- `/tmp/deepqueue-improvement-live-03/verification.json`: initial synthetic linear
  training, source-agent diagnosis and intercept addition, one priority-100 child,
  two completed tmux windows, both original-conversation summaries, and released
  resources. Validation MSE changed from `9.00000000122222` to `6.349603662288748e-09`.
  This is a small synthetic integration check, not evidence about a research model.
  Source: `01a077c7-64a9-76d1-83b1-255767a5b9e0`; parent:
  `b662aa56ce6b434184fd3bd85e2af70c`; child: `a4c2670d7c1b4bc7bffddd70923f7c50`.
- `/tmp/deepqueue-gpu-launch-live-01/verification.json`: real Luna prepared logical
  device flags `0,1,2,3` and four workers for physical `0,1,3,7`, preserving the output
  path. Telemetry was simulated and no GPU training was executed. Source launch task:
  `01a077c9-4e87-7d31-b647-9761ec32ff6f`.

Earlier diagnostic attempts exposed an ambiguous initialization prompt and launch
plans that changed CPU output paths/model structure. Those runs were not counted as
passes. The launch role now excludes continuation instructions, explicitly preserves
the current experiment, and rejects changes to CPU-only commands. tmux followers
drain output after supervisor exit and retain the last log page without a live follower.

Repeat with new local directories:

```bash
uv run scripts/launch_smoke.py --home /tmp/new-launch-check
uv run scripts/improvement_smoke.py --home /tmp/new-improvement-check --agent-sandbox danger-full-access
```

The latter permission profile applies only to the owned diagnostic conversation.
Runtime callbacks inherit source permissions and never override them.

## Parameter Matrix Iteration

The batch compiler now supports Cartesian parameter matrices, typed parameter
provenance, quoted argv commands, matching dependent experiments, and read-only
submission previews. Regression tests cover expansion limits, unknown template
fields, command quoting, dependency cycles, atomic rollback, and existing job
idempotency fingerprints. A colliding batch-owned idempotency key is rejected
instead of creating a batch with nonexistent job references.

The installed CLI previewed `examples/parameter-sweep.json` as 24 experiments:
12 training parameter combinations and 12 dependent evaluations. Preview did not
insert jobs into the operational queue.

`scripts/matrix_smoke.py` also ran against a new local state directory. Six
CPU-only jobs were submitted from one matrix, with an explicit limit of two
concurrent jobs. All six completed once, the observed peak concurrency was two,
and each generated result matched its stored learning-rate parameter. Resubmitting
the same manifest returned the existing batch. This test deliberately used explicit
CPU resources and disabled agent phases; it made zero model requests.

The run report is `/tmp/deepqueue-matrix-smoke-20260906-01/verification.json`.
Repeat the scheduling test with a new directory:

```bash
uv run python scripts/matrix_smoke.py --home /tmp/deepqueue-matrix-NEW_UNIQUE_DIRECTORY
```

The packaged, repository, and user skill copies have been updated together with
matrix and preview instructions. This iteration does not establish that the
upstream inference restriction described below has been resolved.

## Queue Controls and Dashboard

Schema version 2 adds transactional job/batch pause flags and authoritative batch
membership. Migration tests retained existing run IDs, GPU reservations, agent
links, and submission fingerprints, including concurrent upgrades and rollback
on inconsistent legacy membership. Control tests covered independently paused
jobs inside resumed batches, stale scheduler snapshots, admitted work continuing,
priority order for both execution and estimate agents, and atomic batch cancellation.

The React dashboard and local FastAPI service share these database operations with
the CLI. Browser tests changed priorities, persisted pause flags across reloads,
previewed and submitted a batch, started/stopped the isolated scheduler, and verified
one actual CPU command execution and its log. Real CPU artifact output was inspected
in the result panel. Agent record rendering used a clearly labelled `ui-test-fixture`
archive and did not call Luna. HTTP tests also covered original batch names with
slashes/query characters, expired telemetry, encrypted SSH password storage, hidden
environment values, and rejection of cross-origin mutations.

At that dashboard verification, the operational dashboard used `http://127.0.0.1:8765`
and queue home `/root/.local/share/deepqueue`. Its queue was empty, its scheduler was
running, and it read the eight-GPU hardware snapshot. Live browser checks found no
JavaScript or console errors, horizontal page overflow, or overflowing buttons at
1920, 768, 390, and 320 pixel widths. Desktop and mobile screenshots were inspected.
Examples of the live screenshots are `/tmp/deepqueue-live-resources-desktop.png`
and `/tmp/deepqueue-live-resources-mobile.png`. Test queues and fixtures are separate
from this operational queue.

Packaged, repository, and user skill copies include the dashboard entry point and
persistent controls. See [dashboard instructions](dashboard.md), including remote
access by SSH tunnel. This iteration does not verify successful Luna inference or
resolve the provider restriction below.

## Source Conversation Return

Real local `gpt-5.6-luna` verification passed using the existing app-server Unix
control socket and the truthful `deepqueue` client identity. No credentials or
provider settings were changed. The original standalone-process failure below is
retained as historical evidence; it does not describe the verified shared connection.

The complete skill-driven test is reproducible with:

```bash
uv run python scripts/return_smoke.py --home /tmp/deepqueue-return-NEW --via-agent
```

This container cannot create nested Bubblewrap namespaces. Its owned diagnostic
conversation was tested with the explicitly selected
`--agent-sandbox danger-full-access`, matching this development environment.
That option applies only to the test conversation, not to user conversations or
queue configuration. The earlier workspace-write attempt stopped before submission.

Verified artifacts: `/tmp/deepqueue-agent-skill-return-live-02/verification.json`,
`submission.md`, `agents/`, and `archives/` in the same isolated queue home.

- Source and archive thread: `01a07783-f1fa-74e0-a84b-0017a3b73d71`.
- Completion turn: `01a07785-0618-7c42-94fd-424d7636164f`.
- Experiment: `9da801050c8b44dc80f315fc22f145e8`; execution count exactly one.
- Luna read the installed skill and ran preview/submission using `--from-agent`.
  The recorded source ID equals that agent's own app-server conversation ID.
- The source retained a baseline of 0.50. Its completion reply compared the
  measured synthetic score 0.75 with that baseline, reporting a difference of 0.25.
- The summary in source history equals the archived Markdown exactly. No separate
  archive conversation was created, and the original experiment was not rerun.
- Resource estimation through the shared socket also passed real Luna inference:
  `/usr/bin/true` was estimated as zero GPUs, one CPU core, 128 MiB, confidence 0.99.
  Estimate thread: `01a0777d-3ac0-7c00-8324-d89ab91587c8`.

The 85 Python tests cover source binding, matrix propagation, migration from
schema 1 and 2, origin conflicts, per-source serialization and backoff, busy turns,
disconnect recovery, missing delivery acknowledgements, and recorded execution
evidence when the target server is unavailable. The four Playwright workflows
verify agent handoff without premature enqueueing, source links, Markdown tables,
desktop/mobile layout, queue controls, and real CPU output. Browser fixtures are
labelled `ui-test-fixture`; actual model inference is verified separately above.

The operational queue was migrated to schema 3, and its daemon/web service were
restarted. Both `agent_socket` and `return_agent_socket` are set to `auto`; the
model remains Luna. The operational queue contains no test experiments. The source,
repository-local, and installed user skills have identical contents.

## Earlier Standalone Inference Failure

A real Luna smoke test attempted two CPU-only experiment estimates through the
official local `codex app-server`. The configured provider is `https://threecode.cn`.
Both model requests were rejected with HTTP 403 and the message
`This account only allows Codex official clients`.

No experiment command ran in this smoke test. Jobs moved to `needs_review`, and
their failed model requests and real thread IDs were retained. One persistent
thread was independently retrieved with `thread/read`, confirming its failed turn:

- [Estimate task](codex://threads/01a0769e-1c33-73e3-bf37-43afa73400be)
- Resume: `codex resume 01a0769e-1c33-73e3-bf37-43afa73400be`

The diagnostic state is under `.test-state/luna-live-01`. There is no successful
`verification.json` for that run. Model-list visibility is not evidence that a
provider permits actual inference. Desktop deep-link navigation itself has not
been visually verified; thread persistence and API retrieval were verified.

This failure was observed on the standalone stdio route. That route has not been
retested after the shared-socket implementation. The separate legacy smoke test is:

```bash
uv run python scripts/live_smoke.py --home /tmp/deepqueue-luna-NEW_UNIQUE_DIRECTORY
```

The test creates a report only after both CPU experiments, both estimates, and
both archives succeed. It always selects `gpt-5.6-luna`.

## Local Installation

The `deepqueue` executable is installed using an editable uv tool environment.
The user skill is at `/root/.agents/skills/deepqueue`. The default queue home is
`/root/.local/share/deepqueue` on the local filesystem; the project directory is
an NFS mount and is not used for the operational queue database.

The empty local queue daemon was started and its heartbeat checked. It has no
user experiments. No existing remote server credentials or training commands were
registered or modified. SSH was verified against an isolated loopback fixture;
actual remote training servers still need to be registered.
