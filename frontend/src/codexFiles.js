// Convert a message's file reference to a project-relative preview target.
// The server still validates real paths and symlinks before reading content.
export function conversationFile(value, cwd) {
  if (typeof value !== "string" || !value || !cwd?.startsWith("/")) return null;
  let path = value.trim();
  if (
    !path ||
    path.startsWith("#") ||
    path.startsWith("?") ||
    path.startsWith("//")
  )
    return null;
  if (/^file:\/\//i.test(path)) {
    try {
      const url = new URL(path);
      if (url.hostname && url.hostname !== "localhost") return null;
      path = url.pathname + url.hash;
    } catch {
      return null;
    }
  } else if (
    /^[a-z][a-z0-9+.-]*:/i.test(path) &&
    !/^[^:/]+\.[a-z0-9]+:\d+(?::\d+)?$/.test(path)
  )
    return null;
  const anchor = path.match(/#L(\d+)(?:C\d+)?(?:-L?\d+(?:C\d+)?)?$/i);
  let line = anchor ? Number(anchor[1]) : null;
  if (anchor) path = path.slice(0, anchor.index);
  else path = path.split("#")[0];
  path = path.split("?")[0];
  try {
    path = decodeURIComponent(path);
  } catch {
    return null;
  }
  const location = path.match(/:(\d+)(?::\d+)?$/);
  if (location) {
    line = line || Number(location[1]);
    path = path.slice(0, location.index);
  }
  if (!path || /[\x00-\x1f\x7f]/.test(path)) return null;
  const normalize = (path) => {
    const parts = [];
    for (const part of path.split("/")) {
      if (!part || part === ".") continue;
      if (part === "..") parts.pop();
      else parts.push(part);
    }
    return "/" + parts.join("/");
  };
  const root = normalize(cwd),
    target = normalize(path.startsWith("/") ? path : `${root}/${path}`);
  if (target !== root && !target.startsWith(root === "/" ? "/" : root + "/"))
    return { path, line, error: "文件位于当前项目目录之外，无法在此预览" };
  return { path: target.slice(root === "/" ? 1 : root.length + 1), line };
}
