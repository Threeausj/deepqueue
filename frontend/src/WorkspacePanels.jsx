import React, { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Terminal as XTerm } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import {
  ArrowLeft,
  ExternalLink,
  File,
  Files,
  Folder,
  LoaderCircle,
  Monitor,
  RefreshCw,
  Square,
  Terminal,
  X,
} from "lucide-react";
import { api, idPath } from "./api.js";
import { IconButton } from "./components.jsx";
import "./workspaceTools.css";

const bytes = (value) =>
  Uint8Array.from(atob(value || ""), (c) => c.charCodeAt(0));

function InteractiveTerminal({ endpoint, base, threadId, visible, ready }) {
  const root = useRef(null);
  const terminal = useRef(null);
  const fit = useRef(null);
  const current = useRef(null);
  const sendQueue = useRef(Promise.resolve());
  const live = useRef(true);
  const syncing = useRef(false);
  const [state, setState] = useState("closed");
  const [recovery, setRecovery] = useState(false);
  const [recovering, setRecovering] = useState(false);
  const [executionMode, setExecutionMode] = useState("session");
  const [error, setError] = useState("");
  async function sync(start = false) {
    if (syncing.current || !ready) return;
    syncing.current = true;
    try {
      let row = await api(`${endpoint}/terminal`);
      if (start && ["closed", "exited", "error"].includes(row.state))
        row = await api(`${endpoint}/terminal`, {});
      if (!live.current) return;
      const old = current.current;
      const data = bytes(row.output_base64);
      if (
        old?.id !== row.id ||
        (row.offset || 0) - (old?.offset || 0) > data.length
      ) {
        terminal.current?.reset();
        terminal.current?.write(data);
      } else if (row.offset > old.offset)
        terminal.current?.write(
          data.slice(data.length - (row.offset - old.offset)),
        );
      current.current = row;
      setState(row.state);
      setRecovery(row.recovery_available || false);
      setExecutionMode(row.execution_mode || "session");
      setError(row.error || "");
    } catch (error) {
      if (live.current) setError(error.message);
    } finally {
      syncing.current = false;
    }
  }
  useEffect(() => {
    live.current = true;
    const term = new XTerm({
      cursorBlink: true,
      fontSize: 12,
      fontFamily: '"SFMono-Regular", Consolas, monospace',
      scrollback: 4000,
      theme: {
        background: "#15212f",
        foreground: "#dce7f2",
        cursor: "#b7c5ff",
        selectionBackground: "#435676",
      },
    });
    const addon = new FitAddon();
    term.loadAddon(addon);
    term.open(root.current);
    terminal.current = term;
    fit.current = addon;
    const input = term.onData((data) => {
      if (!current.current?.id || current.current.state !== "running") return;
      const id = current.current.id;
      for (let i = 0; i < data.length; i += 8000) {
        const chunk = data.slice(i, i + 8000);
        sendQueue.current = sendQueue.current
          .then(() => api(`${endpoint}/terminal/input`, { id, data: chunk }))
          .catch((error) => {
            if (live.current) setError(error.message);
          });
      }
    });
    let resizing;
    const size = term.onResize(({ cols, rows }) => {
      clearTimeout(resizing);
      resizing = setTimeout(() => {
        const row = current.current;
        if (row?.state === "running")
          api(`${endpoint}/terminal/resize`, {
            id: row.id,
            cols: Math.max(10, Math.min(400, cols)),
            rows: Math.max(3, Math.min(150, rows)),
          }).catch((error) => {
            if (live.current) setError(error.message);
          });
      }, 100);
    });
    const observer = new ResizeObserver(() => {
      if (root.current?.clientWidth && root.current?.clientHeight) addon.fit();
    });
    observer.observe(root.current);
    return () => {
      live.current = false;
      clearTimeout(resizing);
      observer.disconnect();
      input.dispose();
      size.dispose();
      term.dispose();
    };
  }, [endpoint]);
  useEffect(() => {
    if (!visible || !ready) return;
    sync(true);
    const poll = setInterval(() => sync(), 3000);
    requestAnimationFrame(() => {
      fit.current?.fit();
      terminal.current?.focus();
    });
    return () => clearInterval(poll);
  }, [visible, ready]);
  useEffect(() => {
    const event = ({ detail }) => {
      if (detail.base !== base) return;
      const { method, params: p = {} } = detail.event;
      if (method === "deepqueue/resync") {
        sync();
        return;
      }
      if (p.threadId !== threadId || p.id !== current.current?.id) return;
      if (method === "deepqueue/terminal") {
        current.current = { ...current.current, state: p.state };
        setState(p.state);
        setRecovery(p.recovery_available || false);
        setExecutionMode(p.execution_mode || "session");
        setError(p.error || "");
      }
      if (method === "deepqueue/terminal/output") {
        const data = bytes(p.data_base64),
          offset = current.current.offset || 0;
        if (p.offset <= offset) return;
        if (p.offset - offset > data.length) {
          sync();
          return;
        }
        terminal.current?.write(data.slice(data.length - (p.offset - offset)));
        current.current = { ...current.current, offset: p.offset };
      }
    };
    window.addEventListener("deepqueue:workspace-event", event);
    return () => window.removeEventListener("deepqueue:workspace-event", event);
  }, [base, threadId]);
  return (
    <div className="workspace-terminal" hidden={!visible}>
      <div className="workspace-subheading">
        <span>
          {
            {
              closed: "终端",
              starting: "正在启动终端…",
              running:
                executionMode === "server" ? "服务器账户终端" : "交互终端",
              exited: "终端已退出",
              error: "终端连接失败",
            }[state]
          }
        </span>
        <div className="row-actions">
          <IconButton
            icon={RefreshCw}
            label="重新打开终端"
            disabled={!ready}
            onClick={() => sync(true)}
          />
          <IconButton
            icon={Square}
            label="关闭当前终端"
            disabled={!ready || !["running", "starting"].includes(state)}
            onClick={async () => {
              try {
                await api(`${endpoint}/terminal/stop`, {
                  id: current.current.id,
                  data: "",
                });
                await sync();
              } catch (error) {
                setError(error.message);
              }
            }}
          />
        </div>
      </div>
      {error && (
        <p className="error-text workspace-tool-error" role="alert">
          {error}
        </p>
      )}
      {recovery && (
        <div className="workspace-terminal-recovery">
          <p>
            仅本次终端可改为使用服务器登录账户的完整文件与网络权限，对话 agent
            的权限不会改变。
          </p>
          <button
            type="button"
            className="button"
            disabled={!ready || recovering}
            onClick={async () => {
              setRecovering(true);
              try {
                await api(`${endpoint}/terminal/recover`, {
                  id: current.current.id,
                  allow_server_access: true,
                });
                await sync();
              } catch (error) {
                setError(error.message);
              } finally {
                if (live.current) setRecovering(false);
              }
            }}
          >
            {recovering ? "正在重新打开…" : "仅本次以服务器账户重新打开"}
          </button>
        </div>
      )}
      <div
        className="workspace-terminal-screen"
        ref={root}
        aria-label="服务器交互终端"
      />
    </div>
  );
}

function FilePreview({ file, line }) {
  const [url, setUrl] = useState("");
  const code = useRef(null);
  useEffect(() => {
    if (code.current && line) {
      const height =
        parseFloat(getComputedStyle(code.current).lineHeight) || 20;
      code.current.scrollTop = Math.max(0, line - 3) * height;
    }
  }, [file, line]);
  useEffect(() => {
    if (!file) return;
    const url = URL.createObjectURL(
      new Blob([bytes(file.data_base64)], { type: file.mime }),
    );
    setUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);
  if (!file)
    return (
      <div className="workspace-tool-empty">
        <Files size={26} />
        <p>在文件面板选择要预览的文件</p>
      </div>
    );
  const name = file.name.toLowerCase();
  return (
    <div className="workspace-file-preview">
      <div className="workspace-file-name" title={file.path}>
        {file.name}
        <small>
          {line ? `第 ${line} 行 · ` : ""}
          {(file.size / 1024).toFixed(1)} KiB
        </small>
      </div>
      {file.mime.startsWith("image/") ? (
        <div className="workspace-image">
          <img src={url} alt={file.name} />
        </div>
      ) : /\.md(?:own)?$|\.markdown$/.test(name) ? (
        <article className="workspace-markdown">
          <Markdown remarkPlugins={[remarkGfm]}>{file.text || ""}</Markdown>
        </article>
      ) : file.mime === "application/pdf" ? (
        <iframe
          title={`预览 ${file.name}`}
          src={url}
          sandbox="allow-scripts allow-same-origin"
        />
      ) : /\.html?$/.test(name) ? (
        <iframe
          title={`预览 ${file.name}`}
          srcDoc={file.text || ""}
          sandbox="allow-scripts"
        />
      ) : file.text !== null ? (
        <pre className="workspace-code" ref={code}>
          <code>{file.text}</code>
        </pre>
      ) : (
        <div className="workspace-tool-empty">
          暂不支持此二进制文件的在线预览
        </div>
      )}
    </div>
  );
}

function WebPreview({ endpoint, ready, visible }) {
  const [port, setPort] = useState("3000");
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const ref = useRef(null);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      if (ref.current)
        api(`${endpoint}/preview/close`, { id: ref.current.id }).catch(
          () => {},
        );
    };
  }, [endpoint]);
  useEffect(() => {
    if (!preview || !visible || !ready) return;
    const timer = setInterval(
      () =>
        api(`${endpoint}/preview/renew`, { id: preview.id }).catch((error) =>
          setError(error.message),
        ),
      5 * 60_000,
    );
    return () => clearInterval(timer);
  }, [preview, visible, ready]);
  async function close() {
    if (ref.current)
      await api(`${endpoint}/preview/close`, { id: ref.current.id });
    ref.current = null;
    setPreview(null);
  }
  return (
    <div className="workspace-web-preview" hidden={!visible}>
      <form
        className="workspace-port-form"
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError("");
          try {
            await close();
            const result = await api(`${endpoint}/preview`, {
              port: Number(port),
            });
            if (!live.current) {
              api(`${endpoint}/preview/close`, { id: result.id }).catch(
                () => {},
              );
              return;
            }
            ref.current = result;
            setPreview(result);
          } catch (error) {
            if (live.current) setError(error.message);
          } finally {
            if (live.current) setBusy(false);
          }
        }}
      >
        <label>
          服务器端口
          <input
            type="number"
            min="1"
            max="65535"
            required
            aria-label="网页服务端口"
            value={port}
            onChange={(event) => setPort(event.target.value)}
          />
        </label>
        <button className="button" disabled={busy || !ready}>
          {busy ? (
            <LoaderCircle size={14} className="spinning" />
          ) : (
            <Monitor size={14} />
          )}
          打开
        </button>
        {preview && (
          <>
            <IconButton
              icon={RefreshCw}
              label="刷新网页预览"
              onClick={() => setRefresh((n) => n + 1)}
            />
            <a
              className="icon-button"
              aria-label="在新窗口打开预览"
              href={preview.url}
              target="_blank"
              rel="noreferrer"
            >
              <ExternalLink size={15} />
            </a>
            <IconButton
              icon={X}
              label="关闭网页预览"
              onClick={() => close().catch((error) => setError(error.message))}
            />
          </>
        )}
      </form>
      {error && (
        <p className="error-text workspace-tool-error" role="alert">
          {error}
        </p>
      )}
      {preview ? (
        <iframe
          key={`${preview.id}:${refresh}`}
          title="网页服务预览"
          src={preview.url}
          referrerPolicy="no-referrer"
          sandbox="allow-scripts allow-same-origin allow-forms allow-downloads allow-popups"
        />
      ) : (
        <div className="workspace-tool-empty">
          <Monitor size={28} />
          <p>输入当前服务器上运行的网页服务端口</p>
          <small>例如 Vite 5173、Gradio 7860</small>
        </div>
      )}
    </div>
  );
}

export default function WorkspacePanels({
  base,
  thread,
  ready,
  open,
  tab,
  previewRequest,
  onTab,
  onClose,
  onResize,
}) {
  const endpoint = `${base}/threads/${idPath(thread.id)}`;
  const [folder, setFolder] = useState(null);
  const [path, setPath] = useState("");
  const [file, setFile] = useState(null);
  const [fileLine, setFileLine] = useState(null);
  const [mode, setMode] = useState("web");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const flight = useRef(null);
  useEffect(() => () => flight.current?.abort(), []);
  async function read(path, directory = true, line = null) {
    flight.current?.abort();
    const controller = new AbortController();
    flight.current = controller;
    setLoading(true);
    setError("");
    if (!directory) {
      setFile(null);
      setFileLine(line);
      setMode("file");
      onTab("preview");
    }
    try {
      const result = await api(
        `${endpoint}/${directory ? "files" : "file"}?${new URLSearchParams({ path })}`,
        undefined,
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      if (directory) {
        setFolder(result);
        setPath(path);
      } else {
        setFile(result);
        setMode("file");
        onTab("preview");
      }
    } catch (error) {
      if (error.name !== "AbortError") setError(error.message);
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }
  useEffect(() => {
    if (open && tab === "files" && ready && !folder) read("");
  }, [open, tab, ready]);
  useEffect(() => {
    if (!previewRequest || !ready) return;
    setMode("file");
    if (previewRequest.error) {
      flight.current?.abort();
      setLoading(false);
      setFile(null);
      setError(previewRequest.error);
    } else read(previewRequest.path, false, previewRequest.line);
  }, [previewRequest, ready]);
  return (
    <aside
      className="workspace-tools"
      aria-label="工作区工具面板"
      hidden={!open}
    >
      <div
        className="workspace-tools-resizer"
        role="separator"
        aria-label="调整工具面板宽度"
        aria-orientation="vertical"
        onPointerDown={onResize}
      />
      <header className="workspace-tools-heading">
        <div role="tablist" aria-label="工作区工具">
          {[
            ["terminal", "终端", Terminal],
            ["files", "文件", Files],
            ["preview", "预览", Monitor],
          ].map(([id, label, Icon]) => (
            <button
              type="button"
              role="tab"
              aria-selected={tab === id}
              className={tab === id ? "active" : ""}
              key={id}
              onClick={() => onTab(id)}
            >
              <Icon size={14} />
              {label}
            </button>
          ))}
        </div>
        <IconButton icon={X} label="收起右侧面板" onClick={onClose} />
      </header>
      <div className="workspace-root-path" title={thread.cwd}>
        {thread.cwd}
      </div>
      {loading && (
        <p className="workspace-tool-error">
          <LoaderCircle className="spinning" size={14} />
          正在读取…
        </p>
      )}
      {error && (
        <p className="error-text workspace-tool-error" role="alert">
          {error}
        </p>
      )}
      <InteractiveTerminal
        {...{ endpoint, base, ready }}
        threadId={thread.id}
        visible={open && tab === "terminal"}
      />
      <div className="workspace-files" hidden={tab !== "files"}>
        <div className="workspace-subheading">
          <IconButton
            icon={ArrowLeft}
            label="上一级目录"
            disabled={!path || loading}
            onClick={() => read(path.split("/").slice(0, -1).join("/"))}
          />
          <span title={path}>{path || "项目目录"}</span>
          <IconButton
            icon={RefreshCw}
            label="刷新项目文件"
            disabled={!ready || loading}
            onClick={() => read(path)}
          />
        </div>
        <ul aria-label="项目文件列表">
          {folder?.entries.map((entry) => (
            <li key={entry.path}>
              <button
                type="button"
                disabled={!ready || loading}
                onClick={() => read(entry.path, entry.directory)}
                title={entry.name}
              >
                {entry.directory ? <Folder size={16} /> : <File size={16} />}
                <span>{entry.name}</span>
                {entry.symlink && <small>链接</small>}
              </button>
            </li>
          ))}
        </ul>
        {folder && !folder.entries.length && (
          <div className="workspace-tool-empty">此目录为空</div>
        )}
        {folder?.truncated && (
          <p className="subtle workspace-tool-error">
            目录较大，已显示前 2000 项；其余文件可在终端查看。
          </p>
        )}
      </div>
      <div className="workspace-preview" hidden={tab !== "preview"}>
        <div className="workspace-preview-modes segmented">
          <button
            type="button"
            className={mode === "file" ? "active" : ""}
            onClick={() => setMode("file")}
          >
            文件预览
          </button>
          <button
            type="button"
            className={mode === "web" ? "active" : ""}
            onClick={() => setMode("web")}
          >
            网页服务
          </button>
        </div>
        {mode === "file" && <FilePreview file={file} line={fileLine} />}
        <WebPreview
          {...{ endpoint, ready }}
          visible={mode === "web" && open && tab === "preview"}
        />
      </div>
    </aside>
  );
}
