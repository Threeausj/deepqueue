import React, { useEffect, useMemo, useRef, useState } from "react";
import { LoaderCircle, Puzzle, RefreshCw, Sparkles, X } from "lucide-react";
import { api } from "./api.js";
import { IconButton } from "./components.jsx";

export default function CodexComposer({
  endpoint,
  ready,
  text,
  setText,
  selections,
  setSelections,
  active,
}) {
  const [catalog, setCatalog] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [menu, setMenu] = useState(null);
  const [index, setIndex] = useState(0);
  const input = useRef(null);
  const flight = useRef(null);
  const live = useRef(true);
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
    };
  }, []);
  async function load(force = false) {
    if (flight.current || (catalog && !force) || !ready) return;
    setLoading(true);
    setError("");
    const request = api(`${endpoint}/extensions${force ? "?force=true" : ""}`);
    flight.current = request;
    try {
      const result = await request;
      if (live.current) setCatalog(result);
    } catch (error) {
      if (live.current) setError(error.message);
    } finally {
      if (flight.current === request) flight.current = null;
      if (live.current) setLoading(false);
    }
  }
  const items = useMemo(
    () =>
      [
        ...(catalog?.plugins || []).map((item) => ({
          kind: "plugin",
          id: item.id,
          name: item.name,
          label: item.interface?.displayName || item.name,
          description:
            item.interface?.shortDescription ||
            item.interface?.description ||
            item.marketplace ||
            "已启用插件",
        })),
        ...(catalog?.skills || []).map((item) => ({
          kind: "skill",
          id: item.path,
          name: item.name,
          label: item.interface?.displayName || item.name,
          description:
            item.interface?.shortDescription ||
            item.shortDescription ||
            item.description,
          pluginId: item.pluginId,
        })),
      ].filter(
        (item) =>
          !menu?.query ||
          `${item.label} ${item.name} ${item.description}`
            .toLowerCase()
            .includes(menu.query.toLowerCase()),
      ),
    [catalog, menu?.query],
  );
  function detect(value, caret) {
    const match = /(?:^|\s)\/([^\s/]*)$/.exec(value.slice(0, caret));
    if (match) {
      setMenu({
        start: caret - match[1].length - 1,
        end: caret,
        query: match[1],
      });
      setIndex(0);
      load();
    } else setMenu(null);
  }
  function choose(item) {
    if (!item || !menu) return;
    if (
      !selections.some(
        (entry) => entry.kind === item.kind && entry.id === item.id,
      )
    ) {
      if (selections.length >= 16) {
        setError("每次最多选择 16 个插件或 skill");
        return;
      }
      setSelections([...selections, item]);
    }
    const inserted = `$${item.name} `;
    setText(text.slice(0, menu.start) + inserted + text.slice(menu.end));
    const caret = menu.start + inserted.length;
    setMenu(null);
    requestAnimationFrame(() => {
      input.current?.focus();
      input.current?.setSelectionRange(caret, caret);
    });
  }
  function remove(item) {
    setSelections(
      selections.filter(
        (entry) => entry.id !== item.id || entry.kind !== item.kind,
      ),
    );
    setText(text.replace(`$${item.name}`, "").trimStart());
  }
  return (
    <div className="codex-input-wrap">
      {selections.length > 0 && (
        <div
          className="codex-extension-chips"
          aria-label="已选择的插件与 skills"
        >
          {selections.map((item) => (
            <span key={`${item.kind}:${item.id}`}>
              {item.kind === "plugin" ? (
                <Puzzle size={12} />
              ) : (
                <Sparkles size={12} />
              )}
              {item.label}
              <IconButton
                icon={X}
                label={`移除 ${item.label}`}
                onClick={() => remove(item)}
              />
            </span>
          ))}
        </div>
      )}
      {menu && (
        <div className="codex-slash-menu">
          <div className="codex-slash-heading">
            <strong>插件与 Skills</strong>
            <span>↑↓ 选择 · Enter 使用</span>
            <IconButton
              icon={RefreshCw}
              label="刷新插件与 Skills"
              disabled={loading}
              onClick={() => load(true)}
            />
            <IconButton
              icon={X}
              label="关闭插件菜单"
              onClick={() => setMenu(null)}
            />
          </div>
          {loading && (
            <p>
              <LoaderCircle size={14} className="spinning" />
              正在读取此项目的插件与 skills…
            </p>
          )}
          {error && (
            <p className="error-text" role="alert">
              {error}
            </p>
          )}
          {catalog?.errors?.map((error) => (
            <p className="subtle" key={error}>
              {error}
            </p>
          ))}
          <div
            role="listbox"
            aria-label="插件与 Skills"
            id="codex-slash-options"
          >
            {items.slice(0, 100).map((item, i) => (
              <button
                type="button"
                role="option"
                id={`codex-slash-${i}`}
                aria-selected={i === index}
                className={i === index ? "selected" : ""}
                key={`${item.kind}:${item.id}`}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => choose(item)}
              >
                {item.kind === "plugin" ? (
                  <Puzzle size={17} />
                ) : (
                  <Sparkles size={17} />
                )}
                <span>
                  <strong>{item.label}</strong>
                  <small>{item.description}</small>
                </span>
                <em>{item.kind === "plugin" ? "插件" : "Skill"}</em>
              </button>
            ))}
          </div>
          {!loading && !items.length && <p>没有匹配的插件或 skill</p>}
        </div>
      )}
      <textarea
        ref={input}
        aria-label="发给 Codex 的消息"
        aria-controls={menu ? "codex-slash-options" : undefined}
        aria-expanded={!!menu}
        aria-activedescendant={
          menu && items.length ? `codex-slash-${index}` : undefined
        }
        placeholder={
          active
            ? "补充要求，或输入 / 选择插件与 skill…"
            : "输入消息，或按 / 选择插件与 skill…"
        }
        value={text}
        disabled={!ready}
        onFocus={() => load()}
        onClick={(event) => detect(text, event.target.selectionStart)}
        onChange={(event) => {
          setText(event.target.value);
          detect(event.target.value, event.target.selectionStart);
        }}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing) return;
          if (menu && event.key === "Escape") {
            event.preventDefault();
            setMenu(null);
            return;
          }
          if (
            menu &&
            items.length &&
            ["ArrowDown", "ArrowUp"].includes(event.key)
          ) {
            event.preventDefault();
            const count = Math.min(items.length, 100);
            const next =
              (index + (event.key === "ArrowDown" ? 1 : -1) + count) % count;
            setIndex(next);
            document
              .getElementById(`codex-slash-${next}`)
              ?.scrollIntoView({ block: "nearest" });
            return;
          }
          if (
            menu &&
            items.length &&
            ["Enter", "Tab"].includes(event.key) &&
            !event.ctrlKey &&
            !event.metaKey
          ) {
            event.preventDefault();
            choose(items[index]);
            return;
          }
          if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            event.currentTarget.form.requestSubmit();
          }
        }}
      />
    </div>
  );
}
