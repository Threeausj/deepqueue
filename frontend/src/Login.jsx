import React, { useState } from "react";
import { KeyRound, LoaderCircle, LogIn, Workflow } from "lucide-react";
import { api } from "./api.js";

export default function Login({ onLogin, onAuthChange }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <main className="login-page">
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          onAuthChange(true);
          setBusy(true);
          setError("");
          try {
            await api("/auth/login", { token });
            setToken("");
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
        <label>
          <span className="login-label">
            <KeyRound size={16} />
            管理员访问令牌
          </span>
          <input
            type="password"
            autoComplete="current-password"
            required
            value={token}
            onChange={(event) => setToken(event.target.value)}
          />
        </label>
        {error && (
          <p className="error-text" role="alert">
            {error}
          </p>
        )}
        <button className="button primary" disabled={busy || !token}>
          {busy ? (
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
