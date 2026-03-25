import type {
  AccessTokenCacheEntry,
  KfSendMsgParams,
  KfSendMsgResult,
  ResolvedWecomKfAccount,
  SyncMsgResponse,
} from "./types.js";
import { resolveApiBaseUrl } from "./config.js";

const ACCESS_TOKEN_CACHE = new Map<string, AccessTokenCacheEntry>();
const INVALID_ACCESS_TOKEN_ERRCODES = new Set([40001, 40014, 42001]);

function buildApiUrl(account: ResolvedWecomKfAccount, pathWithQuery: string): string {
  const normalizedPath = pathWithQuery.startsWith("/") ? pathWithQuery : `/${pathWithQuery}`;
  return `${resolveApiBaseUrl(account.config)}${normalizedPath}`;
}

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export async function getAccessToken(account: ResolvedWecomKfAccount): Promise<string> {
  if (!account.corpId || !account.corpSecret) {
    throw new Error("corpId or corpSecret not configured");
  }

  const key = `${account.corpId}:wecom-kf`;
  const cached = ACCESS_TOKEN_CACHE.get(key);
  if (cached && Date.now() < cached.expiresAt) {
    return cached.token;
  }

  const url = buildApiUrl(
    account,
    `/cgi-bin/gettoken?corpid=${encodeURIComponent(account.corpId)}&corpsecret=${encodeURIComponent(account.corpSecret)}`
  );
  const data = await readJson<{
    errcode?: number;
    errmsg?: string;
    access_token?: string;
    expires_in?: number;
  }>(await fetch(url));

  if (data.errcode !== undefined && data.errcode !== 0) {
    throw new Error(`gettoken failed: ${data.errmsg ?? "unknown error"} (errcode=${data.errcode})`);
  }
  if (!data.access_token) {
    throw new Error("gettoken returned empty access_token");
  }

  const expiresInMs = Math.max((data.expires_in ?? 7200) - 300, 60) * 1000;
  ACCESS_TOKEN_CACHE.set(key, {
    token: data.access_token,
    expiresAt: Date.now() + expiresInMs,
  });
  return data.access_token;
}

export function clearAccessTokenCache(account: ResolvedWecomKfAccount): void {
  const key = `${account.corpId}:wecom-kf`;
  ACCESS_TOKEN_CACHE.delete(key);
}

export function clearAllAccessTokenCache(): void {
  ACCESS_TOKEN_CACHE.clear();
}

async function callAuthenticatedJson<T extends { errcode?: number; errmsg?: string }>(
  account: ResolvedWecomKfAccount,
  buildPath: (accessToken: string) => string,
  init: Omit<RequestInit, "headers"> & { headers?: Record<string, string> } = {}
): Promise<T> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const accessToken = await getAccessToken(account);
    const url = buildApiUrl(account, buildPath(accessToken));
    const data = await readJson<T>(
      await fetch(url, {
        ...init,
        headers: {
          "Content-Type": "application/json",
          ...(init.headers ?? {}),
        },
      })
    );

    if (
      attempt === 0 &&
      data.errcode !== undefined &&
      INVALID_ACCESS_TOKEN_ERRCODES.has(data.errcode)
    ) {
      clearAccessTokenCache(account);
      continue;
    }

    return data;
  }

  throw new Error("authenticated API call exhausted retries");
}

export async function syncMessages(
  account: ResolvedWecomKfAccount,
  params: {
    cursor?: string;
    token?: string;
    open_kfid?: string;
    limit?: number;
    voice_format?: number;
  }
): Promise<SyncMsgResponse> {
  const body: Record<string, unknown> = {};
  if (params.cursor?.trim()) body.cursor = params.cursor.trim();
  if (params.token?.trim()) body.token = params.token.trim();
  if (params.open_kfid?.trim()) body.open_kfid = params.open_kfid.trim();
  if (typeof params.limit === "number") body.limit = params.limit;
  if (typeof params.voice_format === "number") body.voice_format = params.voice_format;

  return callAuthenticatedJson<SyncMsgResponse>(
    account,
    (accessToken) => `/cgi-bin/kf/sync_msg?access_token=${encodeURIComponent(accessToken)}`,
    {
      method: "POST",
      body: JSON.stringify(body),
    }
  );
}

export async function sendKfMessage(
  account: ResolvedWecomKfAccount,
  params: KfSendMsgParams
): Promise<KfSendMsgResult> {
  return callAuthenticatedJson<KfSendMsgResult>(
    account,
    (accessToken) => `/cgi-bin/kf/send_msg?access_token=${encodeURIComponent(accessToken)}`,
    {
      method: "POST",
      body: JSON.stringify(params),
    }
  );
}

export async function sendKfWelcomeMessage(
  account: ResolvedWecomKfAccount,
  params: KfSendMsgParams
): Promise<KfSendMsgResult> {
  return callAuthenticatedJson<KfSendMsgResult>(
    account,
    (accessToken) => `/cgi-bin/kf/send_msg_on_event?access_token=${encodeURIComponent(accessToken)}`,
    {
      method: "POST",
      body: JSON.stringify(params),
    }
  );
}

export function stripMarkdown(text: string): string {
  let result = text;
  result = result.replace(/```(\w*)\n?([\s\S]*?)```/g, (_match, lang, code) => {
    const trimmedCode = String(code).trim();
    if (!trimmedCode) return "";
    const language = lang ? `[${lang}]\n` : "";
    const indented = trimmedCode
      .split("\n")
      .map((line) => `    ${line}`)
      .join("\n");
    return `\n${language}${indented}\n`;
  });
  result = result.replace(/^#{1,6}\s+(.+)$/gm, "【$1】");
  result = result
    .replace(/\*\*(.*?)\*\*/g, "$1")
    .replace(/\*(.*?)\*/g, "$1")
    .replace(/__(.*?)__/g, "$1")
    .replace(/(?<![\w/])_(.+?)_(?![\w/])/g, "$1")
    .replace(/~~(.*?)~~/g, "$1");
  result = result.replace(/^[-*]\s+/gm, "· ");
  result = result.replace(/^(\d+)\.\s+/gm, "$1. ");
  result = result.replace(/`([^`]+)`/g, "$1");
  result = result.replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1 ($2)");
  result = result.replace(/!\[([^\]]*)\]\([^)]+\)/g, "[图片: $1]");
  result = result.replace(/^>\s?/gm, "");
  result = result.replace(/^[-*_]{3,}$/gm, "────────────");
  result = result.replace(/\n{3,}/g, "\n\n");
  return result.trim();
}

export function splitMessageByBytes(text: string, maxBytes = 2048): string[] {
  const chunks: string[] = [];
  let current = "";

  for (const char of text) {
    const candidate = current + char;
    if (Buffer.byteLength(candidate, "utf8") > maxBytes) {
      if (current) chunks.push(current);
      current = char;
    } else {
      current = candidate;
    }
  }

  if (current) chunks.push(current);
  return chunks;
}

export async function sendKfTextMessage(params: {
  account: ResolvedWecomKfAccount;
  externalUserId: string;
  text: string;
  openKfId?: string;
}): Promise<KfSendMsgResult[]> {
  const { account } = params;
  const openKfId = params.openKfId?.trim() || account.openKfId;
  if (!openKfId) {
    throw new Error("openKfId not available for text sending");
  }

  const chunks = splitMessageByBytes(stripMarkdown(params.text), 2048);
  const results: KfSendMsgResult[] = [];

  for (const chunk of chunks) {
    const result = await sendKfMessage(account, {
      touser: params.externalUserId,
      open_kfid: openKfId,
      msgtype: "text",
      text: { content: chunk },
    });
    results.push(result);
    if (result.errcode !== 0) {
      break;
    }
  }

  return results;
}

export function summarizeSendResults(results: KfSendMsgResult[]): {
  ok: boolean;
  msgid?: string;
  error?: string;
} {
  if (results.length === 0) {
    return { ok: false, error: "no send attempts executed" };
  }

  const failed = results.find((result) => result.errcode !== 0);
  if (failed) {
    return {
      ok: false,
      msgid: failed.msgid,
      error: failed.errmsg || `send failed (errcode=${failed.errcode})`,
    };
  }

  const last = results[results.length - 1];
  return { ok: true, msgid: last?.msgid };
}
