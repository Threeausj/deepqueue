import React, { useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Inbox, X } from "lucide-react";

export const states = {
  pending: ["待预估", "neutral"],
  estimating: ["预估中", "blue"],
  needs_review: ["待确认", "amber"],
  queued: ["排队中", "neutral"],
  starting: ["启动中", "blue"],
  running: ["运行中", "green"],
  lost: ["失联", "red"],
  succeeded: ["已完成", "green"],
  failed: ["失败", "red"],
  cancelled: ["已取消", "neutral"],
  blocked: ["依赖阻塞", "amber"],
};
export const archiveStates = {
  pending: "待归档",
  running: "归档中",
  completed: "已归档",
  failed: "归档失败",
  skipped: "未启用",
};

export function IconButton({ icon: Icon, label, className = "", ...props }) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      className={`icon-button ${className}`}
      {...props}
    >
      <Icon size={16} />
    </button>
  );
}
export function Status({ status, held = false }) {
  const [text, color] = held
    ? ["已暂停", "amber"]
    : states[status] || [status, "neutral"];
  return (
    <span className={`status ${color}`}>
      <i />
      {text}
    </span>
  );
}
export function Empty({ title = "暂无实验", children }) {
  return (
    <div className="empty">
      <Inbox size={34} strokeWidth={1.3} />
      <h3>{title}</h3>
      {children}
    </div>
  );
}
export function Stamp({ value }) {
  return (
    <time title={value ? new Date(value * 1000).toLocaleString("zh-CN") : ""}>
      {value
        ? new Date(value * 1000).toLocaleString("zh-CN", {
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
          })
        : "暂无记录"}
    </time>
  );
}
export function duration(seconds) {
  if (!Number.isFinite(seconds)) return "--";
  const value = Math.max(0, Math.floor(seconds));
  return value >= 3600
    ? `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`
    : value >= 60
      ? `${Math.floor(value / 60)}m ${value % 60}s`
      : `${value}s`;
}
export function memory(mib) {
  if (!Number.isFinite(mib)) return "--";
  return mib >= 1024
    ? `${(mib / 1024).toFixed(1)} GiB`
    : `${Math.round(mib)} MiB`;
}
export function Meter({ value = 0, tone = "green", label }) {
  return (
    <div
      className={`meter ${tone}`}
      role="meter"
      aria-label={label}
      aria-valuenow={Math.round(value)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <span style={{ width: `${Math.min(100, Math.max(0, value))}%` }} />
    </div>
  );
}
export function Priority({ value, onChange, disabled = false }) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  function commit() {
    const number = Number(draft);
    if (
      draft.trim() &&
      Number.isInteger(number) &&
      number >= -100 &&
      number <= 100
    ) {
      if (number !== value) onChange(number);
    }
    setDraft(String(value));
  }
  return (
    <div className="priority" onClick={(event) => event.stopPropagation()}>
      <IconButton
        icon={ArrowUp}
        label="提高优先级"
        disabled={disabled || value >= 100}
        onClick={() => onChange(Math.min(100, value + 10))}
      />
      <input
        aria-label="优先级"
        type="number"
        min="-100"
        max="100"
        value={draft}
        disabled={disabled}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter") event.currentTarget.blur();
        }}
      />
      <IconButton
        icon={ArrowDown}
        label="降低优先级"
        disabled={disabled || value <= -100}
        onClick={() => onChange(Math.max(-100, value - 10))}
      />
    </div>
  );
}
export function Modal({ title, onClose, children, wide = false }) {
  const ref = useRef(null);
  useEffect(() => {
    const dialog = ref.current;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return (
    <dialog
      ref={ref}
      aria-label={title}
      className={`modal ${wide ? "wide" : ""}`}
      onCancel={onClose}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="modal-heading">
        <h2>{title}</h2>
        <IconButton icon={X} label="关闭" onClick={onClose} />
      </div>
      {children}
    </dialog>
  );
}
