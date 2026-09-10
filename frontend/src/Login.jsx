import React, { useEffect, useState } from "react";
import {
  KeyRound,
  LoaderCircle,
  LockKeyhole,
  LogIn,
  Workflow,
} from "lucide-react";
import { api } from "./api.js";

export default function Login({ onLogin, onAuthChange }) {
  const [auth, setAuth] = useState(null);
  const [mode, setMode] = useState("token");
  const [token, setToken] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(() => {
    try {
      return localStorage.getItem("deepqueue.remember-login") !== "false";
    } catch {
      return true;
    }
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    api("/auth", undefined, { signal: controller.signal })
      .then((data) => {
        setAuth(data);
        setMode(data.password_enabled ? "password" : "token");
      })
      .catch((error) => {
        if (error.name !== "AbortError") {
          setError(error.message);
          setAuth({ password_enabled: false });
        }
      });
    return () => controller.abort();
  }, []);
  const secret = mode === "password" ? password : token;
  return (
    <main className="login-page">
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          onAuthChange(true);
          setBusy(true);
          setError("");
          try {
            await api("/auth/login", { [mode]: secret, remember });
            setToken("");
            setPassword("");
            await onLogin();
          } catch (error) {
            setError(error.message);
          } finally {
            setBusy(false);
            onAuthChange(false);
          }
        }}
      >
        <div className="login-brand">
          <Workflow size={28} />
          <h1>DeepQueue</h1>
        </div>
        {auth?.password_enabled && (
          <div
            className="segmented login-methods"
            role="group"
            aria-label="登录方式"
          >
            {[
              ["password", "密码登录"],
              ["token", "令牌登录"],
            ].map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={mode === value ? "active" : ""}
                aria-pressed={mode === value}
                disabled={busy}
                onClick={() => {
                  setMode(value);
                  setError("");
                }}
              >
                {label}
              </button>
            ))}
          </div>
        )}
        <label>
          <span className="login-label">
            {mode === "password" ? (
              <LockKeyhole size={16} />
            ) : (
              <KeyRound size={16} />
            )}
            {mode === "password" ? "管理员密码" : "管理员访问令牌"}
          </span>
          <input
            key={mode}
            type="password"
            name={mode}
            autoComplete={mode === "password" ? "current-password" : "off"}
            maxLength={mode === "password" ? 128 : 1024}
            required
            value={secret}
            disabled={busy || !auth}
            onChange={(event) =>
              mode === "password"
                ? setPassword(event.target.value)
                : setToken(event.target.value)
            }
          />
        </label>
        <label className="checkbox-label remember-login">
          <input
            type="checkbox"
            checked={remember}
            disabled={busy}
            onChange={(event) => {
              setRemember(event.target.checked);
              try {
                localStorage.setItem(
                  "deepqueue.remember-login",
                  String(event.target.checked),
                );
              } catch {
                /* Login can still use cookies without local storage. */
              }
            }}
          />
          在此浏览器记住登录 30 天
        </label>
        {error && (
          <p className="error-text" role="alert">
            {error}
          </p>
        )}
        <button className="button primary" disabled={busy || !auth || !secret}>
          {busy || !auth ? (
            <LoaderCircle size={16} className="spinning" />
          ) : (
            <LogIn size={16} />
          )}
          登录
        </button>
      </form>
    </main>
  );
}
