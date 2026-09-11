import { useEffect, useRef } from "react";
import {
  Check,
  CircleAlert,
  Gauge,
  LoaderCircle,
  Minimize2,
} from "lucide-react";

function count(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}
const number = (value) =>
  value === null ? "未提供" : value.toLocaleString("zh-CN");
const compactNumber = (value) =>
  value >= 1000
    ? `${(value / 1000).toLocaleString("en-US", { maximumFractionDigits: 1 })}k`
    : String(value);

export function compactionStatus(turn, item, observed) {
  if (
    observed?.turnId === turn.id &&
    (observed.itemId === item.id || !observed.itemId)
  )
    return observed.status;
  if (item.status) return item.status;
  // Stored compaction items have no status; later items prove compaction finished.
  if (turn.items.at(-1)?.id !== item.id) return "completed";
  return turn.status === "inProgress" ? "unknown" : turn.status || "completed";
}

function latestCompaction(thread) {
  if (thread.context?.compaction) return thread.context.compaction;
  for (const turn of [...(thread.turns || [])].reverse()) {
    const item = turn.items?.findLast(
      (item) => item.type === "contextCompaction",
    );
    if (item) return { status: compactionStatus(turn, item) };
  }
  return null;
}

function stateLabel(status, live) {
  if (status === "inProgress")
    return live ? "正在压缩上下文" : "压缩状态待同步";
  return (
    {
      completed: "上下文已压缩",
      failed: "上下文压缩失败",
      interrupted: "上下文压缩已中断",
    }[status] || "压缩状态待同步"
  );
}

function StateIcon({ status, live }) {
  return status === "inProgress" && live ? (
    <LoaderCircle size={13} className="spinning" />
  ) : status === "completed" ? (
    <Check size={13} />
  ) : status === "failed" || status === "interrupted" ? (
    <CircleAlert size={13} />
  ) : (
    <Minimize2 size={13} />
  );
}

export function CompactionRecord({ status, live }) {
  return (
    <div className="codex-compaction" role="status" data-state={status}>
      <StateIcon status={status} live={live} />
      <span>{stateLabel(status, live)}</span>
    </div>
  );
}

export default function CodexContext({ thread, live }) {
  const root = useRef(null);
  const usage = thread.context?.tokenUsage;
  const used = count(usage?.last?.totalTokens);
  const window = count(usage?.modelContextWindow);
  const capacity = window > 0 ? window : null;
  const percent =
    used !== null && capacity ? Math.round((used / capacity) * 100) : null;
  const state = latestCompaction(thread);
  useEffect(() => {
    const outside = (event) => {
      if (!root.current?.contains(event.target) && root.current)
        root.current.open = false;
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, []);
  return (
    <div className="codex-context-bar">
      <details
        className="codex-context"
        ref={root}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            root.current.open = false;
            root.current.querySelector("summary").focus();
          }
        }}
      >
        <summary
          aria-label="上下文用量"
          data-level={percent >= 90 ? "high" : "normal"}
        >
          <Gauge size={13} />
          <span>上下文</span>
          {used === null ? (
            <span>等待统计</span>
          ) : (
            <span className="codex-context-value">
              {compactNumber(used)}
              {capacity
                ? ` / ${compactNumber(capacity)} · ${percent}%`
                : " tokens"}
            </span>
          )}
        </summary>
        <div
          className="codex-context-detail"
          role="group"
          aria-label="上下文统计详情"
        >
          <strong>上下文用量</strong>
          {percent !== null && (
            <div
              className="codex-context-meter"
              role="meter"
              aria-label="上下文占用"
              aria-valuemin={0}
              aria-valuemax={capacity}
              aria-valuenow={Math.min(used, capacity)}
              aria-valuetext={`${number(used)} / ${number(capacity)} tokens，${percent}%`}
            >
              <span style={{ width: `${Math.min(100, percent)}%` }} />
            </div>
          )}
          <dl>
            <div>
              <dt>最近上下文</dt>
              <dd>
                {number(used)}
                {used !== null && " tokens"}
              </dd>
            </div>
            <div>
              <dt>窗口上限</dt>
              <dd>
                {number(capacity)}
                {capacity !== null && " tokens"}
              </dd>
            </div>
            <div>
              <dt>累计消耗</dt>
              <dd>{number(count(usage?.total?.totalTokens))}</dd>
            </div>
          </dl>
          <p>
            {used === null
              ? "等待 Codex 返回用量统计。"
              : "按最近一次模型请求统计；未发送的输入尚未计入。"}
          </p>
        </div>
      </details>
      {state && (
        <span
          className="codex-context-state"
          role="status"
          data-state={state.status}
        >
          <StateIcon status={state.status} live={live} />
          {stateLabel(state.status, live)}
        </span>
      )}
    </div>
  );
}
