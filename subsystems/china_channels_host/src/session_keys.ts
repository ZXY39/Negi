// @ts-nocheck
export function normalizeAccountId(value?: string): string {
  return (value || "").trim() || "default";
}

export function buildSessionKey(params: {
  channel: string;
  accountId?: string;
  peer: { kind: string; id: string };
  threadId?: string;
}): string {
  const scope = ["group", "chat", "channel"].includes((params.peer.kind || "").toLowerCase())
    ? "group"
    : "dm";
  let key = `china:${params.channel}:${normalizeAccountId(params.accountId)}:${scope}:${params.peer.id}`;
  if (params.threadId?.trim()) {
    key += `:thread:${params.threadId.trim()}`;
  }
  return key;
}
