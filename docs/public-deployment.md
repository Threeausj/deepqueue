# Public Deployment

For Docker/Compose, follow [Docker deployment](docker-deployment.md). It includes
the application image, optional automatic HTTPS, persistent queue/Codex volumes,
and a lifecycle wrapper compatible with the dashboard's scheduler controls.

The public host runs the dashboard/API, SQLite queue, and one scheduler. It reaches
registered execution servers over SSH using their configured password or key.
Submission clients send manifests over HTTPS; they do not open the queue database.

Every job has an immutable execution server. Resource snapshots, GPU leases,
concurrency limits, queue positions, and improvement holds are scoped to that server.
An unavailable or busy server keeps its jobs waiting there. A server's idle GPUs do
not satisfy a different server's jobs. Improvement children retain their parent's
server and original Codex task. Batch names remain unique across the shared library.

## Public Host

Use Linux with Python 3.10+, a local disk for state, and an HTTPS reverse proxy.
The supplied systemd units expect the application at `/opt/deepqueue`, its Python
environment at `/opt/deepqueue/.venv`, and state at `/var/lib/deepqueue`, owned by a
`deepqueue` account. Adjust the units when using different installation paths/users.
Install the built wheel into that environment; it includes the frontend and skill.

Run the following as the service account with its own Codex installation/login:

```bash
deepqueue --home /var/lib/deepqueue init
deepqueue --home /var/lib/deepqueue access create --admin --label initial-admin
deepqueue --home /var/lib/deepqueue config set public_url https://queue.example.com
deepqueue --home /var/lib/deepqueue config set agent_socket auto
deepqueue --home /var/lib/deepqueue config set return_agent_socket auto
```

Replace the example domain with the real HTTPS origin, without an API suffix or
path prefix. The administrator token is emitted once by `access create`; retain
it in the operator's secret store. The queue saves only its digest. A replacement
administrator token must exist before the last existing one can be revoked.

Alternatively, start a loopback dashboard and set **通用设置 > 公网接入**. The first
save generates an administrator token, displays it once, and signs in that browser.
Later visits require the administrator token. Server-scoped tokens cannot sign in
to the administration dashboard. Disabling the public URL does not remove tokens
or turn off authentication.

Install `deploy/deepqueue-web.service` and `deploy/deepqueue-scheduler.service` into
the system service directory, then reload systemd and start both units. Run exactly
one scheduler for this queue. The scheduler unit uses `daemon run`; do not additionally
start a detached scheduler with the CLI. With systemd, manage lifecycle through
systemd; the dashboard start/stop controls are intended for CLI-managed deployments.

The Nginx example in `deploy/nginx.conf.example` assumes a certificate already
provisioned for the real domain. Substitute the domain and certificate paths, check
the configuration with `nginx -t`, and reload the proxy. Keep the upstream on
`127.0.0.1:8765`; only HTTPS needs public exposure. Nginx preserves the original Host
and forwards the request scheme so login cookies are Secure. See the official
[Nginx proxy documentation](https://nginx.org/en/docs/http/ngx_http_proxy_module.html).
The application accepts its configured hostname and loopback hosts and rejects
cross-origin mutations. API calls require an administrator or server token.

## Execution Servers

Add each server in **服务器资源**, supplying SSH host, username, authentication,
and a verified host fingerprint. A key file path is on the public queue host.
Password and encrypted-key authentication are supported; stored secrets remain
encrypted in that queue's private state. Execution servers need Bash, Python 3.10+,
tmux, and NVIDIA drivers/`nvidia-smi` when using GPUs. Dataset and project paths must
exist on the execution server.

The public host's automatically registered `local` server refers to the public
host itself. Pause it when that machine should only coordinate remote training:

```bash
deepqueue --home /var/lib/deepqueue server pause local
```

Each server's **接入配置** provides its submission token, a downloadable skill ZIP,
and source-task callback configuration. Token creation displays the secret once;
the list shows only token identifiers and labels. Revocation takes effect on the
next request. Tokens for `gpu-a` cannot read/control `gpu-b` jobs or submit mixed
server batches, even when bypassing the CLI.

## Submission Machines

Install the DeepQueue Python package on each machine running the submitting Codex
agent. Configure its public endpoint and server-specific token once:

```bash
deepqueue --url https://queue.example.com --target-server gpu-a client configure
deepqueue client show
deepqueue server list
deepqueue skill install --path ~/.agents/skills/deepqueue
```

The configure command prompts for a token, or reads a named environment variable
with `--token-env NAME`. The private client profile is
`~/.config/deepqueue/client.json`; `DEEPQUEUE_CLIENT_CONFIG` selects another profile.
An environment-backed profile stores only the variable name. The generated skill
names the actual public URL and bound server. The dashboard ZIP contains the same
skill plus a non-secret `deployment.json`; it can be extracted into the machine's
skill directory instead of running `skill install`. Existing skill destinations
are not overwritten automatically.

Submissions now use the configured remote queue:

```bash
deepqueue batch preview /project/round.json --from-agent
deepqueue batch submit /project/round.json --from-agent
deepqueue job list
deepqueue batch report gpu-a-round-01
```

`--from-agent` binds the real local `CODEX_THREAD_ID` before the JSON is sent. Missing
server values inherit the client binding; conflicting values are rejected. Use a
server prefix on batch names. A timeout never causes fallback to a local queue.
Reusing the same batch manifest/origin is idempotent; inspect the batch after an
uncertain submission rather than creating a new name. Use `--idempotency-key` for
single submissions that must tolerate an uncertain response.

Explicit `--url URL --target-server SERVER` overrides profile routing. Saved tokens
are not sent to a different URL, and the HTTP client does not follow redirects.
`--home PATH` explicitly selects local operator mode and cannot be combined with
`--url`. Queue administration, SSH registration, and lost-run resolution remain
operator tasks on the queue host or in the authenticated dashboard.

## Returning to Codex

The public queue must connect to the app-server that owns the source task. Task IDs
do not identify machines. There are two supported arrangements:

- Codex on the public host: use the global `return_agent_socket`, normally `auto`,
  and run the queue under the same account/Codex home.
- Codex on an execution server: enable that server in **Codex 工作区 → 连接设置**.
  The gateway and scheduler use the stored SSH credentials and official app-server
  proxy. See [Codex workspace](codex-workspace.md) for installation, web controls,
  CLI/API and SSE reverse proxy settings. A manually forwarded Unix socket remains
  available as an explicit callback override.

For manual forwarding, run this as the queue service account and keep the
forward running. Substitute the actual remote username and Codex socket path:

```bash
mkdir -p /var/lib/deepqueue/sockets
ssh -NT -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o StreamLocalBindUnlink=yes -o StreamLocalBindMask=0177 -L /var/lib/deepqueue/sockets/gpu-a.sock:/home/train/.codex/app-server-control/app-server-control.sock train@gpu-a
```

Register the forwarded socket in another shell:

```bash
deepqueue --home /var/lib/deepqueue server callback gpu-a --socket /var/lib/deepqueue/sockets/gpu-a.sock
```

OpenSSH supports local Unix-socket forwarding through `-L`; see the official
[SSH manual](https://man.openbsd.org/ssh#L). Password or key authentication can be
used interactively; use an appropriate managed key/agent for a supervised persistent
forward. The remote Codex app-server must already be running. DeepQueue does not
create this SSH forward automatically or expose the app-server on a public TCP port.
Explicit per-server callback sockets take precedence over enabled server Codex
connections, which take precedence over the global callback connection. Estimation
and launch analysis still use the queue's configured agent connection and defaults.

For Codex on a laptop, forward its owning app-server to the queue host and assign
that queue-host socket as the relevant callback connection. A server currently has
one callback endpoint: submissions to it must originate from that app-server. Multiple
independent source app-servers for the same execution server require distinct future
origin routing; this version does not infer or discover those hosts.

The original task also needs its configured remote client and skill for authorized
improvement. Completion prompts use the public endpoint and the unchanged server
when requesting a high-priority child. A missing connection or unknown task remains
a visible archive failure; results are not redirected to a different task.

## State and Upgrades

Preserve the complete queue home, including `queue.sqlite3`, `config.json`,
`access.json`, `known_hosts`, the encrypted credential store/key, and execution/archive
records. Preserve client profiles separately. Stop the queue's web/scheduler before
an application upgrade or SQLite backup, then run `deepqueue --home ... init` and
restart them. Detached training workers continue on their execution servers.
The initialization command applies any database migrations required by the installed
version; keep the pre-upgrade state backup when retaining an older image or wheel.
