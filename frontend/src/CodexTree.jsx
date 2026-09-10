import { useEffect, useRef, useState } from "react";
import { ChevronRight, FolderOpen, MessageSquare, Plus } from "lucide-react";
import { IconButton } from "./components.jsx";
import { titleOf } from "./codexTree.js";

export default function CodexTree({
  groups,
  server,
  selected,
  search,
  ready,
  onOpen,
  onNew,
}) {
  const storageKey = `deepqueue.codex.tree.${server}`;
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem(storageKey)) || {};
    } catch {
      return {};
    }
  });
  const revealed = useRef(null);
  useEffect(() => {
    const group = groups.find((item) =>
      item.threads.some((t) => t.id === selected),
    );
    if (selected && selected !== revealed.current && group) {
      revealed.current = selected;
      setCollapsed((old) => ({ ...old, [group.key]: false }));
    }
  }, [groups, selected]);
  useEffect(() => {
    try {
      localStorage.setItem(storageKey, JSON.stringify(collapsed));
    } catch {
      // Tree navigation still works when browser storage is unavailable.
    }
  }, [storageKey, collapsed]);

  return (
    <ul className="codex-project-tree" aria-label="项目与任务">
      {groups
        .filter((group) => !search || group.threads.length)
        .map((group) => {
          const independent = group.key === "standalone";
          const open =
            search ||
            !(
              collapsed[group.key] ??
              (!group.threads.length ||
                (independent && group.threads.length > 8))
            );
          const Icon = independent ? MessageSquare : FolderOpen;
          return (
            <li className="codex-project" key={group.key}>
              <div className="codex-project-heading">
                <button
                  className="codex-project-toggle"
                  aria-expanded={!!open}
                  title={group.paths.join("\n") || group.name}
                  onClick={() =>
                    setCollapsed((old) => ({ ...old, [group.key]: !!open }))
                  }
                >
                  <ChevronRight size={13} className={open ? "expanded" : ""} />
                  <Icon size={15} />
                  <strong>{group.name}</strong>
                  <small>{group.threads.length}</small>
                </button>
                <IconButton
                  icon={Plus}
                  label={
                    independent ? "新建独立任务" : `在 ${group.name} 中新建任务`
                  }
                  disabled={!ready}
                  onClick={() => onNew(group)}
                />
              </div>
              {open && (
                <ul
                  className="codex-project-threads"
                  aria-label={`${group.name}的任务`}
                >
                  {group.threads.map((item) => (
                    <li key={item.id}>
                      <button
                        className={`codex-task ${selected === item.id ? "selected" : ""}`}
                        aria-current={selected === item.id ? "page" : undefined}
                        title={`${titleOf(item)}\n${item.cwd || ""}`}
                        onClick={() => onOpen(item.id)}
                      >
                        <span
                          className={`dot ${item.status?.type === "active" ? "green" : "gray"}`}
                        />
                        <strong>{titleOf(item)}</strong>
                        <small>
                          {item.updatedAt
                            ? new Date(
                                item.updatedAt * 1000,
                              ).toLocaleDateString("zh-CN", {
                                month: "2-digit",
                                day: "2-digit",
                              })
                            : ""}
                        </small>
                      </button>
                    </li>
                  ))}
                  {!group.threads.length && (
                    <li className="codex-group-empty">暂无任务</li>
                  )}
                </ul>
              )}
            </li>
          );
        })}
    </ul>
  );
}
