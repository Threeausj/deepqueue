export const titleOf = (thread) => thread.name || thread.preview || "新任务";

function pathOf(value) {
  if (!value?.startsWith("/")) return "";
  const parts = [];
  for (const part of value.split("/")) {
    if (!part || part === ".") continue;
    if (part === "..") parts.pop();
    else parts.push(part);
  }
  return "/" + parts.join("/");
}

const inside = (path, root) => path === root || path.startsWith(root + "/");

export function projectTree(threads, projects, codexHome) {
  const groups = new Map(
    projects.map((project) => [
      `project:${project.id}`,
      {
        key: `project:${project.id}`,
        projectId: project.id,
        name: project.name,
        paths: (project.roots || [])
          .map((root) => pathOf(root.path))
          .filter(Boolean),
        threads: [],
      },
    ]),
  );
  const standalone = {
    key: "standalone",
    name: "独立任务",
    paths: [],
    threads: [],
  };
  const home = pathOf(codexHome);
  for (const thread of threads) {
    let group;
    const cwd = pathOf(thread.cwd);
    if (thread.projectId) {
      const key = `project:${thread.projectId}`;
      if (!groups.has(key))
        groups.set(key, {
          key,
          projectId: thread.projectId,
          name: "其他项目",
          paths: [],
          threads: [],
        });
      group = groups.get(key);
    } else if (projects.length && Object.hasOwn(thread, "projectId")) {
      // A persisted null means an independent task, even when its cwd matches a project.
      group = standalone;
    } else {
      group = [...groups.values()]
        .filter((item) => item.projectId)
        .flatMap((item) =>
          item.paths
            .filter((root) => inside(cwd, root))
            .map((root) => ({ item, root })),
        )
        .sort((a, b) => b.root.length - a.root.length)[0]?.item;
      if (!group) {
        const isStandalone =
          !cwd ||
          ["/", "/root"].includes(cwd) ||
          /^\/(tmp|var\/tmp)(\/|$)/.test(cwd) ||
          /\/agents\/[a-f0-9]{32}$/.test(cwd) ||
          /^\/(home|Users)\/[^/]+$/.test(cwd) ||
          (home && inside(cwd, home));
        if (isStandalone) group = standalone;
        else {
          const key = `cwd:${cwd}`;
          if (!groups.has(key))
            groups.set(key, {
              key,
              name: cwd.split("/").at(-1),
              paths: [cwd],
              threads: [],
            });
          group = groups.get(key);
        }
      }
    }
    group.threads.push(thread);
  }
  return [...groups.values(), standalone].map((group) => ({
    ...group,
    cwd: group.paths[0] || "",
    threads: group.threads.sort(
      (a, b) =>
        (b.recencyAt || b.updatedAt || 0) - (a.recencyAt || a.updatedAt || 0),
    ),
  }));
}
