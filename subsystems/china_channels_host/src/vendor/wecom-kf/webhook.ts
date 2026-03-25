import type { IncomingMessage, ServerResponse } from "http";

import { syncMessages, sendKfWelcomeMessage } from "./api.js";
import { decryptWecomEncrypted, verifyWecomSignature } from "./crypto.js";
import { dispatchKfMessage } from "./dispatch.js";
import { tryGetWecomKfRuntime } from "./runtime.js";
import {
  getStoredCursor,
  hasStoredCursor,
  markProcessedMessage,
  setStoredCursor,
  updateAccountState,
} from "./state.js";
import type { ResolvedWecomKfAccount, SyncMsgEvent, WebhookTarget } from "./types.js";

const WEBHOOK_TARGETS = new Map<string, WebhookTarget[]>();

function createLogger(target: WebhookTarget) {
  return {
    info: (message: string) => target.runtime.log(`[wecom-kf] ${message}`),
    warn: (message: string) => target.runtime.log(`[wecom-kf] [WARN] ${message}`),
    error: (message: string) => target.runtime.error(`[wecom-kf] [ERROR] ${message}`),
  };
}

function normalizeWebhookPath(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return "/";
  const withSlash = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  return withSlash.length > 1 && withSlash.endsWith("/") ? withSlash.slice(0, -1) : withSlash;
}

function resolvePath(req: IncomingMessage): string {
  const raw = req.url ?? "/";
  const queryIndex = raw.indexOf("?");
  return normalizeWebhookPath(queryIndex >= 0 ? raw.slice(0, queryIndex) : raw);
}

function resolveQueryParams(req: IncomingMessage): URLSearchParams {
  const raw = req.url ?? "/";
  const queryIndex = raw.indexOf("?");
  return new URLSearchParams(queryIndex >= 0 ? raw.slice(queryIndex + 1) : "");
}

function isXmlFormat(raw: string): boolean {
  return raw.trimStart().startsWith("<");
}

function parseXmlBody(raw: string): Record<string, string> {
  const result: Record<string, string> = {};
  const cdataRegex = /<(\w+)><!\[CDATA\[([\s\S]*?)\]\]><\/\1>/g;
  let match: RegExpExecArray | null = null;
  while ((match = cdataRegex.exec(raw)) !== null) {
    result[match[1]] = match[2];
  }
  const tagRegex = /<(\w+)>([^<]+)<\/\1>/g;
  while ((match = tagRegex.exec(raw)) !== null) {
    if (!result[match[1]]) {
      result[match[1]] = match[2];
    }
  }
  return result;
}

async function readRawBody(
  req: IncomingMessage,
  maxBytes: number
): Promise<{ ok: boolean; raw?: string; error?: string }> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = [];
    let total = 0;

    req.on("data", (chunk: Buffer) => {
      total += chunk.length;
      if (total > maxBytes) {
        resolve({ ok: false, error: "payload too large" });
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve({ ok: true, raw: Buffer.concat(chunks).toString("utf8") }));
    req.on("error", (error) => resolve({ ok: false, error: error.message }));
  });
}

function buildCursorKey(accountId: string, openKfId?: string): string {
  return `${accountId}:${openKfId ?? "all"}`;
}

async function handleEventMessage(
  msg: SyncMsgEvent,
  account: ResolvedWecomKfAccount,
  target: WebhookTarget
): Promise<void> {
  const logger = createLogger(target);
  const eventType = msg.event?.event_type ?? "";

  if (eventType === "enter_session") {
    const welcomeCode = msg.event?.welcome_code?.trim();
    const welcomeText = account.config.welcomeText?.trim();
    if (!welcomeCode || !welcomeText) {
      return;
    }

    try {
      const result = await sendKfWelcomeMessage(account, {
        code: welcomeCode,
        msgtype: "text",
        text: { content: welcomeText },
      });
      if (result.errcode !== 0) {
        const message = result.errmsg || `send welcome failed (errcode=${result.errcode})`;
        await updateAccountState(account.accountId, { lastError: message });
        logger.error(message);
        return;
      }

      await updateAccountState(account.accountId, { lastWelcomeAt: Date.now() });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await updateAccountState(account.accountId, { lastError: message });
      logger.error(`welcome send failed: ${message}`);
    }
    return;
  }

  if (eventType === "msg_send_fail") {
    const message = `msg_send_fail: ${msg.event?.fail_msgid ?? "unknown"} type=${String(
      msg.event?.fail_type ?? "unknown"
    )}`;
    await updateAccountState(account.accountId, { lastError: message });
    logger.warn(message);
  }
}

async function pullAndDispatchMessages(params: {
  target: WebhookTarget;
  callbackToken?: string;
  callbackOpenKfId?: string;
}): Promise<void> {
  const logger = createLogger(params.target);
  const account = params.target.account;
  const runtime = tryGetWecomKfRuntime();
  const effectiveOpenKfId = params.callbackOpenKfId?.trim() || account.openKfId?.trim();

  if (!account.canSendActive) {
    const message =
      "cannot pull messages before corpSecret is configured; finish callback verification, then configure corpSecret and restart";
    await updateAccountState(account.accountId, { lastError: message });
    logger.warn(message);
    return;
  }

  if (!effectiveOpenKfId) {
    const message = "cannot pull messages without open_kfid";
    await updateAccountState(account.accountId, { lastError: message });
    logger.warn(message);
    return;
  }

  const cursorKey = buildCursorKey(account.accountId, effectiveOpenKfId);
  let cursor = await getStoredCursor(cursorKey);
  let hasMore = true;

  while (hasMore) {
    try {
      const response = await syncMessages(account, {
        cursor,
        token: !cursor ? params.callbackToken : undefined,
        open_kfid: effectiveOpenKfId,
        limit: 1000,
      });

      if (response.next_cursor) {
        await setStoredCursor(cursorKey, response.next_cursor);
        cursor = response.next_cursor;
        await updateAccountState(account.accountId, {
          hasCursor: true,
          lastSyncAt: Date.now(),
        });
      }

      hasMore = response.has_more === 1;
      for (const msg of response.msg_list ?? []) {
        const firstSeen = await markProcessedMessage(msg.msgid);
        if (!firstSeen) {
          continue;
        }

        if (msg.msgtype === "event") {
          await handleEventMessage(msg as SyncMsgEvent, account, params.target);
          continue;
        }

        if (!runtime) {
          logger.warn(`runtime unavailable, skip msgid=${msg.msgid}`);
          continue;
        }

        await updateAccountState(account.accountId, { lastInboundAt: Date.now() });
        await dispatchKfMessage({
          cfg: params.target.config,
          account,
          msg,
          runtime,
          log: params.target.runtime.log,
          error: params.target.runtime.error,
        });
      }
    } catch (error) {
      hasMore = false;
      const message = error instanceof Error ? error.message : String(error);
      await updateAccountState(account.accountId, { lastError: message });
      logger.error(`sync_msg failed: ${message}`);
    }
  }
}

export async function primeWecomKfCursor(target: WebhookTarget): Promise<void> {
  if (!target.account.canSendActive) {
    return;
  }

  const openKfId = target.account.openKfId?.trim();
  if (!openKfId) {
    return;
  }

  const cursorKey = buildCursorKey(target.account.accountId, openKfId);
  if (await hasStoredCursor(cursorKey)) {
    await updateAccountState(target.account.accountId, { hasCursor: true });
    return;
  }

  const logger = createLogger(target);
  let cursor = "";
  let hasMore = true;

  logger.info(`priming cursor for account=${target.account.accountId} openKfId=${openKfId}`);
  while (hasMore) {
    try {
      const response = await syncMessages(target.account, {
        cursor,
        open_kfid: openKfId,
        limit: 1000,
      });
      if (response.next_cursor) {
        await setStoredCursor(cursorKey, response.next_cursor);
        cursor = response.next_cursor;
      }
      hasMore = response.has_more === 1;
    } catch (error) {
      hasMore = false;
      const message = error instanceof Error ? error.message : String(error);
      await updateAccountState(target.account.accountId, { lastError: message });
      logger.warn(`cursor prime failed: ${message}`);
    }
  }

  await updateAccountState(target.account.accountId, {
    hasCursor: Boolean(cursor),
    lastSyncAt: Date.now(),
  });
}

export function registerWecomKfWebhookTarget(target: WebhookTarget): () => void {
  const path = normalizeWebhookPath(target.path);
  const nextTarget = { ...target, path };
  const existing = WEBHOOK_TARGETS.get(path) ?? [];
  WEBHOOK_TARGETS.set(path, [...existing, nextTarget]);
  return () => {
    const updated = (WEBHOOK_TARGETS.get(path) ?? []).filter((entry) => entry !== nextTarget);
    if (updated.length > 0) {
      WEBHOOK_TARGETS.set(path, updated);
    } else {
      WEBHOOK_TARGETS.delete(path);
    }
  };
}

export async function handleWecomKfWebhookRequest(
  req: IncomingMessage,
  res: ServerResponse
): Promise<boolean> {
  const path = resolvePath(req);
  const targets = WEBHOOK_TARGETS.get(path);
  if (!targets || targets.length === 0) {
    return false;
  }

  const query = resolveQueryParams(req);
  const signature = query.get("msg_signature") ?? query.get("signature") ?? "";
  const timestamp = query.get("timestamp") ?? "";
  const nonce = query.get("nonce") ?? "";

  if (req.method === "GET") {
    const echostr = query.get("echostr") ?? "";
    const target = targets.find((candidate) => {
      const token = candidate.account.token?.trim();
      return (
        token &&
        verifyWecomSignature({
          token,
          timestamp,
          nonce,
          encrypt: echostr,
          signature,
        })
      );
    });

    if (!target || !echostr) {
      res.statusCode = 401;
      res.end("unauthorized");
      return true;
    }

    try {
      const plaintext = decryptWecomEncrypted({
        encodingAESKey: target.account.encodingAESKey ?? "",
        receiveId: target.account.corpId,
        encrypt: echostr,
      });
      res.statusCode = 200;
      res.setHeader("Content-Type", "text/plain; charset=utf-8");
      res.end(plaintext);
    } catch (error) {
      res.statusCode = 400;
      res.end(error instanceof Error ? error.message : String(error));
    }
    return true;
  }

  if (req.method !== "POST") {
    res.statusCode = 405;
    res.setHeader("Allow", "GET, POST");
    res.end("Method Not Allowed");
    return true;
  }

  const body = await readRawBody(req, 1024 * 1024);
  if (!body.ok || !body.raw) {
    res.statusCode = body.error === "payload too large" ? 413 : 400;
    res.end(body.error ?? "invalid payload");
    return true;
  }

  let encrypted = "";
  let effectiveSignature = signature;
  let effectiveTimestamp = timestamp;
  let effectiveNonce = nonce;

  if (isXmlFormat(body.raw)) {
    const xml = parseXmlBody(body.raw);
    encrypted = xml.Encrypt ?? "";
    effectiveSignature = xml.MsgSignature ?? signature;
    effectiveTimestamp = xml.TimeStamp ?? timestamp;
    effectiveNonce = xml.Nonce ?? nonce;
  } else {
    try {
      const parsed = JSON.parse(body.raw) as Record<string, unknown>;
      encrypted = String(parsed.encrypt ?? parsed.Encrypt ?? "");
    } catch {
      res.statusCode = 400;
      res.end("invalid payload");
      return true;
    }
  }

  const target = targets.find((candidate) => {
    const token = candidate.account.token?.trim();
    return (
      token &&
      encrypted &&
      verifyWecomSignature({
        token,
        timestamp: effectiveTimestamp,
        nonce: effectiveNonce,
        encrypt: encrypted,
        signature: effectiveSignature,
      })
    );
  });

  if (!target) {
    res.statusCode = 401;
    res.end("unauthorized");
    return true;
  }

  res.statusCode = 200;
  res.setHeader("Content-Type", "text/plain; charset=utf-8");
  res.end("success");

  let callbackToken: string | undefined;
  let callbackOpenKfId: string | undefined;
  try {
    const plaintext = decryptWecomEncrypted({
      encodingAESKey: target.account.encodingAESKey ?? "",
      receiveId: target.account.corpId,
      encrypt: encrypted,
    });
    if (isXmlFormat(plaintext)) {
      const xml = parseXmlBody(plaintext);
      callbackToken = xml.Token?.trim() || undefined;
      callbackOpenKfId = xml.OpenKfId?.trim() || undefined;
    } else {
      const parsed = JSON.parse(plaintext) as Record<string, unknown>;
      callbackToken = String(parsed.token ?? parsed.Token ?? "").trim() || undefined;
      callbackOpenKfId = String(parsed.open_kfid ?? parsed.OpenKfId ?? "").trim() || undefined;
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await updateAccountState(target.account.accountId, { lastError: message });
    createLogger(target).error(`callback decrypt failed: ${message}`);
  }

  void pullAndDispatchMessages({
    target,
    callbackToken,
    callbackOpenKfId,
  });

  return true;
}
