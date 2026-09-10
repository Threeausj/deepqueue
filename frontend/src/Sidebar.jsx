import React, { useState } from "react";
import {
  Archive,
  ChevronDown,
  ChevronRight,
  Layers3,
  ListOrdered,
  MessageSquare,
  PanelLeftClose,
  PanelLeftOpen,
  Server,
  Settings2,
  Workflow,
} from "lucide-react";

import { serverLabel } from "./api.js";

export const serverViews = [
  ["queue", "运行队列", ListOrdered],
  ["batches", "实验批次", Layers3],
  ["codex", "Codex 工作区", MessageSquare],
  ["archives", "Agent 归档", Archive],
];
export const globalViews = [
  ["resources", "服务器资源", Server],
  ["settings", "通用设置", Settings2],
];

export default function Sidebar({
  servers,
  selectedServer,
  route,
  mobileOpen,
  expanded,
  onToggle,
  navigate,
}) {
  const [collapsedServer, setCollapsedServer] = useState("");
  const scoped = serverViews.some(([key]) => key === route.view);
  function viewLink([key, label, Icon], server) {
    const selected =
      route.view === key && (!server || selectedServer === server);
    return (
      <a
        key={key}
        href={`#${new URLSearchParams({ view: key, ...(server ? { server } : {}) })}`}
        aria-label={label}
        title={server ? `${server} · ${label}` : label}
        className={selected ? "selected" : ""}
        aria-current={selected ? "page" : undefined}
      >
        <Icon size={18} />
        <span>{label}</span>
      </a>
    );
  }
  return (
    <aside className={`sidebar ${mobileOpen ? "open" : ""}`}>
      <a
        className="brand"
        href={`#${new URLSearchParams({ view: "queue", ...(selectedServer ? { server: selectedServer } : {}) })}`}
        aria-label="DeepQueue"
        title="DeepQueue"
      >
        <span className="brand-mark">
          <Workflow size={23} />
        </span>
        <span>
          <span className="brand-name">DeepQueue</span>
          <small>实验调度工作台</small>
        </span>
      </a>
      <div className="workspace-label">服务器</div>
      <nav
        id="primary-navigation"
        className="main-navigation"
        aria-label="主导航"
      >
        <div className="sidebar-servers">
          {servers.map((server) => {
            const current = selectedServer === server.name;
            const label = serverLabel(server);
            const open = current && collapsedServer !== server.name;
            const groupId = `server-navigation-${encodeURIComponent(server.name)}`;
            return (
              <div
                className={`server-nav-group ${current ? "current" : ""}`}
                key={server.name}
              >
                <button
                  className="server-nav-button"
                  title={
                    label === server.name ? label : `${label} · ${server.name}`
                  }
                  aria-label={`服务器 ${label}`}
                  aria-expanded={open}
                  aria-controls={groupId}
                  onClick={() => {
                    if (!current || !scoped) {
                      setCollapsedServer("");
                      navigate({
                        view: scoped ? route.view : "queue",
                        server: server.name,
                      });
                    } else {
                      setCollapsedServer(open ? server.name : "");
                    }
                  }}
                >
                  <span className="server-avatar" aria-hidden="true">
                    {Array.from(label).slice(0, 2).join("").toUpperCase()}
                  </span>
                  <span className="server-nav-name">{label}</span>
                  {open ? (
                    <ChevronDown className="server-nav-chevron" size={14} />
                  ) : (
                    <ChevronRight className="server-nav-chevron" size={14} />
                  )}
                </button>
                {open && (
                  <div
                    id={groupId}
                    className="server-views"
                    role="group"
                    aria-label={`${label} 工作空间`}
                  >
                    {serverViews.map((view) => viewLink(view, server.name))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
        <div className="sidebar-global">
          {globalViews.map((view) => viewLink(view))}
        </div>
      </nav>
      <div className="sidebar-footer">
        <button
          type="button"
          className="sidebar-toggle"
          title={expanded ? "收起侧栏" : "展开侧栏"}
          aria-label={expanded ? "收起侧栏" : "展开侧栏"}
          aria-expanded={expanded}
          aria-controls="primary-navigation"
          onClick={onToggle}
        >
          {expanded ? (
            <PanelLeftClose size={18} />
          ) : (
            <PanelLeftOpen size={18} />
          )}
          <span>{expanded ? "收起" : "展开"}</span>
        </button>
      </div>
    </aside>
  );
}
