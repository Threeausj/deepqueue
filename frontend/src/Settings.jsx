import React, { useEffect, useState } from "react";
import {
  Check,
  LoaderCircle,
  LogOut,
  Pause,
  Play,
  RotateCcw,
  Save,
} from "lucide-react";
import { api } from "./api.js";
import { AgentModels } from "./AgentModels.jsx";
import { Deployment } from "./Deployment.jsx";
import { Stamp } from "./components.jsx";

export default function Settings({
  onSaved,
  onAuthChange,
  data,
  connected,
  schedulerBusy,
  onSchedulerToggle,
  onLogout,
}) {
  const [form, setForm] = useState(null);
  const [saved, setSaved] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);
  const running = data?.daemon?.running;

  useEffect(() => {
    const controller = new AbortController();
    api("/settings", undefined, { signal: controller.signal })
      .then((settings) => {
        setForm(settings);
        setSaved(settings);
      })
      .catch((error) => {
        if (error.name !== "AbortError") setError(error.message);
      });
    return () => controller.abort();
  }, []);

  const dirty = form && JSON.stringify(form) !== JSON.stringify(saved);
  function change(field, value) {
    setForm((current) => ({ ...current, [field]: value }));
    setSuccess(false);
  }

  return (
    <section className="settings-page">
      <section
        className="runtime-settings"
        aria-labelledby="runtime-settings-title"
      >
        <div className="runtime-heading">
          <h2 id="runtime-settings-title">系统运行</h2>
          {data?.auth_enabled && (
            <button className="button" type="button" onClick={onLogout}>
              <LogOut size={15} />
              退出登录
            </button>
          )}
        </div>
        <div className="runtime-controls">
          <div className="runtime-item">
            <span className="subtle">服务连接</span>
            <span
              className={`connection ${connected ? "" : "offline"}`}
              role="status"
            >
              <i />
              {connected ? "实时连接" : "连接中断"}
            </span>
          </div>
          <div className="runtime-item">
            <span className="subtle">调度器</span>
            <span className="runtime-state" role="status">
              <span className={`dot ${running ? "green" : "gray"}`} />
              {running ? "运行中" : "已停止"}
            </span>
          </div>
          <div className="runtime-item">
            <span className="subtle">最近心跳</span>
            <Stamp value={data?.daemon?.heartbeat?.at} />
          </div>
          <button
            className={`button ${running ? "" : "primary"}`}
            type="button"
            disabled={schedulerBusy || !data || !connected}
            onClick={onSchedulerToggle}
          >
            {schedulerBusy ? (
              <LoaderCircle size={15} className="spinning" />
            ) : running ? (
              <Pause size={15} />
            ) : (
              <Play size={15} />
            )}
            {running ? "停止调度" : "启动调度"}
          </button>
        </div>
      </section>
      {error && (
        <div className="notice error-text" role="alert">
          {error}
        </div>
      )}
      {!form ? (
        !error && (
          <LoaderCircle
            className="spinning"
            aria-label="正在读取设置"
            size={20}
          />
        )
      ) : (
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            setBusy(true);
            setError("");
            setSuccess(false);
            try {
              const updated = await api("/settings", {
                agent_models: form.agent_models,
                agent_efforts: form.agent_efforts,
                launch_max_retries: form.launch_max_retries,
              });
              setForm(updated);
              setSaved(updated);
              setSuccess(true);
              await onSaved();
            } catch (error) {
              setError(error.message);
            } finally {
              setBusy(false);
            }
          }}
        >
          <AgentModels
            value={form.agent_models}
            efforts={form.agent_efforts}
            data={form}
            onChange={(value) => change("agent_models", value)}
            onEffortsChange={(value) => change("agent_efforts", value)}
            hasSource
            globalDefaults
            disabled={busy}
          />
          <section className="recovery-settings">
            <h2>启动自动修复</h2>
            <label>
              自动修复次数上限
              <input
                type="number"
                min="0"
                max="10"
                step="1"
                required
                disabled={busy}
                value={form.launch_max_retries ?? 3}
                onChange={(event) =>
                  change(
                    "launch_max_retries",
                    event.target.value === "" ? "" : Number(event.target.value),
                  )
                }
              />
            </label>
          </section>
          <div className="settings-actions">
            <button
              className="button primary"
              type="submit"
              disabled={busy || !dirty}
            >
              {busy ? (
                <LoaderCircle size={15} className="spinning" />
              ) : (
                <Save size={15} />
              )}
              保存设置
            </button>
            <button
              className="button"
              type="button"
              disabled={busy || !dirty}
              onClick={() => {
                setForm(saved);
                setSuccess(false);
                setError("");
              }}
            >
              <RotateCcw size={15} />
              撤销修改
            </button>
            {success && (
              <span className="settings-saved" role="status">
                <Check size={15} />
                已保存
              </span>
            )}
            {dirty && <span className="subtle">尚未保存</span>}
          </div>
        </form>
      )}
      <Deployment onSaved={onSaved} onAuthChange={onAuthChange} />
    </section>
  );
}
