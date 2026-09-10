import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Activity,
  Archive,
  ArrowRight,
  CheckCheck,
  ChevronRight,
  CircleAlert,
  Clock3,
  Cpu,
  Download,
  LoaderCircle,
  Menu,
  MessageSquare,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Search,
  Square,
  Terminal,
  X,
} from "lucide-react";
import {
  active,
  api,
  download,
  idPath,
  serverLabel,
  terminal,
  waiting,
} from "./api.js";
import {
  archiveStates,
  Empty,
  IconButton,
  Meter,
  Priority,
  Stamp,
  Status,
} from "./components.jsx";
import Detail from "./Detail.jsx";
import Servers from "./Servers.jsx";
import { ServerForm, Submit } from "./Submit.jsx";
import Settings from "./Settings.jsx";
import Login from "./Login.jsx";
import { ServerAccess } from "./Deployment.jsx";
import Codex from "./Codex.jsx";
import Sidebar, { serverViews, globalViews } from "./Sidebar.jsx";

const views = [...serverViews, ...globalViews];

function readRoute() {
  const value = Object.fromEntries(new URLSearchParams(location.hash.slice(1)));
  if (!views.some(([key]) => key === value.view)) value.view = "queue";
  return value;
}

export default function App() {
  const [route, setRoute] = useState(readRoute);
  const isServerView = serverViews.some(([key]) => key === route.view);
  const requestedServer = isServerView ? route.server || null : null;
  const requestScope = useRef(requestedServer);
  requestScope.current = requestedServer;
  const [lastServer, setLastServer] = useState(() => {
    try {
      return localStorage.getItem("deepqueue.selected-server") || "";
    } catch {
      return "";
    }
  });
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [connected, setConnected] = useState(true);
  const [busy, setBusy] = useState(false);
  const [modal, setModal] = useState(null);
  const [mobileNav, setMobileNav] = useState(false);
  const [sidebarExpanded, setSidebarExpanded] = useState(() => {
    try {
      return localStorage.getItem("deepqueue.sidebar-expanded") === "true";
    } catch {
      return false;
    }
  });
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [refreshing, setRefreshing] = useState(false);
  const [needsLogin, setNeedsLogin] = useState(false);
  const [accessServer, setAccessServer] = useState(null);
  const [editServer, setEditServer] = useState(null);
  const authEpoch = useRef(0);
  const authPending = useRef(false);
  const refresh = useCallback(async (signal) => {
    if (authPending.current) return;
    const epoch = authEpoch.current;
    const server = requestScope.current;
    try {
      const next = await api(
        `/state${server ? `?server=${idPath(server)}` : ""}`,
        undefined,
        { signal },
      );
      if (
        epoch !== authEpoch.current ||
        signal?.aborted ||
        server !== requestScope.current
      )
        return;
      setData(next);
      setConnected(true);
      setNeedsLogin(false);
    } catch (error) {
      if (epoch !== authEpoch.current || server !== requestScope.current)
        return;
      if (error.name !== "AbortError") {
        if (error.status === 401) {
          setNeedsLogin(true);
          setData(null);
        }
        setError(error.message);
        setConnected(false);
      }
    }
  }, []);
  function changeAuth(pending) {
    authEpoch.current += 1;
    authPending.current = pending;
    if (!pending) refresh();
  }
  useEffect(() => {
    const controller = new AbortController();
    let timer;
    async function poll() {
      await refresh(controller.signal);
      if (!controller.signal.aborted) timer = setTimeout(poll, 3000);
    }
    poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [refresh, requestedServer]);
  useEffect(() => {
    const onHash = () => {
      setRoute(readRoute());
      setMobileNav(false);
    };
    addEventListener("hashchange", onHash);
    return () => removeEventListener("hashchange", onHash);
  }, []);
  useEffect(() => {
    try {
      localStorage.setItem(
        "deepqueue.sidebar-expanded",
        String(sidebarExpanded),
      );
    } catch {
      // Navigation still works when browser storage is unavailable.
    }
  }, [sidebarExpanded]);
  function navigate(next) {
    location.hash = new URLSearchParams(next).toString();
  }
  async function mutate(path, body) {
    setBusy(true);
    setError("");
    try {
      const result = await api(path, body);
      await refresh();
      return result;
    } catch (error) {
      setError(error.message);
      throw error;
    } finally {
      setBusy(false);
    }
  }
  function controlJob(id, body) {
    return mutate(`/jobs/${idPath(id)}/control`, body).catch(() => {});
  }
  function controlBatch(name, body) {
    return mutate(
      `/batch/control?name=${idPath(name)}&server=${idPath(currentServerName)}`,
      body,
    ).catch(() => {});
  }
  const currentServerName =
    requestedServer ||
    (data?.servers.some((server) => server.name === lastServer)
      ? lastServer
      : data?.servers[0]?.name) ||
    "";
  const currentServer = data?.servers.find(
    (server) => server.name === currentServerName,
  );
  useEffect(() => {
    if (!data || !isServerView || route.server || !currentServer) return;
    const origin =
      route.job && data.jobs.find((job) => job.id === route.job)?.server;
    const next = { ...route, server: origin || currentServer.name };
    history.replaceState(null, "", `#${new URLSearchParams(next)}`);
    setRoute(next);
  }, [data, isServerView, route, currentServer]);
  useEffect(() => {
    if (!currentServer) return;
    setLastServer(currentServer.name);
    try {
      localStorage.setItem("deepqueue.selected-server", currentServer.name);
    } catch {
      /* Server selection remains usable without browser storage. */
    }
  }, [currentServer?.name]);
  useEffect(() => {
    setSearch("");
    setStatusFilter("all");
    setModal(null);
  }, [requestedServer]);
  const jobs = useMemo(
    () =>
      (data?.jobs || []).filter(
        (job) => !isServerView || job.server === currentServerName,
      ),
    [data?.jobs, isServerView, currentServerName],
  );
  const batches = data?.batches || [];
  const counts = useMemo(
    () => ({
      waiting: jobs.filter((job) => waiting.includes(job.status)).length,
      running: jobs.filter((job) => active.includes(job.status)).length,
      succeeded: jobs.filter((job) => job.status === "succeeded").length,
      archived: jobs.filter((job) => job.archive_status === "completed").length,
      failed: jobs.filter((job) =>
        ["failed", "blocked", "lost"].includes(job.status),
      ).length,
    }),
    [jobs],
  );
  const visibleJobs = useMemo(
    () =>
      jobs
        .filter((job) => {
          if (route.batch && job.batch_name !== route.batch) return false;
          if (
            search &&
            !`${job.title} ${job.id} ${job.command} ${job.batch_name || ""}`
              .toLowerCase()
              .includes(search.toLowerCase())
          )
            return false;
          if (statusFilter === "waiting" && !waiting.includes(job.status))
            return false;
          if (statusFilter === "running" && !active.includes(job.status))
            return false;
          if (statusFilter === "finished" && !terminal.includes(job.status))
            return false;
          if (
            statusFilter === "failed" &&
            !["failed", "blocked", "lost"].includes(job.status)
          )
            return false;
          return true;
        })
        .sort((a, b) => {
          const group = (job) =>
            active.includes(job.status)
              ? 0
              : waiting.includes(job.status)
                ? 1
                : 2;
          return (
            group(a) - group(b) ||
            (a.position ?? Infinity) - (b.position ?? Infinity) ||
            b.priority - a.priority ||
            b.created_at - a.created_at
          );
        }),
    [jobs, route.batch, search, statusFilter],
  );
  const viewIndex = views.findIndex(([key]) => key === route.view);
  const title = views[viewIndex][1];
  const selectJob = (id, detail = "overview") =>
    navigate({ ...route, job: id, detail });
  const navigation = (
    <IconButton
      className="mobile-menu"
      icon={Menu}
      label="展开导航"
      aria-expanded={mobileNav}
      onClick={() => setMobileNav(true)}
    />
  );

  if (needsLogin)
    return (
      <Login
        onAuthChange={changeAuth}
        onLogin={async () => {
          setError("");
          await refresh();
        }}
      />
    );

  return (
    <div className={`app-shell ${sidebarExpanded ? "sidebar-expanded" : ""}`}>
      {mobileNav && (
        <button
          className="nav-scrim"
          aria-label="关闭导航"
          onClick={() => setMobileNav(false)}
        />
      )}
      <Sidebar
        servers={data?.servers || []}
        selectedServer={currentServerName}
        route={route}
        mobileOpen={mobileNav}
        expanded={sidebarExpanded}
        onToggle={() => setSidebarExpanded((expanded) => !expanded)}
        navigate={navigate}
      />

      <div className="workspace">
        <main>
          {(route.view !== "codex" || !data) && (
            <header className="workspace-toolbar">
              {navigation}
              <div className="workspace-heading">
                {isServerView && currentServerName && (
                  <span className="workspace-server" title={currentServerName}>
                    {serverLabel(currentServer) || currentServerName}
                  </span>
                )}
                <h1>{title}</h1>
              </div>
              <div className="heading-actions">
                <IconButton
                  icon={refreshing ? LoaderCircle : RefreshCw}
                  label="刷新"
                  className={refreshing ? "spinning" : ""}
                  disabled={refreshing}
                  onClick={async () => {
                    setRefreshing(true);
                    await refresh();
                    setRefreshing(false);
                  }}
                />
                {!["settings", "codex"].includes(route.view) && (
                  <button
                    className="button primary"
                    disabled={!data || (isServerView && !currentServer)}
                    onClick={() =>
                      setModal(route.view === "resources" ? "server" : "submit")
                    }
                  >
                    <Plus size={16} />
                    {route.view === "resources" ? "接入服务器" : "提交实验"}
                  </button>
                )}
              </div>
            </header>
          )}
          {error && (
            <div className="error-banner" role="alert">
              <CircleAlert size={18} />
              <span>{error}</span>
              <IconButton
                icon={X}
                label="关闭错误"
                onClick={() => setError("")}
              />
            </div>
          )}
          <div
            className={`view-content ${route.view === "codex" ? "view-content-codex" : ""}`}
            key={`${route.view}:${requestedServer || ""}`}
          >
            {!data ? (
              <Empty title={connected ? "正在读取队列" : "无法连接队列服务"}>
                <button className="button" onClick={() => refresh()}>
                  重新连接
                </button>
              </Empty>
            ) : data.scope_server !== requestedServer ? (
              <Empty title="正在读取服务器数据" />
            ) : isServerView && !currentServer ? (
              <Empty
                title={route.server ? "未找到此服务器" : "先接入一台服务器"}
              >
                <a className="button" href="#view=resources">
                  管理服务器
                </a>
              </Empty>
            ) : (
              <>
                {route.view === "settings" ? (
                  <Settings
                    onSaved={refresh}
                    onAuthChange={changeAuth}
                    data={data}
                    connected={connected}
                    schedulerBusy={busy}
                    onSchedulerToggle={() =>
                      mutate("/daemon", {
                        action: data.daemon?.running ? "stop" : "start",
                      }).catch(() => {})
                    }
                    onLogout={async () => {
                      changeAuth(true);
                      try {
                        await api("/auth/logout", {});
                        setData(null);
                        setNeedsLogin(true);
                      } catch (error) {
                        setError(error.message);
                      } finally {
                        changeAuth(false);
                      }
                    }}
                  />
                ) : route.view === "codex" ? (
                  <Codex
                    servers={data.servers}
                    route={{ ...route, server: currentServerName }}
                    navigate={navigate}
                    onSaved={refresh}
                    navigation={navigation}
                  />
                ) : (
                  <section className="metrics" aria-label="实验统计">
                    {[
                      [Clock3, "等待调度", counts.waiting, "neutral"],
                      [Activity, "正在运行", counts.running, "green"],
                      [CheckCheck, "已完成", counts.succeeded, "blue"],
                      [Archive, "Agent 归档", counts.archived, "violet"],
                    ].map(([Icon, label, value, tone]) => (
                      <div className={`metric ${tone}`} key={label}>
                        <div>
                          <span>{label}</span>
                          <Icon size={17} />
                        </div>
                        <strong>{String(value).padStart(2, "0")}</strong>
                        <div className="metric-track">
                          <span
                            style={{
                              width: `${jobs.length ? Math.max(4, (value / jobs.length) * 100) : 0}%`,
                            }}
                          />
                        </div>
                      </div>
                    ))}
                  </section>
                )}
                {data.truncated &&
                  !["settings", "codex"].includes(route.view) && (
                    <div className="notice">
                      当前显示最近查询范围内的 10,000 个实验。
                    </div>
                  )}
                {route.view === "queue" && (
                  <section className="queue-section">
                    <div className="section-heading">
                      <h2>
                        {route.batch || "全部实验"}
                        <span>{visibleJobs.length}</span>
                      </h2>
                      <span className="subtle">
                        {currentServer.config.enabled
                          ? "参与调度"
                          : "服务器已暂停调度"}
                      </span>
                    </div>
                    <div className="queue-toolbar">
                      <div
                        className="tabs"
                        role="tablist"
                        aria-label="实验状态"
                      >
                        {[
                          ["all", "全部"],
                          ["waiting", "等待中"],
                          ["running", "运行中"],
                          ["finished", "已结束"],
                          ["failed", "异常"],
                        ].map(([key, label]) => (
                          <button
                            role="tab"
                            aria-selected={statusFilter === key}
                            key={key}
                            className={statusFilter === key ? "active" : ""}
                            onClick={() => setStatusFilter(key)}
                          >
                            {label}
                          </button>
                        ))}
                      </div>
                      <div className="filters">
                        <label className="search">
                          <Search size={15} />
                          <input
                            aria-label="搜索实验"
                            placeholder="搜索实验"
                            value={search}
                            onChange={(event) => setSearch(event.target.value)}
                          />
                        </label>
                        <select
                          aria-label="批次筛选"
                          value={route.batch || ""}
                          onChange={(event) =>
                            navigate({
                              view: "queue",
                              server: currentServerName,
                              ...(event.target.value
                                ? { batch: event.target.value }
                                : {}),
                            })
                          }
                        >
                          <option value="">全部批次</option>
                          {batches.map((batch) => (
                            <option key={batch.name}>{batch.name}</option>
                          ))}
                        </select>
                      </div>
                    </div>
                    <div className="table-scroll">
                      <table className="job-table">
                        <thead>
                          <tr>
                            <th className="position-col">顺序</th>
                            <th>实验</th>
                            <th>状态</th>
                            <th>资源</th>
                            <th>优先级</th>
                            <th>提交时间</th>
                            <th className="actions-col">操作</th>
                          </tr>
                        </thead>
                        <tbody>
                          {visibleJobs.map((job) => (
                            <tr
                              key={job.id}
                              className={
                                route.job === job.id ? "selected-row" : ""
                              }
                            >
                              <td>
                                <span
                                  className={
                                    job.status === "running"
                                      ? "running-position"
                                      : "position"
                                  }
                                >
                                  {job.status === "running" ? (
                                    <Activity size={17} />
                                  ) : job.position ? (
                                    String(job.position).padStart(2, "0")
                                  ) : (
                                    "--"
                                  )}
                                </span>
                              </td>
                              <td className="job-name">
                                <button onClick={() => selectJob(job.id)}>
                                  {job.title}
                                </button>
                                <small>
                                  {job.batch_name || "独立实验"}
                                  {job.parent_job_id && (
                                    <> · 改进第 {job.improvement_round} 轮</>
                                  )}
                                  {job.completion_mode === "improve" && (
                                    <> · 继续改进</>
                                  )}
                                  <span>·</span>
                                  {job.id.slice(0, 8)}
                                </small>
                                {job.reason && (
                                  <p className="wait-reason" title={job.reason}>
                                    {job.reason}
                                  </p>
                                )}
                              </td>
                              <td>
                                <Status
                                  status={job.status}
                                  held={job.pause_sources.length > 0}
                                />
                                <small className="cell-secondary">
                                  {job.status === "starting" &&
                                  job.launch_retries > 0
                                    ? `自动修复 ${job.launch_retries} / ${data.launch_max_retries}`
                                    : job.status === "starting" &&
                                        job.launch_status === "running"
                                      ? "Agent 启动适配中"
                                      : job.archive_status !== "skipped"
                                        ? archiveStates[job.archive_status]
                                        : ""}
                                </small>
                              </td>
                              <td>
                                <span className="resource-cell">
                                  <Cpu size={14} />
                                  {job.resources
                                    ? `${job.resources.gpu_count} GPU`
                                    : "待建模"}
                                </span>
                                <small className="cell-secondary">
                                  {serverLabel(
                                    data.servers.find(
                                      (server) => server.name === job.server,
                                    ),
                                  ) || job.server}
                                </small>
                              </td>
                              <td>
                                <Priority
                                  value={job.priority}
                                  disabled={
                                    busy || !waiting.includes(job.status)
                                  }
                                  onChange={(priority) =>
                                    controlJob(job.id, {
                                      action: "priority",
                                      priority,
                                    })
                                  }
                                />
                              </td>
                              <td className="date-cell">
                                <Stamp value={job.created_at} />
                              </td>
                              <td>
                                <div className="row-actions">
                                  {job.run_id && (
                                    <button
                                      type="button"
                                      className="button queue-log-link"
                                      aria-label={`查看 ${job.title} 实时日志`}
                                      onClick={() => selectJob(job.id, "logs")}
                                    >
                                      <Terminal size={14} />
                                      实时日志
                                    </button>
                                  )}
                                  {waiting.includes(job.status) && (
                                    <IconButton
                                      icon={job.paused ? Play : Pause}
                                      label={
                                        job.paused ? "恢复实验" : "暂停实验"
                                      }
                                      disabled={busy}
                                      onClick={() =>
                                        controlJob(job.id, {
                                          action: job.paused
                                            ? "resume"
                                            : "pause",
                                        })
                                      }
                                    />
                                  )}
                                  {!terminal.includes(job.status) && (
                                    <IconButton
                                      icon={Square}
                                      label="取消实验"
                                      disabled={busy}
                                      onClick={() => {
                                        if (
                                          confirm(`取消实验「${job.title}」？`)
                                        )
                                          controlJob(job.id, {
                                            action: "cancel",
                                          });
                                      }}
                                    />
                                  )}
                                  <IconButton
                                    icon={ArrowRight}
                                    label={`查看 ${job.title}`}
                                    onClick={() => selectJob(job.id)}
                                  />
                                </div>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    {!visibleJobs.length && (
                      <Empty
                        title={jobs.length ? "没有符合条件的实验" : "队列为空"}
                      >
                        {!jobs.length && (
                          <button
                            className="button"
                            onClick={() => setModal("submit")}
                          >
                            <Plus size={15} />
                            提交实验
                          </button>
                        )}
                      </Empty>
                    )}
                    <div className="table-footer">
                      <span>{visibleJobs.length} 个实验</span>
                    </div>
                  </section>
                )}
                {route.view === "batches" && (
                  <section className="batch-section">
                    <div className="section-heading">
                      <h2>
                        实验批次<span>{batches.length}</span>
                      </h2>
                    </div>
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            <th>批次</th>
                            <th>进度</th>
                            <th>状态</th>
                            <th>待运行优先级</th>
                            <th>操作</th>
                          </tr>
                        </thead>
                        <tbody>
                          {batches.map((batch) => {
                            const members = jobs.filter(
                              (job) => job.batch_name === batch.name,
                            );
                            const finished = members.filter((job) =>
                              terminal.includes(job.status),
                            ).length;
                            const next = members.find((job) =>
                              waiting.includes(job.status),
                            );
                            return (
                              <tr key={batch.name}>
                                <td className="job-name">
                                  <button
                                    onClick={() => {
                                      setStatusFilter("all");
                                      navigate({
                                        view: "queue",
                                        server: currentServerName,
                                        batch: batch.name,
                                      });
                                    }}
                                  >
                                    {batch.name}
                                  </button>
                                  <small>
                                    <Stamp value={batch.created_at} />
                                  </small>
                                </td>
                                <td className="batch-progress">
                                  <span>
                                    {finished} / {members.length}
                                  </span>
                                  <Meter
                                    label={`${batch.name} 进度`}
                                    value={
                                      members.length
                                        ? (finished / members.length) * 100
                                        : 0
                                    }
                                  />
                                </td>
                                <td>
                                  {batch.paused ? (
                                    <Status held />
                                  ) : (
                                    <span className="subtle">
                                      {finished === members.length
                                        ? "已结束"
                                        : `${members.filter((job) => active.includes(job.status)).length} 个运行中`}
                                    </span>
                                  )}
                                </td>
                                <td>
                                  <Priority
                                    value={next?.priority ?? 0}
                                    disabled={busy || !next}
                                    onChange={(priority) =>
                                      controlBatch(batch.name, {
                                        action: "priority",
                                        priority,
                                      })
                                    }
                                  />
                                </td>
                                <td>
                                  <div className="row-actions">
                                    <IconButton
                                      icon={batch.paused ? Play : Pause}
                                      label={
                                        batch.paused ? "恢复批次" : "暂停批次"
                                      }
                                      disabled={busy}
                                      onClick={() =>
                                        controlBatch(batch.name, {
                                          action: batch.paused
                                            ? "resume"
                                            : "pause",
                                        })
                                      }
                                    />
                                    <IconButton
                                      icon={Download}
                                      label="下载批次报告"
                                      onClick={async () => {
                                        try {
                                          download(
                                            `${batch.name}.json`,
                                            await api(
                                              `/batch/report?name=${idPath(batch.name)}&server=${idPath(currentServerName)}`,
                                            ),
                                          );
                                        } catch (error) {
                                          setError(error.message);
                                        }
                                      }}
                                    />
                                    <IconButton
                                      icon={Square}
                                      label="取消批次"
                                      disabled={
                                        busy || finished === members.length
                                      }
                                      onClick={() => {
                                        if (
                                          confirm(
                                            `取消服务器「${currentServerName}」上批次「${batch.name}」中所有未结束的实验？`,
                                          )
                                        )
                                          controlBatch(batch.name, {
                                            action: "cancel",
                                          });
                                      }}
                                    />
                                    <IconButton
                                      icon={ArrowRight}
                                      label={`打开批次 ${batch.name}`}
                                      onClick={() =>
                                        navigate({
                                          view: "queue",
                                          server: currentServerName,
                                          batch: batch.name,
                                        })
                                      }
                                    />
                                  </div>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                    {!batches.length && (
                      <Empty title="暂无实验批次">
                        <button
                          className="button"
                          onClick={() => setModal("submit")}
                        >
                          <Plus size={15} />
                          提交批次
                        </button>
                      </Empty>
                    )}
                  </section>
                )}
                {route.view === "resources" && (
                  <Servers
                    servers={data.servers}
                    jobs={jobs}
                    busy={busy}
                    mutate={mutate}
                    navigate={navigate}
                    onEdit={setEditServer}
                    onAccess={setAccessServer}
                  />
                )}
                {route.view === "archives" && (
                  <section className="archive-section">
                    <div className="section-heading">
                      <h2>
                        归档记录
                        <span>
                          {
                            jobs.filter(
                              (job) =>
                                job.archive_status !== "skipped" &&
                                terminal.includes(job.status),
                            ).length
                          }
                        </span>
                      </h2>
                    </div>
                    <div className="archive-list">
                      {jobs
                        .filter(
                          (job) =>
                            terminal.includes(job.status) &&
                            job.archive_status !== "skipped",
                        )
                        .sort((a, b) => b.updated_at - a.updated_at)
                        .map((job) => (
                          <button
                            className="archive-row"
                            key={job.id}
                            onClick={() => selectJob(job.id)}
                          >
                            <span
                              className={`archive-symbol ${job.archive_status === "failed" ? "red" : ""}`}
                            >
                              <Archive size={22} />
                            </span>
                            <span className="archive-title">
                              <strong>{job.title}</strong>
                              <small>
                                {job.batch_name || "独立实验"}
                                <span>·</span>
                                {serverLabel(
                                  data.servers.find(
                                    (server) => server.name === job.server,
                                  ),
                                ) || job.server}
                                {job.source_thread_id && (
                                  <>
                                    <span>·</span>来源对话
                                  </>
                                )}
                              </small>
                            </span>
                            <Status status={job.status} />
                            <span
                              className={`archive-state ${job.archive_status === "failed" ? "error-text" : ""}`}
                            >
                              {archiveStates[job.archive_status]}
                            </span>
                            <Stamp value={job.updated_at} />
                            <ChevronRight size={18} />
                          </button>
                        ))}
                    </div>
                    {!jobs.some(
                      (job) =>
                        terminal.includes(job.status) &&
                        job.archive_status !== "skipped",
                    ) && <Empty title="暂无归档记录" />}
                  </section>
                )}
              </>
            )}
          </div>
        </main>
      </div>
      {route.job && (
        <Detail
          id={route.job}
          initialTab={route.detail || "overview"}
          serverName={serverLabel(
            data?.servers.find(
              (server) =>
                server.name ===
                data.jobs.find((job) => job.id === route.job)?.server,
            ),
          )}
          onTabChange={(detail) => navigate({ ...route, detail })}
          busy={busy}
          onClose={() => {
            const { job, detail, ...next } = route;
            navigate(next);
          }}
          onControl={(id, body) => mutate(`/jobs/${idPath(id)}/control`, body)}
          updatedAt={data?.jobs.find((job) => job.id === route.job)?.updated_at}
          onServer={(server) => {
            if (isServerView && route.server !== server)
              navigate({ ...route, server });
          }}
        />
      )}
      {modal === "submit" && (
        <Submit
          data={data}
          server={currentServer}
          onClose={() => setModal(null)}
          onSubmit={async (path, body) => {
            const result = await mutate(
              `${path}?server=${idPath(currentServerName)}`,
              body,
            );
            setModal(null);
            navigate(
              result.id
                ? { view: "queue", server: currentServerName, job: result.id }
                : {
                    view: "queue",
                    server: currentServerName,
                    batch: result.name,
                  },
            );
          }}
        />
      )}
      {modal === "server" && (
        <ServerForm
          onClose={() => setModal(null)}
          onSubmit={async (body) => {
            await mutate("/servers", body);
            setModal(null);
          }}
        />
      )}
      {editServer && (
        <ServerForm
          server={editServer}
          onClose={() => setEditServer(null)}
          onSubmit={async (body) => {
            await mutate(`/servers/${idPath(editServer.name)}/settings`, body);
            setEditServer(null);
          }}
        />
      )}
      {accessServer && (
        <ServerAccess
          server={accessServer}
          publicUrl={data.public_url}
          onClose={() => setAccessServer(null)}
          onSaved={refresh}
        />
      )}
    </div>
  );
}
