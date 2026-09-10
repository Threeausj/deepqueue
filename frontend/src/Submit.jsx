import React, { useState } from "react";
import {
  Check,
  Copy,
  FileJson,
  KeyRound,
  LoaderCircle,
  Plus,
  Search,
  ShieldCheck,
} from "lucide-react";
import { api, idPath, serverLabel } from "./api.js";
import { Modal } from "./components.jsx";
import { AgentModels } from "./AgentModels.jsx";

export function Submit({ data, server, onClose, onSubmit }) {
  const servers = server ? [server] : data.servers;
  const defaultServer = server?.name || servers[0]?.name || "local";
  const defaultCwd =
    server?.config.codex?.cwd ||
    (server?.config.kind === "ssh" ? "" : data.cwd);
  const [mode, setMode] = useState("agent");
  const [request, setRequest] = useState("");
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState(null);
  const [completion, setCompletion] = useState("finish");
  const [rounds, setRounds] = useState(1);
  const [source, setSource] = useState("");
  const [models, setModels] = useState({});
  const [efforts, setEfforts] = useState({});
  const [customizeAgent, setCustomizeAgent] = useState(false);
  const [idempotency] = useState(() => `web-${crypto.randomUUID()}`);
  const [manifest, setManifest] = useState(
    JSON.stringify(
      {
        name: `experiments-${new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19)}`,
        defaults: { server: defaultServer, cwd: defaultCwd },
        experiments: [
          {
            key: "experiment-1",
            command: "python train.py",
            context_files: ["train.py"],
            artifact_files: ["results/metrics.json"],
          },
        ],
      },
      null,
      2,
    ),
  );
  const [form, setForm] = useState({
    title: "",
    server: defaultServer,
    cwd: defaultCwd,
    command: "",
    intent: "",
    resourceMode: "auto",
    gpu_count: 0,
    cpu_cores: 1,
    ram_mib: 1024,
    gpu_memory_mib: 0,
    archive: true,
    artifact: "",
    context: "",
  });
  const field = (name, value) =>
    setForm((current) => ({ ...current, [name]: value }));
  function completionFields() {
    const count = Number(rounds);
    if (!Number.isInteger(count) || count < 1 || count > 100)
      throw new Error("改进轮数必须为 1 到 100 的整数");
    return {
      completion_mode: completion,
      max_improvement_rounds: count,
      ...(source.trim() ? { source_thread_id: source.trim() } : {}),
    };
  }
  function agentFields(record = {}) {
    const result = { ...record };
    for (const [field, overrides] of [
      ["agent_models", models],
      ["agent_efforts", efforts],
    ]) {
      if (Object.keys(overrides).length)
        result[field] = { ...record[field], ...overrides };
    }
    return result;
  }
  function batchPayload() {
    const payload = JSON.parse(manifest);
    if (
      server &&
      payload.defaults?.server &&
      payload.defaults.server !== server.name
    )
      throw new Error(`此工作空间只能向服务器 ${server.name} 提交实验`);
    const fields = completionFields();
    return {
      ...payload,
      defaults: {
        ...agentFields(payload.defaults),
        ...(server ? { server: server.name } : {}),
        ...fields,
      },
      experiments: payload.experiments.map((item) => ({
        ...agentFields(item),
        ...fields,
      })),
    };
  }
  async function perform(operation) {
    setBusy(true);
    setError("");
    try {
      await operation();
    } catch (error) {
      setError(error.message);
    } finally {
      setBusy(false);
    }
  }
  function submitSingle(event) {
    event.preventDefault();
    const { title, server, cwd, command, intent, archive } = form;
    const manual = form.resourceMode === "manual";
    perform(() =>
      onSubmit("/jobs", {
        title,
        server,
        cwd,
        command,
        intent,
        archive: completion === "improve" || source.trim() ? true : archive,
        ...completionFields(),
        ...agentFields(),
        context_files: form.context
          .split("\n")
          .map((value) => value.trim())
          .filter(Boolean),
        idempotency_key: idempotency,
        artifact_files: form.artifact
          .split("\n")
          .map((value) => value.trim())
          .filter(Boolean),
        skip_estimate: manual,
        ...(manual
          ? {
              resources: {
                gpu_count: Number(form.gpu_count),
                cpu_cores: Number(form.cpu_cores),
                ram_mib: Number(form.ram_mib),
                gpu_memory_mib: Number(form.gpu_count)
                  ? Number(form.gpu_memory_mib)
                  : 0,
              },
            }
          : {}),
      }),
    );
  }
  return (
    <Modal title="提交实验" onClose={busy ? () => {} : onClose} wide>
      <div className="modal-body">
        <div className="segmented" role="tablist" aria-label="提交方式">
          {[
            ["agent", "Agent 提交"],
            ["batch", "实验批次"],
            ["single", "单个实验"],
          ].map(([value, label]) => (
            <button
              key={value}
              role="tab"
              aria-selected={mode === value}
              className={mode === value ? "active" : ""}
              onClick={() => {
                setMode(value);
                setError("");
              }}
            >
              {label}
            </button>
          ))}
        </div>
        <div
          className={`completion-fields form-grid ${completion === "improve" ? "with-rounds" : ""}`}
        >
          <div className="completion-choice">
            <span className="field-label">运行结束后</span>
            <div className="segmented" role="group" aria-label="运行结束后">
              {[
                ["finish", "结束"],
                ["improve", "继续改进"],
              ].map(([value, label]) => (
                <button
                  type="button"
                  key={value}
                  aria-pressed={completion === value}
                  className={completion === value ? "active" : ""}
                  onClick={() => {
                    setCompletion(value);
                    setPreview(null);
                    setCopied(false);
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          {completion === "improve" && (
            <label>
              最多改进轮数
              <input
                type="number"
                min="1"
                max="100"
                step="1"
                value={rounds}
                onChange={(event) => {
                  setRounds(event.target.value);
                  setPreview(null);
                  setCopied(false);
                }}
              />
            </label>
          )}
          {mode !== "agent" && (
            <label className="completion-source">
              来源对话 ID
              <input
                value={source}
                required={completion === "improve"}
                onChange={(event) => {
                  setSource(event.target.value);
                  setPreview(null);
                }}
              />
            </label>
          )}
        </div>
        <details
          className="submission-agent-settings"
          onToggle={(event) => setCustomizeAgent(event.currentTarget.open)}
        >
          <summary>
            本次 Agent 设置
            {Object.keys(models).length || Object.keys(efforts).length
              ? " · 已覆盖"
              : "（可选）"}
          </summary>
          {customizeAgent && (
            <AgentModels
              value={models}
              efforts={efforts}
              data={data}
              hasSource={mode === "agent" || !!source.trim()}
              inheritManifest={mode === "batch"}
              onChange={(value) => {
                setModels(value);
                setPreview(null);
                setCopied(false);
              }}
              onEffortsChange={(value) => {
                setEfforts(value);
                setPreview(null);
                setCopied(false);
              }}
            />
          )}
        </details>
        {error && (
          <div className="notice error-text" role="alert">
            {error}
          </div>
        )}
        {mode === "agent" ? (
          <form
            className="experiment-form"
            onSubmit={(event) => {
              event.preventDefault();
              perform(async () => {
                completionFields();
                await navigator.clipboard.writeText(
                  `请使用 $deepqueue skill 提交以下实验。\n` +
                    (data.public_url
                      ? `公网队列：${data.public_url}\n所有命令使用 deepqueue --url '${data.public_url}' --target-server '${form.server}' 前缀和已配置的服务器提交令牌，不创建本地队列。\n`
                      : `队列目录：${data.home}\n`) +
                    `绑定执行服务器：${form.server}，资源不足时在该服务器等待，不能改投其他服务器。\n项目目录：${form.cwd}\n\n${request}\n\n` +
                    "先检查项目代码与实验约束，编写批次清单并预览，再通过 deepqueue batch submit --from-agent 提交。" +
                    "使用本对话真实的 CODEX_THREAD_ID，不能生成或替换对话 ID。" +
                    "保留资源预估和 archive=true，指定日志及结果文件。" +
                    `设置 completion_mode=${completion}，max_improvement_rounds=${Number(rounds)}。` +
                    (Object.keys(models).length
                      ? `设置 agent_models=${JSON.stringify(models)}，这些是各阶段 Codex agent 的模型选择。`
                      : "未指定的 Agent 模型使用通用设置，保留默认继承，不要写入固定模型 ID。") +
                    (Object.keys(efforts).length
                      ? `设置 agent_efforts=${JSON.stringify(efforts)}。`
                      : "Agent 推理强度使用通用设置，不必在提交时指定。") +
                    "archive=source-thread 表示归档与改进使用来源会话届时的模型，不要替换成固定模型 ID；明确指定其他 archive 模型才覆盖会话模型。" +
                    (completion === "improve"
                      ? "实验结束后回到本对话，按归档模型选择分析实验效果，针对性修改模型并验证，再通过 --parent-job-id 提交一个高优先级改进训练，继承各阶段模型选择并遵守改进轮数限制。"
                      : "实验结束释放资源，回到本对话总结效果，不自动提交后续训练。") +
                    "保留 launch_agent=true 和 tmux=true，启动时由新 agent 根据实际分配 GPU 适配运行参数。" +
                    "提交后报告批次、实验 ID 和来源对话链接。",
                );
                setCopied(true);
              });
            }}
          >
            <div className="form-grid">
              <label>
                服务器
                <select
                  value={form.server}
                  disabled={!!server}
                  onChange={(event) => {
                    field("server", event.target.value);
                    setCopied(false);
                  }}
                >
                  {servers.map((server) => (
                    <option key={server.name} value={server.name}>
                      {serverLabel(server)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                项目目录
                <input
                  required
                  value={form.cwd}
                  onChange={(event) => {
                    field("cwd", event.target.value);
                    setCopied(false);
                  }}
                />
              </label>
            </div>
            <label>
              实验需求
              <textarea
                required
                rows={9}
                value={request}
                onChange={(event) => {
                  setRequest(event.target.value);
                  setCopied(false);
                }}
              />
            </label>
            <div className="form-actions">
              <button className="button primary" disabled={busy} type="submit">
                {copied ? <Check size={15} /> : <Copy size={15} />}
                {copied ? "已复制，待 Agent 提交" : "复制给 Codex"}
              </button>
            </div>
          </form>
        ) : mode === "batch" ? (
          <>
            <div className="editor-heading">
              <label htmlFor="manifest">实验清单 JSON</label>
              <label className="file-button">
                <FileJson size={14} />
                导入文件
                <input
                  type="file"
                  accept=".json,application/json"
                  onChange={async (event) => {
                    const file = event.target.files[0];
                    if (file) {
                      setManifest(await file.text());
                      setPreview(null);
                    }
                  }}
                />
              </label>
            </div>
            <textarea
              id="manifest"
              className="code-input manifest-input"
              spellCheck="false"
              value={manifest}
              onChange={(event) => {
                setManifest(event.target.value);
                setPreview(null);
              }}
            />
            {preview && (
              <div className="preview">
                <div className="preview-heading">
                  <ShieldCheck size={17} />
                  <strong>{preview.experiment_count} 个实验</strong>
                  <span>{preview.estimation_count} 次资源预估</span>
                  {preview.existing && <span>已存在</span>}
                </div>
                <div className="preview-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>实验</th>
                        <th>服务器</th>
                        <th>命令</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.experiments.slice(0, 100).map((job) => (
                        <tr key={job.key}>
                          <td>{job.key}</td>
                          <td>{job.server}</td>
                          <td>
                            <code>{job.command}</code>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {preview.experiment_count > 100 && (
                  <p className="notice">
                    预览前 100 个，共 {preview.experiment_count} 个实验。
                  </p>
                )}
              </div>
            )}
            <div className="form-actions">
              <button
                className="button"
                disabled={busy}
                onClick={() =>
                  perform(async () =>
                    setPreview(
                      await api(
                        `/batches/preview${server ? `?server=${idPath(server.name)}` : ""}`,
                        batchPayload(),
                      ),
                    ),
                  )
                }
              >
                <Search size={15} />
                预览清单
              </button>
              <button
                className="button primary"
                disabled={busy || !preview}
                onClick={() =>
                  perform(() => onSubmit("/batches", batchPayload()))
                }
              >
                {busy ? (
                  <LoaderCircle size={15} className="spinning" />
                ) : (
                  <Plus size={15} />
                )}
                提交批次
              </button>
            </div>
          </>
        ) : (
          <form onSubmit={submitSingle} className="experiment-form">
            <label>
              实验名称
              <input
                required
                maxLength={200}
                value={form.title}
                onChange={(event) => field("title", event.target.value)}
              />
            </label>
            <div className="form-grid">
              <label>
                目标服务器
                <select
                  value={form.server}
                  disabled={!!server}
                  onChange={(event) => field("server", event.target.value)}
                >
                  {servers.map((server) => (
                    <option key={server.name} value={server.name}>
                      {serverLabel(server)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                工作目录
                <input
                  required
                  value={form.cwd}
                  onChange={(event) => field("cwd", event.target.value)}
                />
              </label>
            </div>
            <label>
              运行命令
              <textarea
                required
                className="code-input"
                rows={3}
                value={form.command}
                onChange={(event) => field("command", event.target.value)}
              />
            </label>
            <label>
              实验目标
              <textarea
                rows={2}
                value={form.intent}
                onChange={(event) => field("intent", event.target.value)}
              />
            </label>
            <label>
              资源建模
              <select
                value={form.resourceMode}
                onChange={(event) => field("resourceMode", event.target.value)}
              >
                <option value="auto">Codex 预估</option>
                <option value="manual">指定资源，跳过预估</option>
              </select>
            </label>
            {form.resourceMode === "manual" && (
              <div className="form-grid numeric-grid">
                {[
                  ["gpu_count", "GPU 数量", 0, 256],
                  ["gpu_memory_mib", "单卡显存 MiB", 0],
                  ["cpu_cores", "CPU 核数", 1],
                  ["ram_mib", "系统内存 MiB", 128],
                ].map(([name, label, min, max]) => (
                  <label key={name}>
                    {label}
                    <input
                      type="number"
                      required
                      min={min}
                      max={max}
                      value={form[name]}
                      onChange={(event) => field(name, event.target.value)}
                    />
                  </label>
                ))}
              </div>
            )}
            <label>
              训练代码与配置文件
              <textarea
                rows={2}
                value={form.context}
                onChange={(event) => field("context", event.target.value)}
              />
            </label>
            <label>
              结果文件
              <textarea
                rows={2}
                value={form.artifact}
                onChange={(event) => field("artifact", event.target.value)}
              />
            </label>
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={
                  completion === "improve" || !!source.trim() || form.archive
                }
                disabled={completion === "improve" || !!source.trim()}
                onChange={(event) => field("archive", event.target.checked)}
              />
              完成后调用 Agent 归档
            </label>
            <div className="form-actions">
              <button
                className="button"
                type="button"
                disabled={busy}
                onClick={onClose}
              >
                取消
              </button>
              <button className="button primary" type="submit" disabled={busy}>
                <Plus size={15} />
                提交实验
              </button>
            </div>
          </form>
        )}
      </div>
    </Modal>
  );
}

export function ServerForm({ server, onClose, onSubmit }) {
  const editing = !!server;
  const isSSH = !editing || server.config.kind === "ssh";
  const [generatedName] = useState(
    () => `ssh-${crypto.randomUUID().slice(0, 8)}`,
  );
  const [customIdentifier, setCustomIdentifier] = useState(false);
  const [form, setForm] = useState({
    name: server?.name || "",
    display_name: serverLabel(server),
    host: server?.config.host || "",
    port: server?.config.port || 22,
    username: server?.config.username || "",
    auth: editing ? "keep" : "key",
    key_file: "",
    password: "",
    passphrase: "",
    max_running: server?.config.max_running || 8,
  });
  const [fingerprint, setFingerprint] = useState(null);
  const [trusted, setTrusted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const needsTrust =
    isSSH &&
    (!editing ||
      form.host.trim() !== server.config.host ||
      Number(form.port) !== server.config.port);
  const currentFingerprint =
    fingerprint?.host === form.host.trim() &&
    fingerprint?.port === Number(form.port);
  function field(name, value) {
    if (["host", "port"].includes(name)) {
      setFingerprint(null);
      setTrusted(false);
    }
    setForm((current) => ({
      ...current,
      [name]: value,
      ...(name === "display_name" && !editing && !customIdentifier
        ? {
            name:
              value
                .trim()
                .replace(/[^a-zA-Z0-9_.-]+/g, "-")
                .replace(/^[^a-zA-Z0-9]+/, "")
                .slice(0, 64) || generatedName,
          }
        : {}),
    }));
  }
  async function perform(operation) {
    setBusy(true);
    setError("");
    try {
      await operation();
    } catch (error) {
      setError(error.message);
    } finally {
      setBusy(false);
    }
  }
  function submit(event) {
    event.preventDefault();
    if (needsTrust && (!currentFingerprint || !trusted)) {
      setError("请先核对并确认主机指纹");
      return;
    }
    const body = {
      display_name: form.display_name.trim(),
      max_running: Number(form.max_running),
      ...(isSSH
        ? {
            host: form.host.trim(),
            port: Number(form.port),
            username: form.username.trim(),
          }
        : {}),
      ...(needsTrust ? { fingerprint: fingerprint.fingerprint } : {}),
      ...(!editing ? { name: form.name } : {}),
    };
    if (isSSH && form.auth !== "keep") {
      if (editing) body.authentication = form.auth;
      if (form.auth === "key")
        Object.assign(body, {
          key_file: form.key_file || null,
          passphrase: form.passphrase || null,
        });
      else body.password = form.password;
    }
    perform(() => onSubmit(body));
  }
  return (
    <Modal
      title={editing ? "编辑服务器" : "接入 SSH 服务器"}
      onClose={busy ? () => {} : onClose}
    >
      <form
        className="modal-body experiment-form server-edit-form"
        onSubmit={submit}
      >
        {error && (
          <div className="notice error-text" role="alert">
            {error}
          </div>
        )}
        <label>
          服务器名称
          <input
            required
            maxLength={100}
            value={form.display_name}
            onChange={(event) => field("display_name", event.target.value)}
            placeholder="例如：训练服务器 A"
          />
        </label>
        <div className="form-grid">
          <label>
            服务器标识
            <input
              required
              readOnly={editing}
              pattern="[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}"
              value={form.name}
              onChange={(event) => {
                setCustomIdentifier(true);
                field("name", event.target.value);
              }}
            />
            <small>用于 CLI 和 skill 提交任务</small>
          </label>
          <label>
            最大并发
            <input
              type="number"
              min="1"
              max="1000"
              required
              value={form.max_running}
              onChange={(event) => field("max_running", event.target.value)}
            />
            <small>同时执行的实验数量</small>
          </label>
        </div>
        {isSSH && (
          <>
            <div className="form-grid">
              <label>
                主机地址
                <input
                  required
                  value={form.host}
                  onChange={(event) => field("host", event.target.value)}
                />
              </label>
              <label>
                端口
                <input
                  type="number"
                  min="1"
                  max="65535"
                  required
                  value={form.port}
                  onChange={(event) => field("port", event.target.value)}
                />
              </label>
            </div>
            <label>
              用户名
              <input
                required
                value={form.username}
                onChange={(event) => field("username", event.target.value)}
              />
            </label>
            <label>
              认证方式
              <select
                value={form.auth}
                onChange={(event) => field("auth", event.target.value)}
              >
                {editing && <option value="keep">保持现有认证</option>}
                <option value="key">SSH 密钥</option>
                <option value="password">SSH 密码</option>
              </select>
            </label>
            {form.auth === "key" && (
              <>
                <label>
                  本机密钥路径
                  <input
                    value={form.key_file}
                    onChange={(event) => field("key_file", event.target.value)}
                    placeholder="留空使用默认 SSH 密钥"
                  />
                </label>
                <label>
                  密钥口令
                  <input
                    type="password"
                    autoComplete="new-password"
                    value={form.passphrase}
                    onChange={(event) =>
                      field("passphrase", event.target.value)
                    }
                  />
                </label>
              </>
            )}
            {form.auth === "password" && (
              <label>
                SSH 密码
                <input
                  type="password"
                  autoComplete="new-password"
                  required
                  value={form.password}
                  onChange={(event) => field("password", event.target.value)}
                />
              </label>
            )}
            {needsTrust && (
              <>
                <button
                  className="button"
                  type="button"
                  disabled={busy || !form.host.trim() || !form.port}
                  onClick={() =>
                    perform(async () => {
                      const address = form.host.trim(),
                        port = Number(form.port);
                      const result = await api(
                        `/ssh/fingerprint?host=${idPath(address)}&port=${port}`,
                      );
                      setFingerprint({ ...result, host: address, port });
                      setTrusted(false);
                    })
                  }
                >
                  <KeyRound size={15} />
                  获取主机指纹
                </button>
                {currentFingerprint && (
                  <div className="fingerprint">
                    <code>
                      {fingerprint.key_type}
                      <br />
                      {fingerprint.fingerprint}
                    </code>
                    <label className="checkbox-label">
                      <input
                        type="checkbox"
                        checked={trusted}
                        onChange={(event) => setTrusted(event.target.checked)}
                      />
                      已核对并信任此主机指纹
                    </label>
                  </div>
                )}
              </>
            )}
          </>
        )}
        <div className="form-actions">
          <button
            className="button"
            type="button"
            disabled={busy}
            onClick={onClose}
          >
            取消
          </button>
          <button
            className="button primary"
            type="submit"
            disabled={busy || (needsTrust && (!currentFingerprint || !trusted))}
          >
            {busy ? (
              <LoaderCircle className="spinning" size={15} />
            ) : (
              <Check size={15} />
            )}
            {editing ? "保存修改" : "接入服务器"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
