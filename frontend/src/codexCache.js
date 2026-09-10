const storageKey = "deepqueue.codex.history.v1";
const rows = new Map();
const drafts = new Map();
let loaded = false;
let timer;
function restore() {
  if (loaded) return;
  loaded = true;
  try {
    for (const [key, entry] of JSON.parse(
      sessionStorage.getItem(storageKey) || "[]",
    )) {
      if (Date.now() - entry.at < 30 * 60_000) rows.set(key, entry);
    }
  } catch {
    /* A cache is optional when browser storage is disabled. */
  }
}
export function historyKey(base, generation, id) {
  return `${base}:${generation || "disconnected"}:${id}`;
}
export function cachedHistory(key) {
  restore();
  const entry = rows.get(key);
  if (!entry || Date.now() - entry.at > 30 * 60_000) return null;
  rows.delete(key);
  rows.set(key, entry);
  return entry.thread;
}
export function cacheHistory(key, thread) {
  if (!thread?.id) return;
  restore();
  rows.delete(key);
  rows.set(key, { at: Date.now(), thread });
  while (rows.size > 24) rows.delete(rows.keys().next().value);
  clearTimeout(timer);
  timer = setTimeout(() => {
    try {
      const kept = [];
      let size = 0;
      for (const entry of [...rows].reverse()) {
        const bytes = JSON.stringify(entry).length;
        if (size + bytes > 2_000_000) continue;
        size += bytes;
        kept.unshift(entry);
      }
      sessionStorage.setItem(storageKey, JSON.stringify(kept));
    } catch {
      /* Quotas do not prevent in-memory caching. */
    }
  }, 800);
}
export function readDraft(key) {
  return drafts.get(key) || {};
}
export function saveDraft(key, value) {
  drafts.set(key, value);
  if (drafts.size > 100) drafts.delete(drafts.keys().next().value);
}
export function clearCodexCache() {
  clearTimeout(timer);
  rows.clear();
  drafts.clear();
  try {
    sessionStorage.removeItem(storageKey);
  } catch {
    /* Optional storage. */
  }
}
window.addEventListener("deepqueue:auth-reset", clearCodexCache);
