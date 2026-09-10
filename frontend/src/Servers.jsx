import React, { useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  ChevronDown,
  GripVertical,
  KeyRound,
  ListOrdered,
  MessageSquare,
  Pause,
  Pencil,
  Play,
  RefreshCw,
  Server,
} from "lucide-react";
import { active, idPath, serverLabel } from "./api.js";
import { IconButton, memory, Meter, Stamp } from "./components.jsx";

export default function Servers({
  servers,
  jobs,
  busy,
  mutate,
  navigate,
  onEdit,
  onAccess,
}) {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return (
        JSON.parse(localStorage.getItem("deepqueue.collapsed-resources")) || {}
      );
    } catch {
      return {};
    }
  });
  const [dragging, setDragging] = useState(null);
  const [dropTarget, setDropTarget] = useState(null);
  function toggle(name) {
    const next = { ...collapsed, [name]: !collapsed[name] };
    setCollapsed(next);
    try {
      localStorage.setItem(
        "deepqueue.collapsed-resources",
        JSON.stringify(next),
      );
    } catch {
      /* Folding still works without storage. */
    }
  }
  function move(name, target) {
    const names = servers.map((server) => server.name);
    const index = names.indexOf(name);
    if (
      busy ||
      index < 0 ||
      target < 0 ||
      target >= names.length ||
      index === target
    )
      return;
    names.splice(index, 1);
    names.splice(target, 0, name);
    mutate("/servers/order", { names }).catch(() => {});
  }
  return (
    <section className="server-section" aria-label="服务器资源列表">
      <div className="server-list-hint subtle">
        <GripVertical size={15} /> 拖动手柄或使用上下箭头调整顺序
      </div>
      {servers.map((server, index) => {
        const label = serverLabel(server);
        const snap = server.snapshot;
        const open = !collapsed[server.name];
        const detailsId = `server-resources-${server.name}`;
        const running = jobs.filter(
          (job) => job.server === server.name && active.includes(job.status),
        ).length;
        const holding = (server.reservations || []).filter(
          (run) => run.holding,
        ).length;
        return (
          <article
            key={server.name}
            data-server-id={server.name}
            className={`server-band ${dragging === server.name ? "dragging" : ""} ${dropTarget === server.name ? "drop-target" : ""}`}
            onDragOver={(event) => {
              if (dragging && dragging !== server.name && !busy) {
                event.preventDefault();
                event.dataTransfer.dropEffect = "move";
                setDropTarget(server.name);
              }
            }}
            onDrop={(event) => {
              event.preventDefault();
              if (dragging) move(dragging, index);
              setDragging(null);
              setDropTarget(null);
            }}
          >
            <div className="server-card-heading">
              <button
                type="button"
                className="server-drag-handle"
                draggable={!busy}
                aria-label={`拖动 ${label} 排序`}
                title="拖动调整顺序"
                disabled={busy}
                onDragStart={(event) => {
                  event.dataTransfer.effectAllowed = "move";
                  event.dataTransfer.setData("text/plain", server.name);
                  setDragging(server.name);
                }}
                onDragEnd={() => {
                  setDragging(null);
                  setDropTarget(null);
                }}
              >
                <GripVertical size={18} />
              </button>
              <button
                type="button"
                className="server-summary"
                aria-expanded={open}
                aria-controls={detailsId}
                aria-label={`${open ? "收起" : "展开"} ${label} 资源详情`}
                onClick={() => toggle(server.name)}
              >
                <span className="server-icon">
                  <Server size={22} />
                </span>
                <span className="server-summary-copy">
                  <span className="server-card-title">
                    {label}
                    <span className="type-tag">
                      {server.config.kind === "ssh" ? "SSH" : "LOCAL"}
                    </span>
                  </span>
                  <span className="subtle server-address">
                    {server.config.host
                      ? `${server.config.username}@${server.config.host}:${server.config.port}`
                      : "本机"}
                  </span>
                  <span className="subtle">
                    {snap ? `${snap.gpus?.length || 0} 张 GPU · ` : ""}
                    {running} / {server.config.max_running} 个运行槽位
                    {holding ? ` · ${holding} 个改进保留` : ""}
                  </span>
                </span>
                <ChevronDown
                  size={18}
                  className={`resource-chevron ${open ? "open" : ""}`}
                />
              </button>
              <div className="server-order-actions">
                <IconButton
                  icon={Pencil}
                  label={`编辑 ${label}`}
                  disabled={busy}
                  onClick={() => onEdit(server)}
                />
                <IconButton
                  icon={ArrowUp}
                  label={`上移 ${label}`}
                  disabled={busy || index === 0}
                  onClick={() => move(server.name, index - 1)}
                />
                <IconButton
                  icon={ArrowDown}
                  label={`下移 ${label}`}
                  disabled={busy || index === servers.length - 1}
                  onClick={() => move(server.name, index + 1)}
                />
              </div>
            </div>
            <div className="server-card-actions">
              <span
                className={`server-scheduling ${server.config.enabled ? "enabled" : ""}`}
              >
                <span
                  className={`dot ${server.config.enabled ? "green" : "gray"}`}
                />
                {server.config.enabled ? "参与调度" : "已暂停新实验"}
              </span>
              <div className="row-actions">
                <IconButton
                  icon={MessageSquare}
                  label={`打开 ${label} Codex`}
                  onClick={() =>
                    navigate({ view: "codex", server: server.name })
                  }
                />
                <IconButton
                  icon={ListOrdered}
                  label={`查看 ${label} 队列`}
                  onClick={() =>
                    navigate({ view: "queue", server: server.name })
                  }
                />
                <IconButton
                  icon={KeyRound}
                  label={`${label} 接入配置`}
                  onClick={() => onAccess(server)}
                />
                <IconButton
                  icon={RefreshCw}
                  label={`探测 ${label}`}
                  disabled={busy}
                  onClick={() =>
                    mutate(`/servers/${idPath(server.name)}/probe`, {}).catch(
                      () => {},
                    )
                  }
                />
                <IconButton
                  icon={server.config.enabled ? Pause : Play}
                  label={server.config.enabled ? "暂停服务器" : "恢复服务器"}
                  disabled={busy}
                  onClick={() =>
                    mutate(`/servers/${idPath(server.name)}/control`, {
                      action: server.config.enabled ? "pause" : "resume",
                    }).catch(() => {})
                  }
                />
              </div>
            </div>
            {server.error && (
              <div className="notice error-text">{server.error}</div>
            )}
            <div
              id={detailsId}
              className="server-resource-details"
              hidden={!open}
            >
              {snap && server.snapshot_stale && (
                <div className="notice">资源快照已过期</div>
              )}
              <div className="server-stats">
                <div>
                  <span>CPU 可用</span>
                  <strong>
                    {snap
                      ? `${Number(snap.cpu_available).toFixed(1)} / ${snap.cpu_count}`
                      : "--"}
                    <small>核</small>
                  </strong>
                  <Meter
                    label="CPU 占用"
                    value={
                      snap ? 100 * (1 - snap.cpu_available / snap.cpu_count) : 0
                    }
                    tone="blue"
                  />
                </div>
                <div>
                  <span>系统可用内存</span>
                  <strong>
                    {memory(snap?.ram_available_mib)}
                    <small>/ {memory(snap?.ram_total_mib)}</small>
                  </strong>
                  <Meter
                    label="内存占用"
                    value={
                      snap
                        ? 100 *
                          (1 - snap.ram_available_mib / snap.ram_total_mib)
                        : 0
                    }
                  />
                </div>
                <div>
                  <span>资源快照</span>
                  <strong className="snapshot-time">
                    <Stamp value={snap?.received_at} />
                  </strong>
                  <span>{server.name}</span>
                </div>
              </div>
              <div className="gpu-grid">
                {snap?.gpus?.map((gpu) => {
                  const reservation = server.reservations?.find((run) =>
                    run.allocation.some((item) => item.uuid === gpu.uuid),
                  );
                  return (
                    <div className="gpu-item" key={gpu.uuid}>
                      <div>
                        <span className="gpu-index">GPU {gpu.index}</span>
                        <span
                          className={`dot ${gpu.processes?.length || reservation ? "blue" : "green"}`}
                        />
                        <strong>{gpu.name}</strong>
                      </div>
                      <p className="gpu-uuid">{gpu.uuid}</p>
                      {reservation && (
                        <a
                          className="text-link"
                          href={`#view=queue&server=${idPath(server.name)}&job=${reservation.job_id}`}
                        >
                          {reservation.holding ? "改进保留" : "队列已预留"} ·{" "}
                          {reservation.job_id.slice(0, 8)}
                        </a>
                      )}
                      <dl>
                        <div>
                          <dt>显存占用</dt>
                          <dd>
                            {memory(gpu.memory_total_mib - gpu.memory_free_mib)}{" "}
                            / {memory(gpu.memory_total_mib)}
                          </dd>
                        </div>
                      </dl>
                      <Meter
                        label={`GPU ${gpu.index} 显存`}
                        value={
                          100 * (1 - gpu.memory_free_mib / gpu.memory_total_mib)
                        }
                        tone="blue"
                      />
                      <div className="gpu-bottom">
                        <span>
                          利用率 <b>{gpu.utilization}%</b>
                        </span>
                        <span>{gpu.processes?.length ?? 0} 个进程</span>
                      </div>
                    </div>
                  );
                })}
              </div>
              {!snap && <div className="notice">尚无资源快照</div>}
              {snap?.gpu_probe_error && (
                <div className="notice error-text">{snap.gpu_probe_error}</div>
              )}
              {snap && !snap.gpus?.length && !snap.gpu_probe_error && (
                <div className="notice">未检测到 NVIDIA GPU</div>
              )}
            </div>
          </article>
        );
      })}
    </section>
  );
}
