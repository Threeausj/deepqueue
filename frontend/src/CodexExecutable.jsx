import React, { useEffect, useId, useRef, useState } from "react";
import { LoaderCircle, Search } from "lucide-react";
import { api } from "./api.js";

const kindLabel = {
  npm: "npm 启动入口",
  "npm-native": "npm 内置程序",
  binary: "独立可执行文件",
};

export default function CodexExecutable({ base, value, onChange, disabled }) {
  const id = useId();
  const flight = useRef(null);
  const [detecting, setDetecting] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => () => flight.current?.abort(), []);
  const selected = result?.candidates.find((item) => item.path === value);
  async function detect() {
    flight.current?.abort();
    const controller = new AbortController();
    flight.current = controller;
    setDetecting(true);
    setResult(null);
    setError("");
    try {
      const result = await api(
        `${base}/executables?${new URLSearchParams({ executable: value.trim() || "codex" })}`,
        undefined,
        { signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      setResult(result);
      if (result.recommended) onChange(result.recommended);
    } catch (error) {
      if (!controller.signal.aborted) setError(error.message);
    } finally {
      if (!controller.signal.aborted) setDetecting(false);
    }
  }
  return (
    <div className="codex-executable">
      <label htmlFor={id}>Codex 可执行文件</label>
      <div className="codex-executable-input">
        <input
          id={id}
          required
          disabled={disabled}
          value={value}
          placeholder="codex 或服务器上的绝对路径"
          onChange={(event) => {
            flight.current?.abort();
            setDetecting(false);
            setResult(null);
            setError("");
            onChange(event.target.value);
          }}
        />
        <button
          type="button"
          className="button"
          disabled={disabled || detecting}
          onClick={detect}
        >
          {detecting ? (
            <LoaderCircle size={14} className="spinning" />
          ) : (
            <Search size={14} />
          )}
          {detecting ? "正在查找…" : "自动查找"}
        </button>
      </div>
      <small>支持 PATH、npm 全局安装和 nvm；在当前服务器账户下查找。</small>
      {error && (
        <p className="error-text" role="alert">
          {error}
        </p>
      )}
      {result && (
        <div className="codex-executable-result" aria-live="polite">
          {result.candidates.length ? (
            <>
              <label>
                检测到的 Codex 安装
                <select
                  value={selected ? value : ""}
                  disabled={disabled}
                  onChange={(event) => onChange(event.target.value)}
                >
                  <option value="" disabled>
                    选择一个可用路径
                  </option>
                  {result.candidates.map((item) => (
                    <option
                      key={item.path}
                      value={item.path}
                      disabled={!item.ready}
                    >
                      {kindLabel[item.kind] || item.kind}
                      {item.version ? ` · ${item.version}` : ""} — {item.path}
                      {item.ready ? "" : "（缺少 Node）"}
                    </option>
                  ))}
                </select>
              </label>
              {selected && <code>{selected.path}</code>}
              {selected?.node && (
                <small>
                  Node：<code>{selected.node}</code>
                </small>
              )}
              {!result.recommended && (
                <p className="error-text">
                  {result.candidates[0].problem ||
                    "找到安装，但没有可直接使用的入口。"}
                </p>
              )}
            </>
          ) : (
            <p>
              未找到 Codex。请确认此服务器账户已安装
              @openai/codex，或手动填写绝对路径。
            </p>
          )}
          {!result.candidates.length &&
            result.warnings?.map((message) => (
              <small key={message}>{message}</small>
            ))}
        </div>
      )}
    </div>
  );
}
