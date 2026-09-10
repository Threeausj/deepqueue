from __future__ import annotations

import re
import shlex
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

ACTIVE = ("starting", "running", "lost")
TERMINAL = ("succeeded", "failed", "cancelled", "blocked")
PRESTART = ("pending", "estimating", "needs_review", "queued")
STATUSES = (*PRESTART, *ACTIVE, *TERMINAL)
AGENT_PHASES = ("estimate", "launch", "archive")
AGENT_SETTING_FIELDS = {
    "model",
    "agent_models",
    "agent_effort",
    "agent_efforts",
    "launch_max_retries",
}
ModelId = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\s\x00-\x1f\x7f]+$")]
EffortId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")]


def service_url(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("Queue URL must be an HTTPS origin")
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or (":" in parsed.hostname and not re.fullmatch(r"[0-9a-fA-F:.]+", parsed.hostname))
        or (
            ":" not in parsed.hostname
            and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", parsed.hostname)
        )
        or (
            parsed.scheme != "https"
            and not (
                parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
            )
        )
    ):
        raise ValueError("Queue URL requires HTTPS; HTTP is allowed only on loopback")
    if parsed.port == 0:
        raise ValueError("Queue URL port must be between 1 and 65535")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def preview_url(value):
    value = service_url(value)
    if value is None:
        return None
    from ipaddress import ip_address

    try:
        ip_address(urlsplit(value).hostname)
    except ValueError:
        return value
    raise ValueError("预览地址请使用域名，例如 https://preview.example.com；本机可使用 localhost")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class AgentModels(Model):
    estimate: ModelId | None = None
    launch: ModelId | None = None
    archive: ModelId | None = None

    @field_validator("estimate", "launch")
    @classmethod
    def new_thread_model(cls, value):
        if value == "source-thread":
            raise ValueError("source-thread is only available for archive")
        return value


class AgentEfforts(Model):
    estimate: EffortId | None = None
    launch: EffortId | None = None
    archive: EffortId | None = None

    @field_validator("estimate", "launch")
    @classmethod
    def new_thread_effort(cls, value):
        if value == "source-thread":
            raise ValueError("source-thread effort is only available for archive")
        return value


class Resources(Model):
    gpu_count: int = Field(default=0, ge=0, le=256)
    gpu_memory_mib: int = Field(default=0, ge=0)
    gpu_type: str | None = None
    gpu_ids: list[str] = Field(default_factory=list)
    cpu_cores: int = Field(default=1, ge=1)
    ram_mib: int = Field(default=1024, ge=128)
    estimated_seconds: int = Field(default=3600, ge=1)

    @model_validator(mode="after")
    def valid_gpus(self):
        if len(set(self.gpu_ids)) != len(self.gpu_ids):
            raise ValueError("gpu_ids must be unique physical indexes or UUIDs")
        if self.gpu_ids and len(self.gpu_ids) != self.gpu_count:
            raise ValueError("gpu_ids length must equal gpu_count")
        if self.gpu_count == 0 and (self.gpu_memory_mib or self.gpu_type):
            raise ValueError("GPU memory/type requires gpu_count > 0")
        return self


class Estimate(Model):
    resources: Resources
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)
    evidence: list[str]
    warnings: list[str]
    code_issues: list[str] = Field(default_factory=list)
    needs_review: bool


class Analysis(Model):
    summary: str = Field(min_length=1)
    outcome: Literal["success", "failure", "cancelled", "inconclusive"]
    findings: list[str]
    resource_assessment: str
    next_steps: list[str]
    suggested_command: str | None


class LaunchFile(Model):
    path: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,240}$")
    content: str = Field(max_length=131072)

    @field_validator("path")
    @classmethod
    def relative_file(cls, value):
        if any(part in ("", ".", "..") for part in value.split("/")):
            raise ValueError("Launch files require plain relative paths")
        return value


class LaunchEnv(Model):
    name: str
    value: str


class LaunchPlan(Model):
    command: str = Field(min_length=1, max_length=65536)
    env: list[LaunchEnv] = Field(default_factory=list, max_length=128)
    files: list[LaunchFile] = Field(default_factory=list, max_length=16)
    rationale: str
    needs_review: bool = False

    @model_validator(mode="before")
    @classmethod
    def restore_known_executable(cls, data, info: ValidationInfo):
        reference = (info.context or {}).get("reference_command")
        if not reference or not isinstance(data, dict) or not isinstance(data.get("command"), str):
            return data
        try:
            expected = shlex.split(reference)[0]
            lexer = shlex.shlex(data["command"], posix=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            executable = lexer.get_token()
        except (ValueError, IndexError):
            return data
        if not executable:
            return data
        restored = executable.strip().replace("\\/", "/")
        if restored != executable and restored == expected and expected.startswith("/"):
            # Only restore a known path; retain the rest of the shell command verbatim.
            data = dict(data)
            end = lexer.instream.tell()
            if end and data["command"][end - 1] in lexer.whitespace:
                end -= 1
            data["command"] = shlex.quote(expected) + data["command"][end:]
            if isinstance(data.get("rationale"), str):
                data["rationale"] += (
                    "\n已自动恢复与原命令一致的解释器路径，移除生成时多余的空格或斜杠转义。"
                )
        return data

    @model_validator(mode="after")
    def valid_command(self):
        JobSpec.valid_command(self.command)
        try:
            words = shlex.split(self.command)
        except ValueError:
            words = []
        if words and words[0] != words[0].strip():
            raise ValueError("Executable contains leading/trailing whitespace; preserve its path")
        if words and "\\/" in words[0]:
            raise ValueError(
                "Executable contains literal backslashes before slashes; "
                "preserve the original executable path without JSON-style slash escaping"
            )
        JobSpec.valid_env({item.name: item.value for item in self.env})
        if len({item.name for item in self.env}) != len(self.env):
            raise ValueError("Launch environment names must be unique")
        if len({file.path for file in self.files}) != len(self.files):
            raise ValueError("Launch file paths must be unique")
        return self


class JobSpec(Model):
    server: str = Field(min_length=1)
    command: str = Field(min_length=1, max_length=65536)
    cwd: str = Field(min_length=1)
    title: str = Field(default="Command", min_length=1, max_length=200)
    group: str | None = Field(default=None, max_length=200)
    priority: int = Field(default=0, ge=-100, le=100)
    env: dict[str, str] = Field(default_factory=dict)
    context_files: list[str] = Field(default_factory=list, max_length=16)
    artifact_files: list[str] = Field(default_factory=list, max_length=16)
    intent: str = Field(default="", max_length=16000)
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)
    resources: Resources | None = None
    skip_estimate: bool = False
    archive: bool = True
    agent_models: AgentModels = Field(default_factory=AgentModels)
    agent_efforts: AgentEfforts = Field(default_factory=AgentEfforts)
    completion_mode: Literal["finish", "improve"] = "finish"
    max_improvement_rounds: int = Field(default=1, ge=1, le=100)
    improvement_round: int = Field(default=0, ge=0, le=100)
    parent_job_id: str | None = None
    baseline_job_id: str | None = Field(default=None, min_length=1, max_length=128)
    launch_max_retries: int | None = Field(default=None, ge=0, le=10)
    launch_agent: bool = True
    tmux: bool = True
    source_thread_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    timeout_seconds: int = Field(default=0, ge=0)
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("cwd")
    @classmethod
    def absolute_cwd(cls, value):
        if not value.startswith("/") or "\x00" in value:
            raise ValueError("cwd must be an absolute path on the target server")
        return value

    @field_validator("command")
    @classmethod
    def valid_command(cls, value):
        if "\x00" in value or not value.strip():
            raise ValueError("command must be nonempty and contain no NUL")
        if re.search(r"\b(?:CUDA_VISIBLE_DEVICES|NVIDIA_VISIBLE_DEVICES)\s*=", value):
            raise ValueError("Move explicit GPU selection into resources.gpu_ids")
        return value

    @field_validator("env")
    @classmethod
    def valid_env(cls, value):
        reserved = {
            "CUDA_VISIBLE_DEVICES",
            "NVIDIA_VISIBLE_DEVICES",
            "DEEPQUEUE_RUN_ID",
            "DEEPQUEUE_RUN_DIR",
        }
        for key, item in value.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\x00" in item:
                raise ValueError("invalid environment variable")
            if key in reserved:
                raise ValueError(f"{key} is owned by the scheduler; use resources.gpu_ids")
        return value

    @model_validator(mode="after")
    def explicit_resources(self):
        if self.skip_estimate and self.resources is None:
            raise ValueError("skip_estimate requires explicit resources")
        if self.source_thread_id and not self.archive:
            raise ValueError("source_thread_id requires archive=true to return the result")
        if self.agent_models.archive == "source-thread" and not self.source_thread_id:
            raise ValueError("archive model source-thread requires source_thread_id")
        if self.agent_efforts.archive == "source-thread" and not self.source_thread_id:
            raise ValueError("archive effort source-thread requires source_thread_id")
        if self.completion_mode == "improve" and (not self.source_thread_id or not self.archive):
            raise ValueError("improve mode requires a source conversation and archive=true")
        return self


class JobUpdate(Model):
    """Mutable controls; omitted fields retain their current values."""

    completion_mode: Literal["finish", "improve"] | None = None
    max_improvement_rounds: int | None = Field(default=None, ge=1, le=100)
    agent_models: AgentModels | None = None
    agent_efforts: AgentEfforts | None = None
    launch_max_retries: int | None = Field(default=None, ge=0, le=10)
    baseline_job_id: str | None = Field(default=None, min_length=1, max_length=128)
    intent: str | None = Field(default=None, max_length=16000)
    command: str | None = Field(default=None, min_length=1, max_length=65536)
    resources: Resources | None = None
    context_files: list[str] | None = Field(default=None, max_length=16)
    artifact_files: list[str] | None = Field(default=None, max_length=16)
    parameters: dict[str, str | int | float | bool] | None = None

    @model_validator(mode="after")
    def explicit_values(self):
        for field in self.model_fields_set - {"baseline_job_id", "launch_max_retries"}:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null; omit it to keep the current value")
        return self


class CodexConnection(Model):
    enabled: bool = False
    executable: str = Field(default="codex", min_length=1, max_length=4096)
    socket: str = Field(default="auto", min_length=1, max_length=4096)
    cwd: str | None = Field(default=None, max_length=4096)

    @field_validator("executable", "socket", "cwd")
    @classmethod
    def plain_path(cls, value, info):
        if value is not None and any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Codex paths cannot contain control characters")
        if info.field_name == "socket" and value != "auto" and not value.startswith("/"):
            raise ValueError("Codex socket must be auto or an absolute path on that server")
        if info.field_name == "cwd" and value is not None and not value.startswith("/"):
            raise ValueError("Codex working directory must be absolute on that server")
        if info.field_name == "executable" and value.startswith("-"):
            raise ValueError("Invalid Codex executable")
        return value


class Server(Model):
    name: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    sort_order: int = Field(default=0, ge=0)
    kind: Literal["local", "ssh"] = "local"
    host: str | None = None
    port: int = Field(default=22, ge=1, le=65535)
    username: str | None = None
    key_file: str | None = None
    password_ref: str | None = None
    passphrase_ref: str | None = None
    worker_root: str = ".local/share/deepqueue-worker"
    python: str = "python3"
    enabled: bool = True
    max_running: int = Field(default=8, ge=1)
    reserved_ram_mib: int = Field(default=1024, ge=0)
    gpu_idle_utilization: int = Field(default=10, ge=0, le=100)
    gpu_idle_memory_mib: int = Field(default=512, ge=0)
    gpu_allowlist: list[str] = Field(default_factory=list)
    return_agent_socket: str | None = None
    codex: CodexConnection = Field(default_factory=CodexConnection)

    @field_validator("display_name")
    @classmethod
    def valid_display_name(cls, value):
        if value is not None:
            value = value.strip()
            if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("服务器名称不能为空或包含控制字符")
        return value

    @field_validator("return_agent_socket")
    @classmethod
    def callback_socket(cls, value):
        if value is not None and (not value.startswith("/") or "\x00" in value):
            raise ValueError("Server callback socket must be an absolute path on the queue host")
        return value

    @model_validator(mode="after")
    def valid_connection(self):
        if self.kind == "ssh" and (not self.host or not self.username):
            raise ValueError("SSH servers require host and username")
        if self.password_ref and self.key_file:
            raise ValueError("choose password authentication or a key file")
        return self


class Settings(Model):
    public_url: str | None = None
    preview_origin: str | None = None
    agent_command: list[str] = Field(default_factory=lambda: ["codex", "app-server"])
    agent_socket: str | None = None
    return_agent_socket: str | None = "auto"
    model: ModelId = "gpt-5.6-luna"
    agent_models: AgentModels = Field(default_factory=AgentModels)
    agent_effort: EffortId = "medium"
    agent_efforts: AgentEfforts = Field(default_factory=AgentEfforts)
    agent_timeout_seconds: int = Field(default=300, ge=10)
    agent_max_attempts: int = Field(default=3, ge=1, le=10)
    launch_max_retries: int = Field(default=3, ge=0, le=10)
    agent_retry_seconds: int = Field(default=30, ge=1)
    max_agents: int = Field(default=2, ge=1, le=16)
    min_confidence: float = Field(default=0.65, ge=0, le=1)
    poll_seconds: float = Field(default=10, ge=0.1)
    snapshot_max_age_seconds: float = Field(default=30, ge=1)
    aging_seconds: int = Field(default=600, ge=1)
    improvement_hold_seconds: int = Field(default=900, ge=30, le=86400)
    link_template: str = "codex://threads/{thread_id}"

    _public_url = field_validator("public_url")(service_url)
    _preview_origin = field_validator("preview_origin")(preview_url)

    def model_for(self, phase: str, spec: JobSpec) -> str | None:
        if phase not in AGENT_PHASES:
            raise ValueError(f"Unknown agent phase: {phase}")
        selected = getattr(spec.agent_models, phase) or getattr(self.agent_models, phase)
        if phase == "archive" and selected in (None, "source-thread"):
            if spec.source_thread_id:
                return None
            selected = None
        return selected or self.model

    def effort_for(self, phase: str, spec: JobSpec) -> str | None:
        if phase not in AGENT_PHASES:
            raise ValueError(f"Unknown agent phase: {phase}")
        selected = getattr(spec.agent_efforts, phase) or getattr(self.agent_efforts, phase)
        if phase == "archive" and selected in (None, "source-thread"):
            if spec.source_thread_id:
                return None
            selected = None
        return selected or self.agent_effort

    @field_validator("agent_effort")
    @classmethod
    def concrete_effort(cls, value):
        return AgentEfforts.new_thread_effort(value)

    @field_validator("model")
    @classmethod
    def concrete_model(cls, value):
        return AgentModels.new_thread_model(value)

    @field_validator("agent_command")
    @classmethod
    def command_nonempty(cls, value):
        if not value or not all(value):
            raise ValueError("agent_command must be a nonempty argv array")
        return value

    @field_validator("agent_socket", "return_agent_socket")
    @classmethod
    def valid_socket(cls, value):
        if value is not None and value != "auto" and not value.startswith("/"):
            raise ValueError("app-server socket must be an absolute path, auto, or null for stdio")
        return value

    @field_validator("link_template")
    @classmethod
    def valid_template(cls, value):
        if "{thread_id}" not in value:
            raise ValueError("link_template must contain {thread_id}")
        value.format(thread_id="test")
        return value
