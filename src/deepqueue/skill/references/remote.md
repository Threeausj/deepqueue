# Public Queue Clients

The public host stores the queue database and connects to execution servers over
SSH. A submission client sends JSON over HTTPS. Commands, working directories,
context files, metrics and tmux windows belong to the bound execution server.

## Configure Once

Install the DeepQueue package on the submitting machine. The queue administrator
provides a server-specific submission token and the skill downloaded from that
server's access configuration. The archive contains the public URL and server ID,
but never a token.

```bash
deepqueue --url https://queue.example.com --target-server gpu-a client configure
deepqueue client show
deepqueue server list
deepqueue skill install --path ~/.agents/skills/deepqueue
```

The example URL and server must be replaced with the deployment's actual values.
Configuration prompts for the token without echoing it and writes a private
client profile at `~/.config/deepqueue/client.json`. For a secret managed in an
environment variable, use `client configure --token-env VARIABLE`; only the
variable name is stored. Do not print credentials or insert them into prompts.

`DEEPQUEUE_CLIENT_CONFIG` selects a different client profile. `--url` or
`DEEPQUEUE_URL` selects the public endpoint; `--target-server` or
`DEEPQUEUE_SERVER` selects the client binding. Saved credentials are never reused
for a different URL, and HTTP redirects are not followed. HTTPS is required
except for isolated loopback tests.

## Submit and Monitor

```bash
deepqueue batch preview /absolute/path/batch.json --from-agent
deepqueue batch submit /absolute/path/batch.json --from-agent
deepqueue job list
deepqueue job show JOB_ID
deepqueue job logs JOB_ID
deepqueue job links JOB_ID
deepqueue batch report BATCH_NAME
```

A server-specific skill may require its explicit `--url ... --target-server ...`
prefix on these commands. Keep that prefix for improved child submissions too.
`--from-agent` binds CODEX_THREAD_ID before sending the request. A missing server
is filled from the client binding; a conflicting server is rejected. The server
also enforces the credential's binding, independently of the client.

A paused, unreachable, or full execution server keeps its jobs there. A higher
priority affects that server's waiting work and never preempts another server's
training. Do not use the public host's filesystem as the experiment cwd.

## Source Task Return

The queue host must reach the Codex app-server which owns the original task.
The administrator can enable the execution server in the dashboard's **Codex
workspace → Connection settings**. This reuses the server's saved SSH password or
key and the official `codex app-server proxy`; `socket: auto` means the remote
user's shared Codex socket. The server must have a logged-in Codex installation
supporting app-server daemon/proxy (verified with 0.153.4). An absolute executable
path can be configured when Codex is absent from the SSH PATH.

The browser can open or create tasks on this server. Submit using this skill from
that actual task; its ID and execution server remain attached to the experiment.
The scheduler reconnects directly to the same server for feedback, including after
the browser disconnects. Model improvement runs in the original task with its
existing workspace permissions. The server-bound client credentials and this skill
must be available in the task's environment to submit a child to the public queue.
The administrator gateway token is not needed by submitting agents.

A manually forwarded socket configured as `return_agent_socket` still takes
precedence. It is an absolute path on the queue host, whereas `codex.socket` is a
path on the execution server. Do not swap these two meanings.

If Codex runs on the public host, use the queue's global return connection.
Do not guess a source task ID or switch to another app-server when a task cannot
be found. Report connection failures; a task ID alone does not identify its host.
