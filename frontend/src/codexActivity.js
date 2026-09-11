// Unknown message phases stay visible for compatibility with older providers.
export function conversationBlocks(items = []) {
  const blocks = [];
  for (const item of items || []) {
    const activity =
      item.type !== "userMessage" &&
      item.type !== "contextCompaction" &&
      (item.type !== "agentMessage" || item.phase === "commentary");
    const previous = blocks.at(-1);
    if (activity && previous?.activity) previous.items.push(item);
    else blocks.push({ id: item.id, activity, items: [item] });
  }
  return blocks;
}
