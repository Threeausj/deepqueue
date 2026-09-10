import React, { useEffect, useState } from "react";
import { Check, LoaderCircle, LockKeyhole, Save } from "lucide-react";
import { api } from "./api.js";

export default function PasswordSettings({
  authEnabled,
  onSaved,
  onAuthChange,
}) {
  const [saved, setSaved] = useState(null);
  const [enabled, setEnabled] = useState(false);
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [current, setCurrent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    api("/auth/password", undefined, { signal: controller.signal })
      .then((data) => {
        setSaved(data);
        setEnabled(data.enabled);
      })
      .catch((error) => {
        if (error.name !== "AbortError") setError(error.message);
      });
    return () => controller.abort();
  }, [authEnabled]);
  const dirty = saved && (enabled !== saved.enabled || (enabled && !!password));
  return (
    <section
      className="deployment-settings password-settings"
      aria-label="管理员密码设置"
    >
      <div className="password-settings-heading">
        <h2>
          <LockKeyhole size={17} />
          密码登录
        </h2>
        {saved && (
          <span className={`status ${saved.enabled ? "green" : "neutral"}`}>
            <i />
            {saved.enabled ? "已启用" : "未启用"}
          </span>
        )}
      </div>
      <p className="password-settings-description">
        可选的管理员登录方式。启用后，登录页可使用密码或管理员令牌，并可记住此浏览器
        30 天。
      </p>
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
      {saved && (
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            setError("");
            setSuccess(false);
            if (enabled && password !== confirmation) {
              setError("两次输入的密码不一致");
              return;
            }
            setBusy(true);
            onAuthChange(true);
            try {
              const data = await api("/auth/password", {
                enabled,
                ...(enabled ? { password } : {}),
                ...(saved.requires_current_password
                  ? { current_password: current }
                  : {}),
              });
              setSaved(data);
              setEnabled(data.enabled);
              setPassword("");
              setConfirmation("");
              setCurrent("");
              setSuccess(true);
              await onSaved();
            } catch (error) {
              setError(error.message);
            } finally {
              setBusy(false);
              onAuthChange(false);
            }
          }}
        >
          <label className="checkbox-label password-enabled">
            <input
              type="checkbox"
              checked={enabled}
              disabled={busy || !saved.can_configure}
              onChange={(event) => {
                setEnabled(event.target.checked);
                setSuccess(false);
                setError("");
              }}
            />
            启用密码登录
          </label>
          {!saved.can_configure && (
            <p className="subtle password-settings-description">
              先在“公网接入”保存地址并启用管理员认证，再设置登录密码。
            </p>
          )}
          {saved.requires_current_password && (
            <label className="current-password-field">
              当前密码
              <input
                type="password"
                autoComplete="current-password"
                required
                maxLength={128}
                value={current}
                disabled={busy}
                onChange={(event) => {
                  setCurrent(event.target.value);
                  setSuccess(false);
                }}
              />
            </label>
          )}
          {enabled && (
            <div className="form-grid password-fields">
              <label>
                新管理员密码
                <input
                  type="password"
                  autoComplete="new-password"
                  minLength={12}
                  maxLength={128}
                  required
                  placeholder="12–128 个字符"
                  value={password}
                  disabled={busy}
                  onChange={(event) => {
                    setPassword(event.target.value);
                    setSuccess(false);
                  }}
                />
              </label>
              <label>
                确认管理员密码
                <input
                  type="password"
                  autoComplete="new-password"
                  minLength={12}
                  maxLength={128}
                  required
                  value={confirmation}
                  disabled={busy}
                  onChange={(event) => {
                    setConfirmation(event.target.value);
                    setSuccess(false);
                  }}
                />
              </label>
            </div>
          )}
          {saved.enabled && (
            <p className="subtle password-settings-description">
              修改或关闭密码会使其他密码登录失效；管理员令牌仍可登录。关闭后，请使用管理员令牌重新登录。
            </p>
          )}
          <div className="settings-actions">
            <button
              className="button"
              disabled={
                busy ||
                !saved.can_configure ||
                !dirty ||
                (enabled && (!password || !confirmation))
              }
            >
              {busy ? (
                <LoaderCircle size={15} className="spinning" />
              ) : (
                <Save size={15} />
              )}
              保存密码设置
            </button>
            {success && (
              <span className="settings-saved" role="status">
                <Check size={15} />
                密码设置已保存
              </span>
            )}
          </div>
        </form>
      )}
    </section>
  );
}
