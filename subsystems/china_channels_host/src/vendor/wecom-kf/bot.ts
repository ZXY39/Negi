import type { SyncMsgItem, SyncMsgText } from "./types.js";

export function isInboundCustomerTextMessage(msg: SyncMsgItem): msg is SyncMsgText {
  return msg.origin === 3 && msg.msgtype === "text";
}

export function extractInboundText(msg: SyncMsgItem): string | undefined {
  if (!isInboundCustomerTextMessage(msg)) return undefined;
  const content = msg.text?.content?.trim();
  return content || undefined;
}
