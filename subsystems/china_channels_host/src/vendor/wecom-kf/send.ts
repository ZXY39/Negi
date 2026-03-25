import { sendKfTextMessage, summarizeSendResults } from "./api.js";
import type { ResolvedWecomKfAccount } from "./types.js";

export type SendMessageOptions = {
  text: string;
  openKfId?: string;
};

export type SendResult = {
  ok: boolean;
  msgid?: string;
  error?: string;
};

function normalizeExternalUserId(rawTarget: string): string {
  let normalized = rawTarget.trim();
  if (normalized.startsWith("wecom-kf:")) {
    normalized = normalized.slice("wecom-kf:".length);
  }
  if (normalized.startsWith("user:")) {
    normalized = normalized.slice("user:".length);
  }
  const atIndex = normalized.lastIndexOf("@");
  if (atIndex > 0) {
    normalized = normalized.slice(0, atIndex);
  }
  return normalized.trim();
}

export async function sendWecomKfDM(
  account: ResolvedWecomKfAccount,
  target: string,
  options: SendMessageOptions
): Promise<SendResult> {
  if (!account.canSendActive) {
    return {
      ok: false,
      error: "Account not configured for active sending (missing corpId/corpSecret)",
    };
  }

  const externalUserId = normalizeExternalUserId(target);
  if (!externalUserId) {
    return { ok: false, error: "target is empty" };
  }

  try {
    const results = await sendKfTextMessage({
      account,
      externalUserId,
      text: options.text,
      openKfId: options.openKfId,
    });
    const summary = summarizeSendResults(results);
    return {
      ok: summary.ok,
      msgid: summary.msgid,
      error: summary.error,
    };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}
