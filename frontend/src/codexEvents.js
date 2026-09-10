// App-server owns history. Streaming updates are reconciled with fresh thread reads.
export function applyCodexEvent(thread, event) {
  if (!thread) return thread;
  const p = event.params || {};
  const id = p.threadId || p.thread?.id;
  if (id !== thread.id) return thread;
  if (event.method === "thread/status/changed")
    return { ...thread, status: p.status };
  if (event.method === "thread/name/updated")
    return { ...thread, name: p.threadName || p.name };
  if (event.method === "thread/settings/updated") {
    const settings = p.threadSettings || {};
    return {
      ...thread,
      ...settings,
      ...(settings.sandboxPolicy ? { sandbox: settings.sandboxPolicy } : {}),
      ...("effort" in settings ? { reasoningEffort: settings.effort } : {}),
    };
  }
  let turns = [...(thread.turns || [])];
  const turnId = p.turnId || p.turn?.id;
  if (!turnId) return thread;
  let index = turns.findIndex((turn) => turn.id === turnId);
  if (index === -1) {
    index = turns.length;
    turns.push({ id: turnId, status: "inProgress", items: [] });
  }
  let turn = { ...turns[index], items: [...(turns[index].items || [])] };
  if (["turn/started", "turn/completed"].includes(event.method)) {
    turn = {
      ...turn,
      ...p.turn,
      items: p.turn.items?.length ? p.turn.items : turn.items,
    };
  } else if (["item/started", "item/completed"].includes(event.method)) {
    const itemIndex = turn.items.findIndex((item) => item.id === p.item.id);
    if (itemIndex === -1) turn.items.push(p.item);
    else turn.items[itemIndex] = { ...turn.items[itemIndex], ...p.item };
  } else if (
    p.itemId &&
    (event.method?.endsWith("/delta") ||
      [
        "item/commandExecution/outputDelta",
        "item/reasoning/summaryTextDelta",
      ].includes(event.method))
  ) {
    let itemIndex = turn.items.findIndex((item) => item.id === p.itemId);
    const type = event.method.split("/")[1];
    if (itemIndex === -1) {
      itemIndex = turn.items.length;
      turn.items.push({ id: p.itemId, type });
    }
    const item = { ...turn.items[itemIndex] };
    if (type === "agentMessage")
      item.text = (item.text || "") + (p.delta || "");
    if (type === "commandExecution")
      item.aggregatedOutput = (
        (item.aggregatedOutput || "") + (p.delta || "")
      ).slice(-131072);
    if (type === "reasoning")
      item.liveSummary = (item.liveSummary || "") + (p.delta || "");
    turn.items[itemIndex] = item;
  } else return thread;
  turns[index] = turn;
  return {
    ...thread,
    turns,
    ...(event.method === "turn/started" ? { status: { type: "active" } } : {}),
    ...(event.method === "turn/completed" ? { status: { type: "idle" } } : {}),
  };
}
