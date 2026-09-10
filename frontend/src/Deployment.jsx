import React, { useEffect, useState } from "react";
import {
  Check,
  Copy,
  Download,
  KeyRound,
  LoaderCircle,
  Save,
  Trash2,
} from "lucide-react";
import { api, idPath, serverLabel } from "./api.js";
import { IconButton, Modal } from "./components.jsx";

function Credential({ token }) {
  const [copied, setCopied] = useState(false);
  return (
    <label className="credential-field">
      访问令牌（仅显示一次）
      <div className="inline-field">
        <input readOnly type="password" value={token} aria-label="新访问令牌" />
        <IconButton
          icon={copied ? Check : Copy}
          label="复制访问令牌"
          onClick={async () => {
            await navigator.clipboard.writeText(token);
            setCopied(true);
          }}
        />
      </div>
    </label>
  );
}

export function Deployment({ onSaved, onAuthChange }) {
  const [saved, setSaved] = useState(null);
  const [url, setUrl] = useState("");
  const [previewOrigin, setPreviewOrigin] = useState("");
  const [credential, setCredential] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    api("/deployment", undefined, { signal: controller.signal })
      .then((data) => {
        setSaved(data);
        setUrl(data.public_url || "");
        setPreviewOrigin(data.preview_origin || "");
      })
      .catch((error) => {
        if (error.name !== "AbortError") setError(error.message);
      });
    return () => controller.abort();
  }, []);
  return (
    <section className="deployment-settings">
      <h2>公网接入</h2>
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
      {saved && (
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            onAuthChange(true);
            setBusy(true);
            setError("");
            try {
              const data = await api("/deployment", {
                public_url: url.trim() || null,
                preview_origin: previewOrigin.trim() || null,
              });
              setSaved(data);
              setUrl(data.public_url || "");
              setPreviewOrigin(data.preview_origin || "");
              if (data.credential) setCredential(data.credential);
              await onSaved();
            } catch (error) {
              setError(error.message);
            } finally {
              setBusy(false);
              onAuthChange(false);
            }
          }}
        >
          <label>
            公网访问地址
            <input
              type="url"
              placeholder="https://queue.example.com"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              disabled={busy}
            />
          </label>
          <details className="preview-origin-settings">
            <summary>网页服务预览</summary>
            <label>
              独立预览域名
              <input
                type="url"
                placeholder="https://preview.example.com"
                value={previewOrigin}
                onChange={(event) => setPreviewOrigin(event.target.value)}
                disabled={busy}
              />
            </label>
            <p className="subtle">
              通过域名或内网 IP 访问时填写。请将 *.preview.example.com 的
              DNS、HTTPS 和 WebSocket 转发到 DeepQueue 同一端口。本机 localhost
              访问可留空。
            </p>
          </details>
          <div className="settings-actions">
            <button
              className="button"
              disabled={
                busy ||
                (url === (saved.public_url || "") &&
                  previewOrigin === (saved.preview_origin || ""))
              }
            >
              {busy ? (
                <LoaderCircle size={15} className="spinning" />
              ) : (
                <Save size={15} />
              )}
              保存接入设置
            </button>
            <span className="subtle">
              {saved.auth_enabled ? "访问认证已启用" : "仅本机访问"}
            </span>
          </div>
          {credential && <Credential token={credential.token} />}
        </form>
      )}
    </section>
  );
}

export function ServerAccess({ server, publicUrl, onClose, onSaved }) {
  const [tokens, setTokens] = useState([]);
  const [credential, setCredential] = useState(null);
  const [socket, setSocket] = useState(server.config.return_agent_socket || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const path = `/servers/${idPath(server.name)}`;
  const command = publicUrl
    ? `deepqueue --url ${publicUrl} --target-server ${server.name} client configure`
    : "";
  const refresh = () =>
    api("/access").then((rows) =>
      setTokens(rows.filter((row) => row.server === server.name)),
    );
  useEffect(() => {
    refresh().catch((error) => setError(error.message));
  }, [server.name]);
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
  return (
    <Modal title={`${serverLabel(server)} 接入配置`} onClose={onClose}>
      <div className="server-access">
        {error && (
          <p className="error-text" role="alert">
            {error}
          </p>
        )}
        <dl className="access-target">
          <dt>公网队列</dt>
          <dd>{publicUrl || "尚未设置"}</dd>
          <dt>绑定执行服务器</dt>
          <dd>{server.name}</dd>
        </dl>
        {command && (
          <label>
            客户端接入命令
            <div className="inline-field">
              <code>{command}</code>
              <IconButton
                icon={Copy}
                label="复制接入命令"
                onClick={() => navigator.clipboard.writeText(command)}
              />
            </div>
          </label>
        )}
        <div className="settings-actions">
          <button
            className="button"
            disabled={busy || !publicUrl}
            onClick={() =>
              perform(async () => {
                setCredential(
                  await api(`${path}/access`, {
                    label: `${server.name} skill`,
                  }),
                );
                await refresh();
              })
            }
          >
            <KeyRound size={15} />
            创建提交令牌
          </button>
          {publicUrl && (
            <a className="button" href={`/api${path}/skill`} download>
              <Download size={15} />
              下载服务器 Skill
            </a>
          )}
        </div>
        {credential && <Credential token={credential.token} />}
        <ul className="access-tokens">
          {tokens.map((token) => (
            <li key={token.id}>
              <span>
                {token.label}
                <small>{token.id}</small>
              </span>
              <IconButton
                icon={Trash2}
                label={`撤销令牌 ${token.id}`}
                disabled={busy}
                onClick={() =>
                  perform(async () => {
                    await api(`/access/${idPath(token.id)}/revoke`, {});
                    if (credential?.id === token.id) setCredential(null);
                    await refresh();
                  })
                }
              />
            </li>
          ))}
        </ul>
        <form
          className="callback-settings"
          onSubmit={(event) => {
            event.preventDefault();
            perform(async () => {
              await api(`${path}/callback`, {
                return_agent_socket: socket.trim() || null,
              });
              await onSaved();
            });
          }}
        >
          <label>
            来源任务回传 Socket
            <input
              value={socket}
              onChange={(event) => setSocket(event.target.value)}
              placeholder={
                server.config.codex?.enabled
                  ? "使用此服务器的 Codex 连接"
                  : "使用全局连接"
              }
              disabled={busy}
            />
          </label>
          <button className="button" disabled={busy}>
            <Save size={15} />
            保存回传连接
          </button>
        </form>
      </div>
    </Modal>
  );
}
