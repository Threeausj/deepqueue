import React, { useEffect, useLayoutEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Archive,
  ArrowDown,
  ArrowUpRight,
  Check,
  Copy,
  Download,
  FileText,
  MessageSquare,
  Pause,
  Play,
  RefreshCw,
  Square,
  Terminal,
  X,
} from "lucide-react";
import { active, api, download, idPath, terminal, waiting } from "./api.js";
import { agentPhases, effortLabel, modelLabel } from "./AgentModels.jsx";
import {
  archiveStates,
  duration,
  Empty,
  IconButton,
  memory,
  Priority,
  Stamp,
  Status,
} from "./components.jsx";

function AgentRecord({ agent }) {
  const link = /^(codex:\/\/|https?:\/\/)/.test(agent.deep_link || "")
    ? agent.deep_link
    : null;
  return (
    <article className="agent-record">
      <div className="agent-heading">
        <span>
          {{ estimate: "资源预估", launch: "启动适配", archive: "实验归档" }[
            agent.phase
          ] || agent.phase}
        </span>
        <span
          className={`agent-result ${agent.status === "failed" ? "error-text" : ""}`}
        >
          {{
            running: "分析中",
            completed: "已完成",
            failed: "调用失败",
            deferred: "等待对话",
          }[agent.status] || agent.status}
        </span>
      </div>
      <div className="subtle">
        {modelLabel(agent.model)} <span className="inline-separator">·</span>{" "}
        {agent.effort && (
          <>
            {effortLabel(agent.effort)}{" "}
            <span className="inline-separator">·</span>{" "}
          </>
        )}
        <Stamp value={agent.created_at} />
      </div>
      {agent.error && <pre className="agent-error">{agent.error}</pre>}
      {link && (
        <a className="thread-link" href={link}>
          <ArrowUpRight size={15} />在 Codex 中打开
          <span>{agent.thread_id?.slice(0, 8)}</span>
        </a>
      )}
      {agent.thread_id && (
        <div className="resume-command">
          <code>codex resume {agent.thread_id}</code>
          <IconButton
            icon={Copy}
            label="复制恢复命令"
            onClick={() =>
              navigator.clipboard
                .writeText(`codex resume ${agent.thread_id}`)
                .catch(() => {})
            }
          />
        </div>
      )}
    </article>
  );
}

export default function Detail({
  id,
  onClose,
  onControl,
  busy,
  updatedAt,
  onServer,
  initialTab,
  onTabChange,
  serverName,
}) {
  const panel = useRef(null);
  const detailContent = useRef(null);
  const logOutput = useRef(null);
  const followLogs = useRef(true);
  const [followingLogs, setFollowingLogs] = useState(true);
  const [job, setJob] = useState(null);
  const [tab, setTab] = useState("overview");
  const [error, setError] = useState("");
  const [logs, setLogs] = useState(null);
  const [logRun, setLogRun] = useState("");
  const [files, setFiles] = useState(null);
  const [runtime, setRuntime] = useState(null);
  const [tmux, setTmux] = useState(null);
  const [resources, setResources] = useState("");
  const [copied, setCopied] = useState(false);
  const [rounds, setRounds] = useState(null);
  useEffect(() => {
    if (job?.id === id) onServer?.(job.server);
  }, [id, job?.id, job?.server, onServer]);
  useEffect(() => {
    setJob(null);
    setTab(initialTab || "overview");
    setLogs(null);
    setLogRun("");
    setFiles(null);
    setRuntime(null);
    setTmux(null);
    setError("");
    setResources("");
    setRounds(null);
  }, [id]);
  useEffect(() => {
    if (["overview", "logs", "results", "archive"].includes(initialTab))
      setTab(initialTab);
  }, [initialTab]);
  useEffect(() => {
    followLogs.current = true;
    setFollowingLogs(true);
    if (detailContent.current) detailContent.current.scrollTop = 0;
  }, [id, tab, logRun]);
  useLayoutEffect(() => {
    if (tab === "logs" && followLogs.current && logOutput.current)
      logOutput.current.scrollTop = logOutput.current.scrollHeight;
  }, [logs, tab, logRun]);
  function latestLogs() {
    followLogs.current = true;
    setFollowingLogs(true);
    requestAnimationFrame(() => {
      if (logOutput.current)
        logOutput.current.scrollTop = logOutput.current.scrollHeight;
    });
  }
  function showTab(value) {
    if (value === "logs") latestLogs();
    if (detailContent.current) detailContent.current.scrollTop = 0;
    setTab(value);
    onTabChange?.(value);
  }
  useEffect(() => {
    const controller = new AbortController();
    let timer;
    async function poll() {
      try {
        const next = await api(`/jobs/${idPath(id)}`, undefined, {
          signal: controller.signal,
        });
        setJob(next);
        setResources(
          (current) => current || JSON.stringify(next.resources || {}, null, 2),
        );
      } catch (error) {
        if (error.name !== "AbortError") setError(error.message);
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 3000);
    }
    poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [id, updatedAt]);
  useEffect(() => {
    const previous = document.activeElement;
    panel.current.querySelector("button")?.focus();
    const onKey = (event) => {
      if (document.querySelector("dialog[open]")) return;
      if (event.key === "Escape") onClose();
      if (event.key === "Tab") {
        const nodes = [
          ...panel.current.querySelectorAll(
            "button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea, summary, [tabindex='0']",
          ),
        ];
        const first = nodes[0],
          last = nodes.at(-1);
        if (event.shiftKey && document.activeElement === first) {
          last?.focus();
          event.preventDefault();
        } else if (!event.shiftKey && document.activeElement === last) {
          first?.focus();
          event.preventDefault();
        }
      }
    };
    addEventListener("keydown", onKey);
    return () => {
      removeEventListener("keydown", onKey);
      previous?.focus();
    };
  }, [id]);
  useEffect(() => {
    if (!job) return;
    const controller = new AbortController();
    let timer;
    async function poll() {
      try {
        const options = { signal: controller.signal };
        if (tab === "logs") {
          const query = logRun ? `?run_id=${idPath(logRun)}` : "";
          setLogs(
            await api(`/jobs/${idPath(id)}/logs${query}`, undefined, options),
          );
          setTmux(
            await api(
              `/jobs/${idPath(id)}/terminal${query}`,
              undefined,
              options,
            ),
          );
        }
        if (tab === "results")
          setFiles(
            (await api(`/jobs/${idPath(id)}/artifacts`, undefined, options))
              .files,
          );
        if (active.includes(job.status))
          setRuntime(
            await api(`/jobs/${idPath(id)}/runtime`, undefined, options),
          );
      } catch (error) {
        if (error.name !== "AbortError") setError(error.message);
      }
      if (!controller.signal.aborted && active.includes(job.status))
        timer = setTimeout(poll, 3000);
    }
    poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [id, tab, job?.status, job?.run_id, logRun]);
  async function control(body) {
    try {
      setError("");
      const result = await onControl(id, body);
      if (result) {
        setJob(result);
        if (body.action === "mode") setRounds(null);
      }
    } catch (error) {
      setError(error.message);
    }
  }
  const run = job?.runs?.at(-1);
  const result = run?.result || runtime;
  const metrics = result?.metrics;
  const analysis = job?.analysis;
  const source = job?.spec.source_thread_id;
  const lastArchive = job?.agents
    .filter((agent) => agent.phase === "archive")
    .at(-1);
  const returnState = !terminal.includes(job?.status)
    ? "等待实验结束"
    : job.archive_status === "completed"
      ? "原对话已总结"
      : job.archive_status === "failed"
        ? "回传失败"
        : job.archive_status === "running"
          ? "原对话总结中"
          : lastArchive?.status === "deferred"
            ? "等待来源对话"
            : "待回传原对话";
  return (
    <>
      <div className="detail-scrim" onClick={onClose} />
      <aside
        ref={panel}
        className={`detail-panel ${tab === "logs" ? "showing-logs" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label="实验详情"
      >
        <header className="detail-heading">
          <div>
            <span className="eyebrow">实验详情</span>
            <span className="detail-id">{id.slice(0, 12)}</span>
          </div>
          <IconButton icon={X} label="关闭实验详情" onClick={onClose} />
        </header>
        {error && (
          <div className="notice error-text" role="alert">
            {error}
          </div>
        )}
        {!job ? (
          <Empty title="正在读取实验" />
        ) : (
          <>
            <div className="detail-intro">
              <h2>{job.spec.title}</h2>
              <div>
                <Status
                  status={job.status}
                  held={job.pause_sources.length > 0}
                />
                <span className="subtle">{serverName || job.server}</span>
                <span className="subtle">{job.batch_name || "独立实验"}</span>
              </div>
            </div>
            {source && (
              <div className="source-conversation">
                <a
                  className="thread-link"
                  href={
                    /^(codex:\/\/|https?:\/\/)/.test(job.source_link || "")
                      ? job.source_link
                      : undefined
                  }
                >
                  <MessageSquare size={15} />
                  来源对话
                  <ArrowUpRight size={14} />
                </a>
                <a
                  className="thread-link"
                  href={`#${new URLSearchParams({ view: "codex", server: job.server, thread: source })}`}
                >
                  网页打开
                  <ArrowUpRight size={14} />
                </a>
                <span>{returnState}</span>
                <code>{source}</code>
              </div>
            )}
            <div className="detail-controls">
              <button
                type="button"
                className="button primary log-open-button"
                disabled={!job.runs.length}
                onClick={() => {
                  if (logRun) {
                    setLogRun("");
                    setLogs(null);
                    setTmux(null);
                  }
                  showTab("logs");
                }}
                title={
                  job.runs.length
                    ? "打开训练输出并跳到最新结果"
                    : "实验启动后可查看日志"
                }
              >
                <Terminal size={16} />
                实时日志 · tmux
              </button>
              <Priority
                value={job.priority}
                disabled={busy || !waiting.includes(job.status)}
                onChange={(priority) =>
                  control({ action: "priority", priority })
                }
              />
              <div className="row-actions">
                {waiting.includes(job.status) && (
                  <IconButton
                    icon={job.paused ? Play : Pause}
                    label={job.paused ? "恢复实验" : "暂停实验"}
                    disabled={busy}
                    onClick={() =>
                      control({ action: job.paused ? "resume" : "pause" })
                    }
                  />
                )}
                {!terminal.includes(job.status) && (
                  <IconButton
                    icon={Square}
                    label="取消实验"
                    disabled={busy}
                    onClick={() => {
                      if (confirm(`取消实验「${job.spec.title}」？`))
                        control({ action: "cancel" });
                    }}
                  />
                )}
                <IconButton
                  icon={Download}
                  label="下载实验记录"
                  onClick={() => download(`${id}.json`, job)}
                />
              </div>
            </div>
            <div className="completion-controls">
              <div className="segmented" role="group" aria-label="运行结束后">
                {[
                  ["finish", "结束"],
                  ["improve", "继续改进"],
                ].map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={job.completion_mode === value}
                    className={job.completion_mode === value ? "active" : ""}
                    disabled={
                      busy ||
                      (value === "improve" &&
                        (!source ||
                          (terminal.includes(job.status) &&
                            (!["succeeded", "failed"].includes(job.status) ||
                              job.archive_status !== "pending")) ||
                          (job.spec.improvement_round || 0) >=
                            Number(
                              rounds ?? job.spec.max_improvement_rounds ?? 1,
                            )))
                    }
                    onClick={() =>
                      control({
                        action: "mode",
                        completion_mode: value,
                        ...(value === "improve" && rounds !== null
                          ? { max_improvement_rounds: Number(rounds) }
                          : {}),
                      })
                    }
                  >
                    {label}
                  </button>
                ))}
              </div>
              <span className="subtle">
                改进 {job.spec.improvement_round || 0} /{" "}
                {job.spec.max_improvement_rounds || 1} 轮
              </span>
              {source &&
                !job.improvement_child &&
                (!terminal.includes(job.status) ||
                  job.archive_status === "pending") && (
                  <form
                    className="round-controls"
                    onSubmit={async (event) => {
                      event.preventDefault();
                      await control({
                        action: "mode",
                        completion_mode: job.completion_mode,
                        max_improvement_rounds: Number(
                          rounds ?? job.spec.max_improvement_rounds ?? 1,
                        ),
                      });
                    }}
                  >
                    <label>
                      最多改进轮数
                      <input
                        type="number"
                        min={Math.max(1, (job.spec.improvement_round || 0) + 1)}
                        max="100"
                        required
                        value={rounds ?? job.spec.max_improvement_rounds ?? 1}
                        onChange={(event) => setRounds(event.target.value)}
                      />
                    </label>
                    <button type="submit" disabled={busy || rounds === null}>
                      保存轮数
                    </button>
                  </form>
                )}
            </div>
            <div
              className="tabs detail-tabs"
              role="tablist"
              aria-label="实验详情视图"
            >
              {[
                ["overview", "概览"],
                ["logs", "日志"],
                ["results", "结果"],
                ["archive", "Agent 归档"],
              ].map(([key, label]) => (
                <button
                  key={key}
                  role="tab"
                  aria-selected={tab === key}
                  className={tab === key ? "active" : ""}
                  onClick={() => showTab(key)}
                >
                  {label}
                </button>
              ))}
            </div>
            <div
              ref={detailContent}
              className={`detail-content ${tab === "logs" ? "detail-content-logs" : ""}`}
            >
              {tab === "overview" && (
                <>
                  {job.pause_sources.length > 0 && (
                    <div className="notice">
                      暂停范围：
                      {job.pause_sources
                        .map(
                          (scope) =>
                            ({
                              job: "当前实验",
                              batch: "所属批次",
                              server: "目标服务器",
                            })[scope],
                        )
                        .join("、")}
                    </div>
                  )}
                  {job.reason && <div className="notice">{job.reason}</div>}
                  {job.resource_hold && (
                    <div className="notice">
                      资源保留至 <Stamp value={job.resource_hold.expires_at} />
                      ，等待改进实验
                    </div>
                  )}
                  {(job.spec.parent_job_id || job.improvement_child) && (
                    <dl className="properties">
                      {job.spec.parent_job_id && (
                        <div>
                          <dt>上一轮实验</dt>
                          <dd>
                            <a
                              className="text-link"
                              href={`#view=queue&server=${idPath(job.server)}&job=${job.spec.parent_job_id}`}
                            >
                              {job.spec.parent_job_id.slice(0, 12)}
                            </a>
                          </dd>
                        </div>
                      )}
                      {job.improvement_child && (
                        <div>
                          <dt>改进实验</dt>
                          <dd>
                            <a
                              className="text-link"
                              href={`#view=queue&server=${idPath(job.server)}&job=${job.improvement_child.id}`}
                            >
                              {job.improvement_child.id.slice(0, 12)}
                            </a>
                            <Status status={job.improvement_child.status} />
                          </dd>
                        </div>
                      )}
                    </dl>
                  )}
                  <section className="detail-section">
                    <div className="detail-section-heading">
                      <h3>
                        <Terminal size={15} />
                        运行命令
                      </h3>
                      <IconButton
                        icon={copied ? Check : Copy}
                        label="复制命令"
                        onClick={async () => {
                          try {
                            await navigator.clipboard.writeText(
                              job.spec.command,
                            );
                            setCopied(true);
                            setTimeout(() => setCopied(false), 1500);
                          } catch (error) {
                            setError(error.message);
                          }
                        }}
                      />
                    </div>
                    <pre className="command-block">{job.spec.command}</pre>
                    <dl className="properties">
                      <div>
                        <dt>工作目录</dt>
                        <dd className="monospace">{job.spec.cwd}</dd>
                      </div>
                      <div>
                        <dt>提交时间</dt>
                        <dd>
                          <Stamp value={job.created_at} />
                        </dd>
                      </div>
                      <div>
                        <dt>依赖实验</dt>
                        <dd>
                          {job.spec.depends_on.length
                            ? job.spec.depends_on.map((dependency) => (
                                <a
                                  className="text-link"
                                  key={dependency}
                                  href={`#view=queue&job=${dependency}`}
                                >
                                  {dependency.slice(0, 12)}
                                </a>
                              ))
                            : "无"}
                        </dd>
                      </div>
                      {job.spec.intent && (
                        <div>
                          <dt>实验目标</dt>
                          <dd>{job.spec.intent}</dd>
                        </div>
                      )}
                    </dl>
                  </section>
                  <section className="detail-section">
                    <h3>Agent 模型</h3>
                    <dl className="properties">
                      {agentPhases.map(([phase, label]) => (
                        <div key={phase}>
                          <dt>{label}</dt>
                          <dd>
                            <span>
                              {modelLabel(
                                job.effective_agent_models?.[phase],
                              ) || "默认"}
                            </span>
                            {job.effective_agent_efforts?.[phase] && (
                              <span className="subtle">
                                {" "}
                                ·{" "}
                                {effortLabel(
                                  job.effective_agent_efforts[phase],
                                )}
                              </span>
                            )}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  </section>
                  {job.launch_status !== "skipped" && (
                    <section className="detail-section">
                      <h3>
                        启动适配{" "}
                        <span>
                          {
                            {
                              pending: "待分析",
                              running: "分析中",
                              completed: "已就绪",
                              failed: "待处理",
                            }[job.launch_status]
                          }
                        </span>
                      </h3>
                      {job.launch_retries > 0 && (
                        <p className="recovery-state">
                          自动修复 {job.launch_retries} /{" "}
                          {job.launch_max_retries} 次
                        </p>
                      )}
                      {run?.launch_plan && (
                        <>
                          <p>{run.launch_plan.rationale}</p>
                          <pre className="command-block">
                            {run.launch_plan.command}
                          </pre>
                          <dl className="properties">
                            <div>
                              <dt>进程内 GPU</dt>
                              <dd>
                                {run.allocation.map((_, i) => i).join(", ") ||
                                  "CPU"}
                              </dd>
                            </div>
                          </dl>
                          {run.launch_plan.files.map((file) => (
                            <details key={file.path}>
                              <summary>{file.path}</summary>
                              <pre className="command-block">
                                {file.content}
                              </pre>
                            </details>
                          ))}
                        </>
                      )}
                      {job.launch_status === "failed" &&
                        job.status === "needs_review" && (
                          <button
                            className="button"
                            disabled={busy}
                            onClick={() =>
                              control({
                                action: "retry-agent",
                                phase: "launch",
                              })
                            }
                          >
                            <RefreshCw size={14} />
                            重新准备启动
                          </button>
                        )}
                    </section>
                  )}
                  {job.runs.length > 1 && (
                    <section className="detail-section">
                      <h3>
                        运行记录 <span>{job.runs.length} 次</span>
                      </h3>
                      {job.runs.map((attempt, index) => (
                        <details className="run-attempt" key={attempt.id}>
                          <summary>
                            <span>第 {index + 1} 次运行</span>
                            <Status status={attempt.status} />
                          </summary>
                          <p>
                            {attempt.recovery?.kind === "oom"
                              ? "OOM 自动修复"
                              : attempt.recovery
                                ? "GPU 映射修复"
                                : "首次启动"}
                          </p>
                          <p>{attempt.launch_plan?.rationale}</p>
                          {attempt.result?.error && (
                            <p className="error-text">{attempt.result.error}</p>
                          )}
                          <pre className="command-block">
                            {attempt.launch_plan?.command || job.spec.command}
                          </pre>
                          <button
                            className="button"
                            onClick={() => {
                              setLogs(null);
                              setTmux(null);
                              setLogRun(attempt.id);
                              showTab("logs");
                            }}
                          >
                            <Terminal size={14} />
                            查看此次日志
                          </button>
                        </details>
                      ))}
                    </section>
                  )}
                  <section className="detail-section">
                    <h3>资源请求</h3>
                    {job.resources ? (
                      <div className="resource-summary">
                        <div>
                          <span>GPU</span>
                          <strong>
                            {job.resources.gpu_count}
                            <small>张</small>
                          </strong>
                        </div>
                        <div>
                          <span>CPU</span>
                          <strong>
                            {job.resources.cpu_cores}
                            <small>核</small>
                          </strong>
                        </div>
                        <div>
                          <span>系统内存</span>
                          <strong>{memory(job.resources.ram_mib)}</strong>
                        </div>
                      </div>
                    ) : (
                      <div className="notice">等待 Codex 资源建模</div>
                    )}
                    {job.resources?.gpu_count > 0 && (
                      <dl className="properties">
                        <div>
                          <dt>单卡显存</dt>
                          <dd>{memory(job.resources.gpu_memory_mib)}</dd>
                        </div>
                        <div>
                          <dt>GPU 约束</dt>
                          <dd>
                            {job.resources.gpu_type || "不限型号"}
                            {job.resources.gpu_ids?.length
                              ? ` · ${job.resources.gpu_ids.join(", ")}`
                              : ""}
                          </dd>
                        </div>
                      </dl>
                    )}
                    {run?.allocation?.length > 0 && (
                      <dl className="properties">
                        <div>
                          <dt>已分配</dt>
                          <dd>
                            {run.allocation.map((gpu) => (
                              <span key={gpu.uuid}>
                                GPU {gpu.index} · {gpu.name}
                              </span>
                            ))}
                          </dd>
                        </div>
                      </dl>
                    )}
                  </section>
                  {Object.keys(job.spec.parameters || {}).length > 0 && (
                    <section className="detail-section">
                      <h3>实验参数</h3>
                      <dl className="properties">
                        {Object.entries(job.spec.parameters).map(
                          ([key, value]) => (
                            <div key={key}>
                              <dt>{key}</dt>
                              <dd>{String(value)}</dd>
                            </div>
                          ),
                        )}
                      </dl>
                    </section>
                  )}
                  {job.status === "needs_review" && (
                    <section className="detail-section">
                      <h3>资源确认</h3>
                      <textarea
                        className="code-input"
                        aria-label="确认资源 JSON"
                        rows={8}
                        value={resources}
                        onChange={(event) => setResources(event.target.value)}
                      />
                      <div className="form-actions">
                        <button
                          className="button"
                          disabled={busy}
                          onClick={() =>
                            control({
                              action: "retry-agent",
                              phase: "estimate",
                            })
                          }
                        >
                          <RefreshCw size={14} />
                          重新预估
                        </button>
                        <button
                          className="button primary"
                          disabled={busy}
                          onClick={() => {
                            try {
                              control({
                                action: "approve",
                                resources: JSON.parse(resources),
                              });
                            } catch (error) {
                              setError(error.message);
                            }
                          }}
                        >
                          <Check size={14} />
                          批准资源
                        </button>
                      </div>
                    </section>
                  )}
                  {job.estimate && (
                    <section className="detail-section">
                      <h3>
                        预估依据
                        <span>
                          {Math.round(job.estimate.confidence * 100)}%
                        </span>
                      </h3>
                      <p>{job.estimate.rationale}</p>
                      {job.estimate.warnings?.map((text, index) => (
                        <p className="notice" key={index}>
                          {text}
                        </p>
                      ))}
                      {job.estimate.code_issues?.length > 0 && (
                        <>
                          <h4>交给启动 agent 检查的问题</h4>
                          {job.estimate.code_issues.map((text, index) => (
                            <p className="notice" key={index}>
                              {text}
                            </p>
                          ))}
                        </>
                      )}
                    </section>
                  )}
                  <section className="detail-section">
                    <h3>事件记录</h3>
                    <ol className="event-list">
                      {job.events
                        .slice(-20)
                        .reverse()
                        .map((event) => (
                          <li key={event.id}>
                            <i />
                            <div>
                              <strong>
                                {{
                                  submitted: "实验已提交",
                                  reserved: "资源已分配",
                                  running: "实验开始运行",
                                  succeeded: "实验运行完成",
                                  failed: "实验运行失败",
                                  paused: "实验已暂停",
                                  resumed: "实验已恢复",
                                  priority_changed: "优先级已调整",
                                  agent_started: "Agent 开始分析",
                                  agent_completed: "Agent 分析完成",
                                  job_updated: "实验设置已调整",
                                  completion_mode_changed: "后续运行计划已调整",
                                  agent_failed: "Agent 调用失败",
                                  launch_recovery: "启动自动修复",
                                  waiting: "等待调度",
                                  cancelled: "实验已取消",
                                }[event.kind] || event.kind}
                              </strong>
                              {event.data.reason && <p>{event.data.reason}</p>}
                              {event.kind === "launch_recovery" && (
                                <p>
                                  第 {event.data.attempt} 次 ·{" "}
                                  {event.data.kind === "oom"
                                    ? "OOM"
                                    : "GPU 映射"}
                                </p>
                              )}
                              {event.kind === "priority_changed" && (
                                <p>
                                  {event.data.old} → {event.data.new}
                                </p>
                              )}
                              <Stamp value={event.at} />
                            </div>
                          </li>
                        ))}
                    </ol>
                  </section>
                </>
              )}
              {tab === "logs" && (
                <section className="detail-section log-section">
                  {job.runs.length > 1 && (
                    <label className="log-run-select">
                      运行记录
                      <select
                        aria-label="运行记录"
                        value={logRun}
                        onChange={(event) => {
                          setLogs(null);
                          setTmux(null);
                          setLogRun(event.target.value);
                        }}
                      >
                        <option value="">最新运行</option>
                        {job.runs.map((attempt, index) => (
                          <option key={attempt.id} value={attempt.id}>
                            第 {index + 1} 次 · {attempt.id.slice(0, 8)}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {tmux?.attach_command && (
                    <div className="terminal-connection">
                      <div className="terminal-connection-heading">
                        <h3>
                          <Terminal size={15} />
                          tmux 窗口
                          <span>{tmux.available ? "可连接" : "已关闭"}</span>
                        </h3>
                        <IconButton
                          icon={Copy}
                          label="复制 tmux 连接命令"
                          disabled={!tmux.available}
                          onClick={() =>
                            navigator.clipboard
                              .writeText(tmux.attach_command)
                              .catch((error) => setError(error.message))
                          }
                        />
                      </div>
                      <details>
                        <summary>查看终端连接命令</summary>
                        <code>{tmux.attach_command}</code>
                      </details>
                    </div>
                  )}
                  <div className="detail-section-heading">
                    <h3>
                      <Terminal size={15} />
                      训练输出
                    </h3>
                    <div className="log-follow-controls">
                      <span className="subtle">
                        {active.includes(job.status)
                          ? "自动刷新"
                          : "运行已结束"}
                      </span>
                      <button
                        type="button"
                        className="button"
                        onClick={latestLogs}
                        aria-label="回到最新日志"
                        aria-pressed={followingLogs}
                      >
                        <ArrowDown size={14} />
                        {followingLogs ? "跟随最新" : "回到最新"}
                      </button>
                    </div>
                  </div>
                  {logs?.truncated && (
                    <div className="notice">显示日志末尾 64 KiB</div>
                  )}
                  <pre
                    ref={logOutput}
                    className="log-output"
                    role="log"
                    aria-label="实时训练日志"
                    aria-live="off"
                    tabIndex={0}
                    onScroll={(event) => {
                      const node = event.currentTarget;
                      const following =
                        node.scrollHeight - node.scrollTop - node.clientHeight <
                        40;
                      followLogs.current = following;
                      setFollowingLogs(following);
                    }}
                  >
                    {logs === null
                      ? "正在读取日志…"
                      : logs.text || "暂无运行日志"}
                  </pre>
                </section>
              )}
              {tab === "results" && (
                <>
                  <section className="detail-section">
                    <h3>运行结果</h3>
                    {!result ? (
                      <Empty title="暂无执行结果" />
                    ) : (
                      <>
                        <div className="resource-summary">
                          <div>
                            <span>退出码</span>
                            <strong>{result.exit_code ?? "--"}</strong>
                          </div>
                          <div>
                            <span>运行时长</span>
                            <strong>
                              {duration(metrics?.elapsed_seconds)}
                            </strong>
                          </div>
                          <div>
                            <span>内存峰值</span>
                            <strong>{memory(metrics?.peak_ram_mib)}</strong>
                          </div>
                        </div>
                        {result.error && (
                          <div className="notice error-text">
                            {result.error}
                          </div>
                        )}
                        <dl className="properties">
                          <div>
                            <dt>完成时间</dt>
                            <dd>
                              <Stamp value={result.finished_at} />
                            </dd>
                          </div>
                          <div>
                            <dt>GPU 采样数</dt>
                            <dd>{metrics?.gpu_samples ?? "--"}</dd>
                          </div>
                          {Object.entries(
                            metrics?.gpu_peak_memory_mib || {},
                          ).map(([gpu, value]) => (
                            <div key={gpu}>
                              <dt>{gpu}</dt>
                              <dd>{memory(value)}</dd>
                            </div>
                          ))}
                        </dl>
                      </>
                    )}
                  </section>
                  <section className="detail-section">
                    <h3>
                      <FileText size={15} />
                      结果文件
                    </h3>
                    {files === null ? (
                      <div className="notice">正在读取结果文件…</div>
                    ) : !files.length ? (
                      <Empty title="未配置结果文件" />
                    ) : (
                      files.map((file) => (
                        <details className="artifact" key={file.path} open>
                          <summary>{file.path}</summary>
                          {file.truncated && (
                            <span className="notice">显示前 32 KiB</span>
                          )}
                          <pre className={file.error ? "error-text" : ""}>
                            {file.error || file.content}
                          </pre>
                        </details>
                      ))
                    )}
                  </section>
                </>
              )}
              {tab === "archive" && (
                <>
                  <section className="detail-section">
                    <div className="detail-section-heading">
                      <h3>
                        <Archive size={15} />
                        实验归档
                      </h3>
                      <span className="subtle">
                        {source
                          ? returnState
                          : archiveStates[job.archive_status]}
                      </span>
                    </div>
                    {analysis?.format === "markdown" ? (
                      <div className="analysis-markdown">
                        <Markdown remarkPlugins={[remarkGfm]} skipHtml>
                          {analysis.summary}
                        </Markdown>
                      </div>
                    ) : analysis ? (
                      <>
                        <p className="analysis-summary">{analysis.summary}</p>
                        {[
                          ["findings", "主要发现"],
                          ["next_steps", "下一步实验"],
                        ].map(([key, title]) => (
                          <div className="analysis-section" key={key}>
                            <h4>{title}</h4>
                            <ul>
                              {analysis[key].map((text, index) => (
                                <li key={index}>{text}</li>
                              ))}
                            </ul>
                          </div>
                        ))}
                        <div className="analysis-section">
                          <h4>资源评估</h4>
                          <p>{analysis.resource_assessment}</p>
                        </div>
                        {analysis.suggested_command && (
                          <div className="analysis-section">
                            <h4>建议命令</h4>
                            <pre className="command-block">
                              {analysis.suggested_command}
                            </pre>
                          </div>
                        )}
                      </>
                    ) : (
                      <Empty
                        title={
                          job.archive_status === "failed"
                            ? "归档调用失败"
                            : "尚无归档结论"
                        }
                      />
                    )}
                    {terminal.includes(job.status) &&
                      job.archive_status !== "running" && (
                        <button
                          className="button"
                          disabled={busy}
                          onClick={() =>
                            control({ action: "retry-agent", phase: "archive" })
                          }
                        >
                          <RefreshCw size={14} />
                          {job.archive_status === "completed"
                            ? "重新归档"
                            : "请求归档"}
                        </button>
                      )}
                  </section>
                  <section className="detail-section">
                    <h3>
                      Agent 会话<span>{job.agents.length}</span>
                    </h3>
                    {job.agents.length ? (
                      job.agents.map((agent) => (
                        <AgentRecord key={agent.id} agent={agent} />
                      ))
                    ) : (
                      <Empty title="暂无 Agent 会话" />
                    )}
                  </section>
                </>
              )}
            </div>
          </>
        )}
      </aside>
    </>
  );
}
