import React, { useEffect, useState } from "react";
import { ShieldCheck } from "lucide-react";
import { api } from "./api.js";
import { Modal } from "./components.jsx";

const profiles = {
  ":read-only": [
    "只读",
    "查看文件；文件写入和网络访问受此服务器的权限配置限制。",
  ],
  ":workspace": [
    "工作区读写",
    "允许修改项目文件；工作区外写入和网络访问受限。",
  ],
  ":danger-full-access": [
    "完全访问",
    "可读写此服务器账户能访问的文件，并访问网络。",
  ],
};
const approvals = {
  "on-request": "按需审批",
  untrusted: "不受信任的命令需审批",
  never: "不询问（受限操作直接失败）",
};

export function accessLabel(thread) {
  const id = thread?.activePermissionProfile?.id;
  if (id) return profiles[id]?.[0] || id;
  if (thread?.sandbox) return "自定义权限";
  return "会话权限";
}

export function PermissionFields({
  base,
  cwd,
  value,
  onChange,
  current,
  disabled,
}) {
  const [catalog, setCatalog] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    setCatalog(null);
    setError("");
    const timer = setTimeout(() => {
      const query = new URLSearchParams();
      if (cwd?.startsWith("/")) query.set("cwd", cwd);
      api(`${base}/permissions?${query}`, undefined, {
        signal: controller.signal,
      })
        .then(setCatalog)
        .catch((error) => {
          if (error.name !== "AbortError") setError(error.message);
        });
    }, 150);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [base, cwd]);
  const selected = (catalog?.data || []).find(
    (item) => item.id === value.permissions,
  );
  const allowed = catalog?.requirements?.allowedApprovalPolicies;
  const policy = current?.approvalPolicy;
  return (
    <div className="codex-access-fields">
      {error && (
        <p className="notice error-text" role="alert">
          无法读取服务器权限配置：{error}
        </p>
      )}
      <label>
        文件与网络访问
        <select
          aria-label="文件与网络访问"
          value={value.permissions || ""}
          disabled={disabled || !catalog}
          onChange={(event) =>
            onChange({ ...value, permissions: event.target.value || null })
          }
        >
          <option value="">
            {current ? `沿用当前 · ${accessLabel(current)}` : "服务器默认权限"}
          </option>
          {(catalog?.data || []).map((item) => (
            <option key={item.id} value={item.id} disabled={!item.allowed}>
              {profiles[item.id]?.[0] || item.id}
              {!item.allowed ? " · 管理员已限制" : ""}
            </option>
          ))}
        </select>
        <small>
          {selected?.description ||
            profiles[value.permissions]?.[1] ||
            "使用此服务器 Codex 保存的权限配置。"}
        </small>
      </label>
      <label>
        审批策略
        <select
          aria-label="审批策略"
          value={value.approval_policy || ""}
          disabled={disabled || !catalog}
          onChange={(event) =>
            onChange({ ...value, approval_policy: event.target.value || null })
          }
        >
          <option value="">
            {current
              ? `沿用当前 · ${approvals[policy] || "自定义策略"}`
              : "服务器默认审批策略"}
          </option>
          {Object.entries(approvals).map(([id, title]) => (
            <option
              key={id}
              value={id}
              disabled={allowed && !allowed.includes(id)}
            >
              {title}
            </option>
          ))}
        </select>
        <small>需要确认的命令和文件修改会在对话中显示。</small>
      </label>
      {current?.sandbox && (
        <details className="codex-access-details">
          <summary>查看当前生效权限</summary>
          <pre>
            {JSON.stringify(
              {
                permissions: current.activePermissionProfile,
                sandbox: current.sandbox,
                approvalPolicy: current.approvalPolicy,
                approvalsReviewer: current.approvalsReviewer,
              },
              null,
              2,
            )}
          </pre>
        </details>
      )}
    </div>
  );
}

export function AccessDialog({ base, thread, busy, error, onClose, onSave }) {
  const [value, setValue] = useState({});
  return (
    <Modal title="访问权限" onClose={onClose}>
      <form
        className="codex-dialog-form"
        onSubmit={(event) => {
          event.preventDefault();
          onSave(value);
        }}
      >
        <p className="subtle">
          设置保存在这台服务器的当前任务中，对后续轮次生效。
        </p>
        {error && (
          <p className="notice error-text" role="alert">
            {error}
          </p>
        )}
        <PermissionFields
          {...{ base, value }}
          cwd={thread.cwd}
          current={thread}
          onChange={setValue}
          disabled={busy}
        />
        <div className="form-actions">
          <button type="button" className="button" onClick={onClose}>
            取消
          </button>
          <button
            className="button primary"
            disabled={busy || (!value.permissions && !value.approval_policy)}
          >
            <ShieldCheck size={15} />
            保存权限
          </button>
        </div>
      </form>
    </Modal>
  );
}
