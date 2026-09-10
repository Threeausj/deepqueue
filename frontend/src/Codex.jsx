import React, {
  lazy,
  Suspense,
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import Markdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowUp,
  Brain,
  Check,
  ChevronDown,
  CircleAlert,
  Copy,
  Cpu,
  ExternalLink,
  FolderOpen,
  GitFork,
  LoaderCircle,
  MessageSquare,
  MoreHorizontal,
  PanelLeft,
  PanelRight,
  Files,
  Monitor,
  Plug,
  Plus,
  Pencil,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  Square,
  Terminal,
  Unplug,
  X,
} from "lucide-react";
import { api, idPath, serverLabel } from "./api.js";
import { effortLabel } from "./AgentModels.jsx";
import { Empty, IconButton, Modal } from "./components.jsx";
import { applyCodexEvent } from "./codexEvents.js";
import { conversationFile } from "./codexFiles.js";
import CodexTree from "./CodexTree.jsx";
import CodexComposer from "./CodexComposer.jsx";
const WorkspacePanels = lazy(() => import("./WorkspacePanels.jsx"));
import {
  cachedHistory,
  cacheHistory,
  historyKey,
  readDraft,
  saveDraft,
} from "./codexCache.js";
import { projectTree, titleOf } from "./codexTree.js";
import { AccessDialog, PermissionFields, accessLabel } from "./CodexAccess.jsx";

const defaults = {
  enabled: false,
  executable: "codex",
  socket: "auto",
  cwd: null,
};
const connectionLabels = {
  connected: "已连接",
  connecting: "连接中",
  disconnected: "未连接",
  error: "连接中断",
};

export default function Codex({
  servers,
  route,
  navigate,
  onSaved,
  navigation,
}) {
  const server =
    servers.find((item) => item.name === route.server) ||
    (!route.server ? servers[0] : null);
  if (!server)
    return (
      <section className="codex-workspace" aria-label="Codex 工作区">
        <div className="codex-toolbar workspace-toolbar">
          {navigation}
          <a className="button" href="#view=resources">
            管理服务器
          </a>
        </div>
        <Empty title={route.server ? "未找到此服务器" : "先接入一台服务器"} />
      </section>
    );
  return (
    <Workspace
      key={server.name}
      {...{ server, servers, route, navigate, onSaved, navigation }}
    />
  );
}

function Workspace({ server, servers, route, navigate, onSaved, navigation }) {
  const base = `/servers/${idPath(server.name)}/codex`;
  const [status, setStatus] = useState({
    state: "disconnected",
    config: server.config.codex || defaults,
    pending: [],
  });
  const [settings, setSettings] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [threads, setThreads] = useState([]);
  const [projects, setProjects] = useState([]);
  const treeKey = `deepqueue.codex.sidebar.${server.name}`;
  const [treeOpen, setTreeOpen] = useState(() => {
    try {
      return localStorage.getItem(treeKey) !== "false";
    } catch {
      return true;
    }
  });
  const panelKey = `deepqueue.codex.tools.${server.name}`;
  const [panelOpen, setPanelOpen] = useState(false);
  const [panelTab, setPanelTab] = useState("files");
  const [previewRequest, setPreviewRequest] = useState(null);
  const previewFile = useCallback((target) => {
    setPreviewRequest({
      ...target,
      threadId: currentThread.current,
      requestId: crypto.randomUUID(),
    });
    setPanelTab("preview");
    setPanelOpen(true);
  }, []);
  const [panelWidth, setPanelWidth] = useState(() => {
    try {
      return Math.max(
        300,
        Math.min(680, Number(localStorage.getItem(panelKey)) || 420),
      );
    } catch {
      return 420;
    }
  });
  const [selections, setSelections] = useState([]);
  const eventVersion = useRef(0);
  const forceHistory = useRef(false);
  const [taskProject, setTaskProject] = useState("");
  const [listLoading, setListLoading] = useState(false);
  const [cursor, setCursor] = useState(null);
  const [search, setSearch] = useState("");
  const [models, setModels] = useState([]);
  const [thread, setThread] = useState(null);
  const [loading, setLoading] = useState(false);
  const [text, setText] = useState("");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  const [newTask, setNewTask] = useState(false);
  const [cwd, setCwd] = useState(server.config.codex?.cwd || "");
  const [revision, setRevision] = useState(0);
  const [historyLimit, setHistoryLimit] = useState(40);
  const [copied, setCopied] = useState(false);
  const [accessOpen, setAccessOpen] = useState(false);
  const [newAccess, setNewAccess] = useState({});
  const [rename, setRename] = useState(null);
  const forkRequests = useRef(new Map());
  const currentThread = useRef(route.thread);
  currentThread.current = route.thread;
  const currentSearch = useRef(search);
  currentSearch.current = search;
  const listEpoch = useRef(0);
  const projectEpoch = useRef(0);
  const alive = useRef(true);
  const output = useRef(null);
  const follow = useRef(true);
  const ready = status.state === "connected";
  const groups = useMemo(
    () => projectTree(threads, projects, status.runtime?.codexHome),
    [threads, projects, status.runtime?.codexHome],
  );
  const active = thread?.turns?.findLast(
    (turn) => turn.status === "inProgress",
  );
  const options = models.find(
    (item) => (item.model || item.id) === (model || thread?.model),
  );
  const efforts = options?.supportedReasoningEfforts || [];
  const pending = (status.pending || []).filter(
    (item) => item.params?.threadId === route.thread,
  );

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    const screen = window.matchMedia("(max-width: 760px)");
    const resize = () => {
      if (screen.matches) setTreeOpen(!currentThread.current);
      else {
        try {
          setTreeOpen(localStorage.getItem(treeKey) !== "false");
        } catch {
          setTreeOpen(true);
        }
      }
    };
    resize();
    screen.addEventListener("change", resize);
    return () => screen.removeEventListener("change", resize);
  }, []);
  function toggleTree() {
    const next = !treeOpen;
    setTreeOpen(next);
    if (!window.matchMedia("(max-width: 760px)").matches) {
      try {
        localStorage.setItem(treeKey, String(next));
      } catch {
        /* Optional storage. */
      }
    }
  }
  function openPanel(tab) {
    setPanelTab(tab);
    setPanelOpen(true);
  }
  function resizePanel(event) {
    event.preventDefault();
    const start = event.clientX,
      width = panelWidth;
    const move = (event) =>
      setPanelWidth(
        Math.max(300, Math.min(680, width + start - event.clientX)),
      );
    const stop = (event) => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", stop);
      try {
        localStorage.setItem(
          panelKey,
          String(Math.max(300, Math.min(680, width + start - event.clientX))),
        );
      } catch {
        /* Optional storage. */
      }
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", stop, { once: true });
  }
  function openThread(id) {
    setNewTask(false);
    if (window.matchMedia("(max-width: 760px)").matches) setTreeOpen(false);
    navigate({ view: "codex", server: server.name, thread: id });
  }
  function createIn(group) {
    setNewAccess({});
    setTaskProject(group?.projectId || "");
    setCwd(group?.cwd || status.config?.cwd || "");
    setNewTask(true);
  }
  async function loadProjects() {
    const epoch = ++projectEpoch.current;
    let next = null;
    const rows = [];
    const seen = new Set();
    do {
      const query = next ? `?${new URLSearchParams({ cursor: next })}` : "";
      const result = await api(`${base}/projects${query}`);
      if (!alive.current || epoch !== projectEpoch.current) return;
      rows.push(...result.data);
      next = result.nextCursor;
      if (next && (seen.has(next) || seen.size >= 100))
        throw new Error("项目列表分页异常，请刷新后重试");
      seen.add(next);
    } while (next);
    setProjects([...new Map(rows.map((item) => [item.id, item])).values()]);
  }
  async function refreshTree() {
    await Promise.all([list(), loadProjects()]);
  }
  async function run(action) {
    setBusy(true);
    setError("");
    try {
      return await action();
    } catch (error) {
      if (alive.current) setError(error.message);
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  async function list(nextCursor = null) {
    const epoch = ++listEpoch.current;
    const query = new URLSearchParams({ search: currentSearch.current });
    if (nextCursor) query.set("cursor", nextCursor);
    setListLoading(true);
    try {
      const result = await api(`${base}/threads?${query}`);
      if (!alive.current || epoch !== listEpoch.current) return;
      setThreads((old) => {
        const rows = nextCursor ? [...old, ...result.data] : result.data;
        return [...new Map(rows.map((item) => [item.id, item])).values()];
      });
      setCursor(result.nextCursor);
    } finally {
      if (alive.current && epoch === listEpoch.current) setListLoading(false);
    }
  }

  useEffect(() => {
    const events = new EventSource(`/api${base}/events`);
    events.onopen = () => {
      setStreaming(true);
      setRevision((value) => value + 1);
    };
    events.onerror = () => setStreaming(false);
    events.onmessage = ({ data }) => {
      let event;
      try {
        event = JSON.parse(data);
      } catch {
        return;
      }
      const p = event.params || {};
      window.dispatchEvent(
        new CustomEvent("deepqueue:workspace-event", {
          detail: { base, event },
        }),
      );
      if (
        (p.threadId || p.thread?.id) === currentThread.current &&
        /^(thread|turn|item)\//.test(event.method)
      )
        eventVersion.current += 1;
      if (["deepqueue/status", "deepqueue/resync"].includes(event.method)) {
        setStatus(p);
        if (event.method === "deepqueue/resync")
          setRevision((value) => value + 1);
      } else if (event.method === "deepqueue/unauthorized") {
        events.close();
        setStreaming(false);
        setThread(null);
        setThreads([]);
        setProjects([]);
        setError("登录已过期，请重新登录");
        onSaved();
      } else if (event.id !== undefined) {
        setStatus((old) => ({
          ...old,
          pending: [
            ...(old.pending || []).filter((item) => item.id !== event.id),
            event,
          ],
        }));
      } else if (event.method === "serverRequest/resolved") {
        setStatus((old) => ({
          ...old,
          pending: (old.pending || []).filter(
            (item) => item.id !== p.requestId,
          ),
        }));
      } else {
        setThread((old) => applyCodexEvent(old, event));
        if (
          [
            "project/changed",
            "project/deleted",
            "thread/project/updated",
          ].includes(event.method)
        )
          refreshTree().catch((error) => setError(error.message));
        if (event.method === "deepqueue/unsupportedRequest")
          setError(
            `Codex 请求 ${p.method} 暂不支持，请在原 Codex 客户端处理。`,
          );
        if (event.method === "turn/completed") {
          setStatus((old) => ({
            ...old,
            pending: (old.pending || []).filter(
              (item) => item.params?.turnId !== p.turn?.id,
            ),
          }));
          if (p.threadId === currentThread.current)
            setRevision((value) => value + 1);
        }
        if (event.method === "error" && p.threadId === currentThread.current)
          setError(p.error?.message || p.message || "Codex 执行出错");
      }
    };
    return () => events.close();
  }, [base]);

  useEffect(() => {
    if (!ready) return;
    loadProjects().catch((error) => {
      if (alive.current) setError(error.message);
    });
    return () => {
      projectEpoch.current += 1;
    };
  }, [ready, status.generation]);

  useEffect(() => {
    if (!ready) return;
    const controller = new AbortController();
    api(`${base}/models`, undefined, { signal: controller.signal })
      .then((result) => setModels(result.data || []))
      .catch((error) => {
        if (error.name !== "AbortError") setError(error.message);
      });
    return () => controller.abort();
  }, [ready, status.generation, base]);

  useEffect(() => {
    if (!ready) return;
    const timer = setTimeout(
      () =>
        list().catch((error) => {
          if (alive.current) setError(error.message);
        }),
      200,
    );
    return () => {
      clearTimeout(timer);
      listEpoch.current += 1;
    };
  }, [ready, status.generation, search, revision]);

  useEffect(() => {
    setThread(
      ready
        ? cachedHistory(historyKey(base, status.generation, route.thread))
        : null,
    );
    const draft = readDraft(`${base}:${route.thread}`);
    setText(draft.text || "");
    setSelections(draft.selections || []);
    setModel(draft.model || "");
    setEffort(draft.effort || "");
    setHistoryLimit(40);
    setAccessOpen(false);
    setRename(null);
    follow.current = true;
  }, [route.thread, status.generation]);

  useEffect(() => {
    if (ready && thread && thread.id === route.thread)
      cacheHistory(historyKey(base, status.generation, thread.id), thread);
  }, [thread, ready, status.generation]);
  useEffect(() => {
    if (thread && thread.id === route.thread)
      saveDraft(`${base}:${route.thread}`, { text, selections, model, effort });
  }, [text, selections, model, effort, thread?.id]);

  useEffect(() => {
    if (!ready || !route.thread) return;
    const controller = new AbortController();
    let timer;
    setLoading(true);
    async function read(open = false) {
      const started = eventVersion.current;
      const force = forceHistory.current;
      forceHistory.current = false;
      try {
        const result = await api(
          `${base}/threads/${idPath(route.thread)}${open ? "/open" : ""}?limit=${historyLimit}${force ? "&force=true" : ""}`,
          open ? {} : undefined,
          { signal: controller.signal },
        );
        if (!controller.signal.aborted && started === eventVersion.current)
          setThread(result.thread);
      } catch (error) {
        if (error.name !== "AbortError") setError(error.message);
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          // Repair missed events after reconnect or an external client changing this task.
          timer = setTimeout(() => read(), 15000);
        }
      }
    }
    read(true);
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [ready, status.generation, route.thread, revision, historyLimit]);

  useEffect(() => {
    if (follow.current && output.current)
      output.current.scrollTop = output.current.scrollHeight;
  }, [thread, pending.length]);

  async function send(event) {
    event.preventDefault();
    if (!text.trim() || !thread) return;
    const value = text;
    const taskId = thread.id;
    const result = await run(() =>
      api(`${base}/threads/${idPath(taskId)}/messages`, {
        text: value,
        extensions: selections.map(({ kind, id }) => ({ kind, id })),
        model: model || null,
        effort: effort || null,
        request_id: crypto.randomUUID(),
      }),
    );
    if (result && currentThread.current === taskId) {
      setText("");
      setSelections([]);
      saveDraft(`${base}:${taskId}`, {});
      follow.current = true;
      setThread((old) =>
        applyCodexEvent(old, {
          method: "turn/started",
          params: { threadId: taskId, turn: result.turn },
        }),
      );
      setRevision((value) => value + 1);
    }
  }

  async function fork(lastTurnId = null) {
    const taskId = thread.id;
    const key = JSON.stringify([taskId, lastTurnId, model, effort]);
    if (!forkRequests.current.has(key))
      forkRequests.current.set(key, crypto.randomUUID());
    const result = await run(() =>
      api(`${base}/threads/${idPath(taskId)}/fork`, {
        last_turn_id: lastTurnId,
        model: model || null,
        effort: effort || null,
        request_id: forkRequests.current.get(key),
      }),
    );
    if (!result || !alive.current) return;
    forkRequests.current.delete(key);
    if (currentThread.current === taskId) {
      setSearch("");
      openThread(result.thread.id);
    }
    if (result.warnings?.length) setError(result.warnings.join("\n"));
    setRevision((value) => value + 1);
  }

  return (
    <section className="codex-workspace" aria-label="Codex 工作区">
      <div className="codex-toolbar workspace-toolbar">
        {navigation}
        <IconButton
          icon={PanelLeft}
          label={treeOpen ? "收起任务目录" : "展开任务目录"}
          aria-expanded={treeOpen}
          onClick={toggleTree}
        />
        <div className="codex-host-picker">
          <label>
            <select
              aria-label="Codex 服务器"
              value={server.name}
              onChange={(event) =>
                navigate({ view: "codex", server: event.target.value })
              }
            >
              {servers.map((item) => (
                <option key={item.name} value={item.name}>
                  {serverLabel(item)}
                </option>
              ))}
            </select>
          </label>
        </div>
        <span className={`codex-connection ${ready ? "online" : ""}`}>
          <span className={`dot ${ready ? "green" : "gray"}`} />
          {connectionLabels[status.state] || "未连接"}
          {ready && !streaming ? " · 正在恢复实时连接" : ""}
        </span>
        <div className="codex-tool-triggers">
          <IconButton
            icon={Terminal}
            label="打开终端"
            disabled={!ready || !thread}
            onClick={() => openPanel("terminal")}
          />
          <IconButton
            icon={Files}
            label="打开项目文件"
            disabled={!ready || !thread}
            onClick={() => openPanel("files")}
          />
          <IconButton
            icon={Monitor}
            label="打开预览"
            disabled={!ready || !thread}
            onClick={() => openPanel("preview")}
          />
          <IconButton
            icon={PanelRight}
            label={panelOpen ? "收起工具面板" : "展开工具面板"}
            aria-expanded={panelOpen}
            disabled={!thread}
            onClick={() => setPanelOpen((value) => !value)}
          />
        </div>
        <div className="row-actions">
          <button
            className="button"
            onClick={() => setSettings({ ...defaults, ...status.config })}
          >
            <Settings2 size={15} />
            连接设置
          </button>
          <button
            className={`button ${ready ? "" : "primary"}`}
            disabled={busy || status.state === "connecting"}
            onClick={() => {
              if (!status.config?.enabled) {
                setSettings({ ...defaults, ...status.config, enabled: true });
                return;
              }
              run(async () => {
                setStatus(
                  await api(`${base}/${ready ? "disconnect" : "connect"}`, {}),
                );
              });
            }}
          >
            {busy ? (
              <LoaderCircle className="spinning" size={15} />
            ) : ready ? (
              <Unplug size={15} />
            ) : (
              <Plug size={15} />
            )}
            {ready ? "断开连接" : "连接 Codex"}
          </button>
        </div>
      </div>

      {(error || status.error) && (
        <div className="error-banner" role="alert">
          <CircleAlert size={17} />
          <span>{error || status.error}</span>
          <IconButton
            icon={X}
            label="关闭 Codex 错误"
            onClick={() => {
              setError("");
              setStatus((old) => ({ ...old, error: null }));
            }}
          />
        </div>
      )}
      {(status.pending || []).some(
        (item) => item.params?.threadId !== route.thread,
      ) && (
        <div className="codex-pending-links">
          有任务等待你的回复：
          {[
            ...new Set(
              status.pending
                .map((item) => item.params?.threadId)
                .filter(Boolean),
            ),
          ].map((id) => (
            <button
              className="text-button"
              key={id}
              onClick={() => openThread(id)}
            >
              {threads.find((item) => item.id === id)?.name || id.slice(0, 12)}
            </button>
          ))}
        </div>
      )}

      <div
        className={`codex-layout ${treeOpen ? "tree-open" : "tree-closed"} ${panelOpen && thread ? "tools-open" : ""}`}
        style={{ "--tools-width": `${panelWidth}px` }}
      >
        {treeOpen && (
          <button
            className="codex-tree-scrim"
            aria-label="关闭任务目录"
            onClick={() => setTreeOpen(false)}
          />
        )}
        {treeOpen && (
          <aside className="codex-task-panel" aria-label="任务目录">
            <div className="codex-panel-heading">
              <h2>项目与任务</h2>
              <div className="row-actions">
                <IconButton
                  icon={RefreshCw}
                  label="刷新 Codex 任务"
                  disabled={!ready || busy}
                  onClick={() => run(refreshTree)}
                />
                <IconButton
                  icon={Plus}
                  label="新建 Codex 任务"
                  disabled={!ready}
                  onClick={() => createIn()}
                />
              </div>
            </div>
            <label className="codex-search">
              <Search size={15} />
              <input
                aria-label="搜索 Codex 任务"
                placeholder="搜索任务…"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </label>
            <div className="codex-task-list" aria-busy={listLoading}>
              <CodexTree
                {...{ groups, search, ready }}
                server={server.name}
                selected={route.thread}
                onOpen={openThread}
                onNew={createIn}
              />
              {!threads.length && (
                <p className="codex-list-empty">
                  {listLoading
                    ? "正在读取任务…"
                    : ready
                      ? search
                        ? "没有匹配的任务"
                        : "还没有任务，可以新建一个。"
                      : "连接后查看此服务器的任务"}
                </p>
              )}
              {cursor && (
                <button
                  className="button codex-load-more"
                  disabled={busy || !ready || listLoading}
                  onClick={() => run(() => list(cursor))}
                >
                  <ChevronDown size={14} />
                  加载更多任务
                </button>
              )}
            </div>
          </aside>
        )}

        <div className="codex-chat">
          {thread ? (
            <>
              <div className="codex-chat-heading">
                <div>
                  <h2>{titleOf(thread)}</h2>
                  <p>
                    <FolderOpen size={13} />
                    {thread.cwd}
                    <span>·</span>
                    {thread.model || "会话模型"}
                  </p>
                </div>
                <div className="row-actions">
                  <IconButton
                    icon={RefreshCw}
                    label="刷新当前对话"
                    disabled={!ready || loading}
                    onClick={() => {
                      forceHistory.current = true;
                      setRevision((n) => n + 1);
                    }}
                  />
                  <IconButton
                    icon={copied ? Check : Copy}
                    label="复制任务 ID"
                    onClick={async () => {
                      try {
                        await navigator.clipboard.writeText(thread.id);
                        setCopied(true);
                        setTimeout(() => setCopied(false), 2000);
                      } catch {
                        setError("复制失败，请从地址栏获取任务 ID");
                      }
                    }}
                  />
                  <a
                    className="icon-button"
                    aria-label="在 Codex 中打开任务"
                    title="在 Codex 中打开任务"
                    href={`codex://threads/${encodeURIComponent(thread.id)}`}
                  >
                    <ExternalLink size={16} />
                  </a>
                  <TaskMenu>
                    <button
                      type="button"
                      disabled={
                        busy || !ready || !!active || !thread.turns?.length
                      }
                      onClick={() => fork()}
                    >
                      <GitFork size={15} />
                      分支到新聊天
                    </button>
                    <button
                      type="button"
                      disabled={busy || !ready}
                      onClick={() => setRename(titleOf(thread))}
                    >
                      <Pencil size={15} />
                      重命名聊天
                    </button>
                    <button
                      type="button"
                      disabled={busy || !ready}
                      onClick={() => setAccessOpen(true)}
                    >
                      <ShieldCheck size={15} />
                      访问权限
                    </button>
                  </TaskMenu>
                </div>
              </div>
              {thread.forkedFromId && (
                <div className="codex-fork-origin">
                  <GitFork size={13} />
                  此聊天来自分支
                  <button
                    type="button"
                    onClick={() => openThread(thread.forkedFromId)}
                  >
                    返回原聊天
                  </button>
                </div>
              )}
              <div
                className="codex-messages"
                ref={output}
                role="log"
                aria-label="Codex 对话内容"
                onScroll={() => {
                  const el = output.current;
                  follow.current =
                    el.scrollHeight - el.scrollTop - el.clientHeight < 90;
                }}
              >
                {thread.omittedTurns > 0 && (
                  <button
                    className="button codex-load-more"
                    disabled={historyLimit >= 1000}
                    onClick={() => {
                      follow.current = false;
                      setHistoryLimit((value) => Math.min(1000, value + 80));
                    }}
                  >
                    加载更早的对话（{thread.omittedTurns} 轮）
                  </button>
                )}
                {thread.turns?.map((turn) => (
                  <React.Fragment key={turn.id}>
                    {(turn.items || []).map((item) => (
                      <ConversationItem
                        key={item.id}
                        item={item}
                        cwd={thread.cwd}
                        onFile={previewFile}
                      />
                    ))}
                    {turn.error && (
                      <div className="notice error-text">
                        {turn.error.message || JSON.stringify(turn.error)}
                      </div>
                    )}
                    {turn.status === "interrupted" && (
                      <p className="codex-turn-state">此轮已中断</p>
                    )}
                    {turn.status !== "inProgress" && (
                      <div className="codex-turn-actions">
                        <button
                          type="button"
                          disabled={busy || !ready}
                          onClick={() => fork(turn.id)}
                          title="保留截至此轮的上下文，在新聊天中继续"
                        >
                          <GitFork size={13} />
                          从此处分支
                        </button>
                      </div>
                    )}
                  </React.Fragment>
                ))}
                {!thread.turns?.length && (
                  <div className="codex-conversation-start">
                    <MessageSquare size={28} />
                    <h3>开始对话</h3>
                  </div>
                )}
                {active && (
                  <div className="codex-turn-state">
                    <LoaderCircle className="spinning" size={14} />
                    Codex 正在工作
                  </div>
                )}
                {pending.map((item) => (
                  <PendingRequest
                    key={`${status.generation}-${item.id}`}
                    item={item}
                    busy={busy || !ready}
                    onReply={(result) =>
                      run(() =>
                        api(`${base}/reply`, {
                          request_id: item.id,
                          generation: status.generation,
                          result,
                        }),
                      )
                    }
                  />
                ))}
              </div>
              <form className="codex-composer" onSubmit={send}>
                <CodexComposer
                  key={`${status.generation}:${thread.id}`}
                  endpoint={`${base}/threads/${idPath(thread.id)}`}
                  {...{
                    ready,
                    text,
                    setText,
                    selections,
                    setSelections,
                    active,
                  }}
                />
                <div className="codex-composer-actions">
                  <div className="codex-model-options">
                    <label
                      className="codex-option codex-model-option"
                      title="选择本次对话使用的模型"
                    >
                      <Cpu size={14} aria-hidden="true" />
                      <span className="codex-option-label">模型</span>
                      <select
                        aria-label="Codex 对话模型"
                        value={model}
                        disabled={!ready}
                        onChange={(event) => {
                          setModel(event.target.value);
                          setEffort("");
                        }}
                      >
                        <option value="">
                          {thread?.model
                            ? `${thread.model} · 会话`
                            : "沿用会话模型"}
                        </option>
                        {models.map((item) => (
                          <option key={item.id} value={item.model || item.id}>
                            {item.displayName || item.model || item.id}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label
                      className="codex-option codex-effort-option"
                      title="选择本次对话的推理强度"
                    >
                      <Brain size={14} aria-hidden="true" />
                      <span className="codex-option-label">推理</span>
                      <select
                        aria-label="Codex 推理强度"
                        value={effort}
                        disabled={!ready}
                        onChange={(event) => setEffort(event.target.value)}
                      >
                        <option value="">沿用会话</option>
                        {(efforts.length
                          ? efforts.map((item) => item.reasoningEffort)
                          : ["low", "medium", "high", "xhigh"]
                        ).map((value) => (
                          <option key={value} value={value}>
                            {effortLabel(value).split(" (")[0]}
                          </option>
                        ))}
                      </select>
                    </label>
                    <button
                      type="button"
                      className="codex-access-trigger codex-option"
                      aria-label="访问权限"
                      title={`访问权限：${accessLabel(thread)}`}
                      disabled={!ready || busy}
                      onClick={() => setAccessOpen(true)}
                    >
                      <ShieldCheck size={14} />
                      <span className="codex-option-label">权限</span>
                      <span>{accessLabel(thread)}</span>
                    </button>
                  </div>
                  <div className="row-actions">
                    {active && (
                      <button
                        type="button"
                        className="button"
                        disabled={busy || !ready}
                        onClick={() =>
                          run(async () => {
                            await api(
                              `${base}/threads/${idPath(thread.id)}/interrupt`,
                              { turn_id: active.id },
                            );
                            setRevision((value) => value + 1);
                          })
                        }
                      >
                        <Square size={13} />
                        中断
                      </button>
                    )}
                    <button
                      className="button primary codex-send"
                      disabled={busy || !ready || !streaming || !text.trim()}
                    >
                      <ArrowUp size={16} />
                      {active ? "补充说明" : "发送"}
                    </button>
                  </div>
                </div>
                <small className="codex-composer-hint">
                  <span>Enter 换行</span>
                  <span>
                    <kbd>Ctrl</kbd> / <kbd>⌘</kbd> + <kbd>Enter</kbd> 发送
                  </span>
                </small>
              </form>
            </>
          ) : (
            <div className="codex-welcome">
              <span className="codex-welcome-icon">
                <MessageSquare size={34} />
              </span>
              <h2>
                {loading
                  ? "正在打开任务…"
                  : ready
                    ? "选择任务，继续对话"
                    : "连接服务器的 Codex"}
              </h2>
              {ready ? (
                <button className="button primary" onClick={() => createIn()}>
                  <Plus size={16} />
                  新建任务
                </button>
              ) : (
                <button
                  className="button"
                  onClick={() =>
                    setSettings({
                      ...defaults,
                      ...status.config,
                      enabled: true,
                    })
                  }
                >
                  <Plug size={16} />
                  设置连接
                </button>
              )}
            </div>
          )}
        </div>
        {thread && (
          <Suspense fallback={null}>
            <WorkspacePanels
              key={`${status.generation}:${thread.id}`}
              {...{ base, thread, ready }}
              open={panelOpen}
              tab={panelTab}
              previewRequest={
                previewRequest?.threadId === thread.id ? previewRequest : null
              }
              onTab={setPanelTab}
              onClose={() => setPanelOpen(false)}
              onResize={resizePanel}
            />
          </Suspense>
        )}
      </div>

      {settings && (
        <Modal
          title={`连接 ${serverLabel(server)} 的 Codex`}
          onClose={() => setSettings(null)}
        >
          <form
            className="codex-dialog-form"
            onSubmit={(event) => {
              event.preventDefault();
              run(async () => {
                const result = await api(`${base}/settings`, settings);
                setStatus(result);
                setSettings(null);
                await onSaved();
              });
            }}
          >
            <p className="subtle">
              {server.config.kind === "ssh"
                ? `使用 ${server.config.username}@${server.config.host} 已保存的 SSH 凭据。`
                : "连接此服务器当前用户的 Codex。"}
              任务记录和模型登录由对应服务器的 Codex 保存。
            </p>
            <label className="checkbox-field">
              <input
                type="checkbox"
                checked={settings.enabled}
                onChange={(event) =>
                  setSettings({ ...settings, enabled: event.target.checked })
                }
              />
              启用网页连接与原任务反馈
            </label>
            <label>
              默认项目目录
              <input
                placeholder="/data/projects/my-experiment"
                value={settings.cwd || ""}
                onChange={(event) =>
                  setSettings({
                    ...settings,
                    cwd: event.target.value || null,
                  })
                }
              />
            </label>
            <label>
              Codex 可执行文件
              <input
                required
                value={settings.executable}
                onChange={(event) =>
                  setSettings({ ...settings, executable: event.target.value })
                }
              />
              <small>SSH 环境找不到 codex 时，填写其绝对路径。</small>
            </label>
            <label>
              服务器上的共享 Socket
              <input
                required
                value={settings.socket}
                onChange={(event) =>
                  setSettings({ ...settings, socket: event.target.value })
                }
              />
              <small>auto 使用该服务器当前用户的默认 Codex Socket。</small>
            </label>
            {server.config.return_agent_socket && (
              <div className="notice">
                此服务器已配置单独的归档转发 Socket，实验反馈仍优先使用该地址。
              </div>
            )}
            <div className="codex-connection-help">
              <ShieldCheck size={17} />
              <p>
                请先在服务器安装并登录 Codex。已有 app-server
                可以直接连接；首次使用可在保存后启动共享服务。
              </p>
            </div>
            <div className="form-actions">
              <button
                type="button"
                className="button"
                onClick={() => setSettings(null)}
              >
                取消
              </button>
              <button className="button primary" disabled={busy}>
                保存连接设置
              </button>
            </div>
          </form>
        </Modal>
      )}

      {!ready && status.config?.enabled && (
        <div className="codex-start-hint">
          <span>服务器尚未运行共享 Codex 服务？</span>
          <button
            className="text-button"
            disabled={busy || status.config.socket !== "auto"}
            onClick={() =>
              run(async () =>
                setStatus(await api(`${base}/connect`, { start: true })),
              )
            }
          >
            启动并连接
          </button>
          <span>需要服务器的 Codex 支持 app-server daemon / proxy。</span>
        </div>
      )}

      {newTask && (
        <Modal title="新建 Codex 任务" onClose={() => setNewTask(false)}>
          <form
            className="codex-dialog-form"
            onSubmit={(event) => {
              event.preventDefault();
              run(async () => {
                const result = await api(`${base}/threads`, {
                  cwd,
                  model: model || null,
                  project_id: taskProject || null,
                  ...newAccess,
                });
                openThread(result.thread.id);
                await list();
              });
            }}
          >
            {!!projects.length && (
              <label>
                所属项目
                <select
                  value={taskProject}
                  onChange={(event) => {
                    const id = event.target.value;
                    setTaskProject(id);
                    const project = projects.find((item) => item.id === id);
                    if (project?.roots?.[0]?.path)
                      setCwd(project.roots[0].path);
                  }}
                >
                  <option value="">独立任务</option>
                  {projects.map((project) => (
                    <option key={project.id} value={project.id}>
                      {project.name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label>
              项目目录
              <input
                autoFocus
                required
                placeholder="/data/projects/my-experiment"
                value={cwd}
                onChange={(event) => setCwd(event.target.value)}
              />
            </label>
            <label>
              模型
              <select
                aria-label="任务模型"
                value={model}
                onChange={(event) => setModel(event.target.value)}
              >
                <option value="">服务器默认模型</option>
                {models.map((item) => (
                  <option key={item.id} value={item.model || item.id}>
                    {item.displayName || item.model || item.id}
                  </option>
                ))}
              </select>
            </label>
            <details className="codex-new-access">
              <summary>访问权限</summary>
              <PermissionFields
                base={base}
                cwd={cwd}
                value={newAccess}
                onChange={setNewAccess}
                disabled={busy}
              />
            </details>
            {error && (
              <p className="notice error-text" role="alert">
                {error}
              </p>
            )}
            <div className="form-actions">
              <button
                type="button"
                className="button"
                onClick={() => setNewTask(false)}
              >
                取消
              </button>
              <button className="button primary" disabled={busy || !ready}>
                创建任务
              </button>
            </div>
          </form>
        </Modal>
      )}
      {accessOpen && thread && (
        <AccessDialog
          base={base}
          thread={thread}
          busy={busy || !ready}
          error={error}
          onClose={() => setAccessOpen(false)}
          onSave={(value) => {
            const taskId = thread.id;
            run(async () => {
              const result = await api(
                `${base}/threads/${idPath(taskId)}/access`,
                value,
              );
              if (alive.current && currentThread.current === taskId) {
                setThread((old) => ({
                  ...old,
                  ...result.thread,
                  turns: old.turns,
                  omittedTurns: old.omittedTurns,
                }));
                setAccessOpen(false);
              }
            });
          }}
        />
      )}
      {rename !== null && thread && (
        <Modal title="重命名聊天" onClose={() => setRename(null)}>
          <form
            className="codex-dialog-form"
            onSubmit={(event) => {
              event.preventDefault();
              const taskId = thread.id;
              run(async () => {
                const result = await api(
                  `${base}/threads/${idPath(taskId)}/name`,
                  { name: rename },
                );
                if (alive.current && currentThread.current === taskId) {
                  setThread((old) => ({ ...old, name: result.name }));
                  setRename(null);
                }
                setRevision((value) => value + 1);
              });
            }}
          >
            <label>
              聊天名称
              <input
                autoFocus
                required
                maxLength={200}
                value={rename}
                onChange={(event) => setRename(event.target.value)}
              />
            </label>
            {error && (
              <p className="notice error-text" role="alert">
                {error}
              </p>
            )}
            <div className="form-actions">
              <button
                type="button"
                className="button"
                onClick={() => setRename(null)}
              >
                取消
              </button>
              <button
                className="button primary"
                disabled={busy || !ready || !rename.trim()}
              >
                保存名称
              </button>
            </div>
          </form>
        </Modal>
      )}
    </section>
  );
}

function TaskMenu({ children }) {
  const [open, setOpen] = useState(false);
  const root = useRef(null);
  useEffect(() => {
    if (!open) return;
    const outside = (event) => {
      if (!root.current?.contains(event.target)) setOpen(false);
    };
    const escape = (event) => {
      if (event.key === "Escape") {
        setOpen(false);
        root.current?.querySelector("button").focus();
      }
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  return (
    <div
      className="codex-task-menu"
      ref={root}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
    >
      <IconButton
        icon={MoreHorizontal}
        label="更多聊天操作"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      />
      {open && (
        <div
          className="codex-task-menu-items"
          role="group"
          aria-label="聊天操作"
          onClick={() => setOpen(false)}
        >
          {children}
        </div>
      )}
    </div>
  );
}

const ConversationItem = memo(function ConversationItem({ item, cwd, onFile }) {
  const previewLink = (href, children) => {
    const target = conversationFile(href, cwd);
    return target ? (
      <button
        type="button"
        className="codex-file-link"
        title={`预览 ${target.path}`}
        onClick={() => onFile(target)}
      >
        {children}
      </button>
    ) : (
      <a
        href={defaultUrlTransform(href || "")}
        target="_blank"
        rel="noreferrer"
      >
        {children}
      </a>
    );
  };
  if (item.type === "userMessage")
    return (
      <article className="codex-message user">
        <span className="codex-role">你</span>
        <div>
          {(item.content || []).map((part, index) => (
            <p key={index}>
              {part.text ||
                (part.type === "image"
                  ? "[图片]"
                  : part.type === "skill"
                    ? `$${part.name}`
                    : `[${part.type}]`)}
            </p>
          ))}
        </div>
      </article>
    );
  if (item.type === "agentMessage")
    return (
      <article className="codex-message assistant">
        <span className="codex-role">
          <span className="codex-mini-mark">✳</span>Codex
        </span>
        <div className="markdown">
          <Markdown
            remarkPlugins={[remarkGfm]}
            skipHtml
            urlTransform={(url) =>
              conversationFile(url, cwd) ? url : defaultUrlTransform(url)
            }
            components={{
              a: ({ href, children }) => previewLink(href, children),
              img: ({ src, alt }) => {
                const target = conversationFile(src, cwd);
                return target ? (
                  <button
                    type="button"
                    className="codex-file-link"
                    onClick={() => onFile(target)}
                  >
                    <Files size={14} />
                    {alt || target.path}
                  </button>
                ) : (
                  <img src={defaultUrlTransform(src || "")} alt={alt || ""} />
                );
              },
            }}
          >
            {item.text || ""}
          </Markdown>
        </div>
      </article>
    );
  if (item.type === "reasoning")
    return (
      <details className="codex-tool">
        <summary>思考过程</summary>
        <p>{item.liveSummary || (item.summary || []).join("\n")}</p>
      </details>
    );
  const names = {
    commandExecution: "运行命令",
    fileChange: "文件变更",
    mcpToolCall: "工具调用",
    webSearch: "搜索",
    plan: "计划",
    contextCompaction: "整理上下文",
  };
  return (
    <details className="codex-tool">
      <summary>
        <Terminal size={14} />
        <span>{names[item.type] || item.type}</span>
        <code>{item.command || item.tool || ""}</code>
        <small>
          {item.status === "inProgress"
            ? "执行中"
            : item.status === "failed"
              ? "失败"
              : item.exitCode !== undefined && item.exitCode !== null
                ? `exit ${item.exitCode}`
                : ""}
        </small>
      </summary>
      {item.type === "fileChange" && item.changes?.length ? (
        <div className="codex-file-changes">
          {item.changes.map((change, index) => {
            const path = change.kind?.move_path || change.path;
            return (
              <section key={`${path}:${index}`}>
                {previewLink(
                  path,
                  <>
                    <Files size={14} />
                    <span>{path}</span>
                    <small>预览</small>
                  </>,
                )}
                {change.diff && <pre>{change.diff}</pre>}
              </section>
            );
          })}
        </div>
      ) : (
        <pre>
          {item.aggregatedOutput ||
            item.text ||
            (item.changes
              ? item.changes
                  .map((change) => `${change.path}\n${change.diff || ""}`)
                  .join("\n\n")
              : JSON.stringify(item.result || item, null, 2))}
        </pre>
      )}
    </details>
  );
});

function PendingRequest({ item, onReply, busy }) {
  const [answers, setAnswers] = useState({});
  const params = item.params || {};
  const input = item.method === "item/tool/requestUserInput";
  const permissions = item.method === "item/permissions/requestApproval";
  const elicitation = item.method === "mcpServer/elicitation/request";
  const decisions = params.availableDecisions || [
    "accept",
    "decline",
    "cancel",
  ];
  return (
    <section className="codex-request">
      <h3>
        <CircleAlert size={17} />
        {input ? "Codex 需要你的补充" : "Codex 请求确认"}
      </h3>
      <p>
        {params.reason ||
          params.message ||
          params.command ||
          (permissions ? "请求访问以下资源" : "请查看请求内容后选择是否允许。")}
      </p>
      {params.cwd && <small>目录：{params.cwd}</small>}
      {input ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onReply({
              answers: Object.fromEntries(
                params.questions.map((q) => [
                  q.id,
                  { answers: [answers[q.id]] },
                ]),
              ),
            });
          }}
        >
          {params.questions.map((question) => (
            <label key={question.id}>
              {question.question}
              <input
                aria-label={question.question}
                required
                list={`answers-${item.id}-${question.id}`}
                type={question.isSecret ? "password" : "text"}
                value={answers[question.id] || ""}
                onChange={(event) =>
                  setAnswers({ ...answers, [question.id]: event.target.value })
                }
              />
              <datalist id={`answers-${item.id}-${question.id}`}>
                {question.options?.map((option) => (
                  <option key={option.label} value={option.label}>
                    {option.description}
                  </option>
                ))}
              </datalist>
            </label>
          ))}
          <button className="button primary" disabled={busy}>
            提交回复
          </button>
        </form>
      ) : (
        <>
          <details>
            <summary>查看请求详情</summary>
            <pre>{JSON.stringify(params, null, 2)}</pre>
          </details>
          <div className="row-actions">
            {!elicitation && (permissions || decisions.includes("accept")) && (
              <button
                className="button primary"
                disabled={busy}
                onClick={() =>
                  onReply(
                    permissions
                      ? { permissions: params.permissions, scope: "turn" }
                      : { decision: "accept" },
                  )
                }
              >
                允许本次
              </button>
            )}
            {(permissions || elicitation || decisions.includes("decline")) && (
              <button
                className="button"
                disabled={busy}
                onClick={() =>
                  onReply(
                    permissions
                      ? { permissions: {}, scope: "turn" }
                      : elicitation
                        ? { action: "decline", content: null }
                        : { decision: "decline" },
                  )
                }
              >
                拒绝
              </button>
            )}
            {!permissions && !elicitation && decisions.includes("cancel") && (
              <button
                className="button"
                disabled={busy}
                onClick={() => onReply({ decision: "cancel" })}
              >
                取消此轮
              </button>
            )}
          </div>
        </>
      )}
    </section>
  );
}
