export async function api(path, body, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    ...(body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-DeepQueue": "1" },
          body: JSON.stringify(body),
        }),
  });
  const data = await response.json().catch(() => null);
  if (response.status === 401 || (path === "/auth/logout" && response.ok))
    window.dispatchEvent(new Event("deepqueue:auth-reset"));
  if (!response.ok) {
    const detail = data?.detail;
    const error = new Error(
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail
              .map((item) => `${item.loc?.slice(1).join(".")}: ${item.msg}`)
              .join("\n")
          : `请求失败 (${response.status})`,
    );
    error.status = response.status;
    throw error;
  }
  return data;
}

export function download(name, data) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
  );
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const waiting = ["pending", "estimating", "needs_review", "queued"];
export const active = ["starting", "running", "lost"];
export const terminal = ["succeeded", "failed", "cancelled", "blocked"];
export const idPath = (id) => encodeURIComponent(id);
export const serverLabel = (server) =>
  server?.config?.display_name || server?.name || "";
