import React, { useEffect, useState } from "react";
import { api } from "./api.js";

export const agentPhases = [
  ["estimate", "资源预估"],
  ["launch", "启动适配"],
  ["archive", "归档与改进"],
];

export const modelLabel = (model) =>
  model === "source-thread" ? "使用会话当前模型" : model;

export const effortLabel = (effort) =>
  ({
    "source-thread": "使用会话当前强度",
    none: "关闭 (none)",
    minimal: "最小 (minimal)",
    low: "低 (low)",
    medium: "中 (medium)",
    high: "高 (high)",
    xhigh: "超高 (xhigh)",
    max: "最大 (max)",
    ultra: "极高 (ultra)",
  })[effort] || effort;

export function AgentModels({
  value,
  onChange,
  data,
  hasSource,
  inheritManifest,
  efforts = {},
  onEffortsChange,
  globalDefaults = false,
  disabled = false,
}) {
  const [catalog, setCatalog] = useState(null);
  const [custom, setCustom] = useState({});
  useEffect(() => {
    const controller = new AbortController();
    api("/models", undefined, { signal: controller.signal })
      .then(setCatalog)
      .catch((error) => {
        if (error.name !== "AbortError") setCatalog({ errors: { all: true } });
      });
    return () => controller.abort();
  }, []);

  function choose(phase, model) {
    const next = { ...value };
    if (model) next[phase] = model;
    else delete next[phase];
    onChange(next);
  }

  function chooseEffort(phase, effort) {
    const next = { ...efforts };
    if (effort) next[phase] = effort;
    else delete next[phase];
    onEffortsChange(next);
  }

  return (
    <fieldset className="agent-model-fields" disabled={disabled}>
      <legend>Agent 模型{onEffortsChange && "与推理强度"}</legend>
      <div className="agent-model-grid">
        {agentPhases.map(([phase, label], index) => {
          let fallback = globalDefaults ? null : data.agent_models?.[phase];
          if (
            phase === "archive" &&
            (!fallback || fallback === "source-thread")
          )
            fallback = hasSource ? "source-thread" : data.model;
          fallback ||= data.model;
          const catalogPhase =
            phase === "archive" && !hasSource && !inheritManifest
              ? "estimate"
              : phase;
          const options = new Map(
            (catalog?.models?.[catalogPhase] || [])
              .filter((item) => !item.hidden)
              .map((item) => [item.model, item.model]),
          );
          if (phase === "archive")
            options.set("source-thread", modelLabel("source-thread"));
          if (fallback) options.set(fallback, modelLabel(fallback));
          if (value[phase]) options.set(value[phase], modelLabel(value[phase]));
          const selectedModel = value[phase] || fallback;
          const available = catalog?.models?.[catalogPhase] || [];
          const selectedInfo = available.find(
            (item) => item.model === selectedModel,
          );
          let fallbackEffort = globalDefaults
            ? null
            : data.agent_efforts?.[phase];
          if (
            phase === "archive" &&
            (!fallbackEffort || fallbackEffort === "source-thread")
          )
            fallbackEffort = hasSource ? "source-thread" : data.agent_effort;
          fallbackEffort ||= data.agent_effort || "medium";
          const availableEfforts = selectedInfo?.efforts?.length
            ? selectedInfo.efforts
            : [...new Set(available.flatMap((item) => item.efforts || []))];
          const effortOptions = new Set(availableEfforts);
          if (phase === "archive") effortOptions.add("source-thread");
          effortOptions.add(efforts[phase] || fallbackEffort);
          return (
            <div key={phase}>
              {globalDefaults && (
                <h3 className="phase-heading">
                  <span aria-hidden="true">0{index + 1} /</span>
                  {label}
                </h3>
              )}
              <label>
                {globalDefaults ? "模型" : `${label}模型`}
                <select
                  aria-label={`${label}模型`}
                  value={custom[phase] ? "custom" : value[phase] || ""}
                  onChange={(event) => {
                    const manual = event.target.value === "custom";
                    setCustom((current) => ({ ...current, [phase]: manual }));
                    if (!manual) {
                      choose(phase, event.target.value);
                      const info = available.find(
                        (item) =>
                          item.model === (event.target.value || fallback),
                      );
                      const currentEffort = efforts[phase] || fallbackEffort;
                      if (
                        onEffortsChange &&
                        info?.efforts?.length &&
                        currentEffort !== "source-thread" &&
                        !info.efforts.includes(currentEffort)
                      )
                        chooseEffort(
                          phase,
                          info.default_effort || info.efforts[0],
                        );
                    }
                  }}
                >
                  <option value="">
                    {inheritManifest
                      ? "沿用清单与队列默认"
                      : `默认 · ${modelLabel(fallback)}`}
                  </option>
                  {!catalog && <option disabled>正在读取模型...</option>}
                  {[...options].map(([id, name]) => (
                    <option key={id} value={id}>
                      {name}
                    </option>
                  ))}
                  <option value="custom">自定义模型 ID</option>
                </select>
              </label>
              {custom[phase] && (
                <input
                  aria-label={`${label}自定义模型 ID`}
                  value={value[phase] || ""}
                  maxLength={200}
                  onChange={(event) => choose(phase, event.target.value.trim())}
                />
              )}
              {onEffortsChange && (
                <label className="effort-field">
                  {globalDefaults ? "推理强度" : `${label}推理强度`}
                  <select
                    aria-label={`${label}推理强度`}
                    value={efforts[phase] || ""}
                    onChange={(event) =>
                      chooseEffort(phase, event.target.value)
                    }
                  >
                    <option
                      value=""
                      disabled={
                        selectedInfo?.efforts?.length > 0 &&
                        fallbackEffort !== "source-thread" &&
                        !selectedInfo.efforts.includes(fallbackEffort)
                      }
                    >
                      {inheritManifest
                        ? "沿用清单与队列默认"
                        : `默认 · ${effortLabel(fallbackEffort)}`}
                    </option>
                    {[...effortOptions].map((effort) => (
                      <option
                        key={effort}
                        value={effort}
                        disabled={
                          selectedInfo?.efforts?.length > 0 &&
                          effort !== "source-thread" &&
                          !selectedInfo.efforts.includes(effort)
                        }
                      >
                        {effortLabel(effort)}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
          );
        })}
      </div>
      {catalog && Object.keys(catalog.errors || {}).length > 0 && (
        <p className="error-text" role="status">
          部分模型列表暂不可用
        </p>
      )}
    </fieldset>
  );
}
