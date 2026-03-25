import { randomUUID } from "node:crypto";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";

import type { WSClient } from "@wecom/aibot-node-sdk";

type WecomRuntimeEnv = {
  log?: (message: string) => void;
  error?: (message: string) => void;
};

type McpConfigResponse = {
  errcode?: number;
  errmsg?: string;
  body?: unknown;
};

type PersistedDocConfig = {
  type: string;
  url: string;
};

type PersistedWecomMcpAccountConfig = {
  fetchedAt?: string;
  isAuthed?: boolean;
  mcpConfig?: {
    doc?: PersistedDocConfig;
    [key: string]: unknown;
  };
  [key: string]: unknown;
};

type PersistedWecomMcpFile = {
  updatedAt?: string;
  mcpConfig?: {
    doc?: PersistedDocConfig;
    [key: string]: unknown;
  };
  accounts?: Record<string, PersistedWecomMcpAccountConfig>;
  [key: string]: unknown;
};

export interface WecomDocMcpConfig {
  bizType: "doc";
  url: string;
  type: string;
  isAuthed?: boolean;
  fetchedAt: number;
}

const DOC_BIZ_TYPE = "doc";
const DEFAULT_DOC_MCP_TYPE = "streamable-http";
const MCP_GET_CONFIG_CMD = "aibot_get_mcp_config";
const MCP_FETCH_TIMEOUT_MS = 5_000;
const writeQueues = new Map<string, Promise<void>>();

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function resolveOpenClawStateDir(): string {
  const override = process.env.OPENCLAW_STATE_DIR?.trim() || process.env.CLAWDBOT_STATE_DIR?.trim();
  if (override) {
    if (override.startsWith("~")) {
      const home = os.homedir();
      const normalized = override === "~" ? home : path.join(home, override.slice(2));
      return path.resolve(normalized);
    }
    return path.resolve(override);
  }
  return path.join(os.homedir(), ".openclaw");
}

export function resolveWecomMcpConfigPath(): string {
  return path.join(resolveOpenClawStateDir(), "wecomConfig", "config.json");
}

function withTimeout<T>(promise: Promise<T>, timeoutMs: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(label));
    }, timeoutMs);

    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      }
    );
  });
}

function readResponseField(body: unknown, key: string): string | undefined {
  if (!isRecord(body)) return undefined;
  const value = body[key];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function readResponseBoolean(body: unknown, key: string): boolean | undefined {
  if (!isRecord(body)) return undefined;
  const value = body[key];
  return typeof value === "boolean" ? value : undefined;
}

export async function fetchWecomDocMcpConfig(client: WSClient): Promise<WecomDocMcpConfig> {
  const reqId = randomUUID();
  const response = await withTimeout(
    client.reply(
      {
        headers: {
          req_id: reqId,
        },
      },
      { biz_type: DOC_BIZ_TYPE },
      MCP_GET_CONFIG_CMD
    ) as Promise<McpConfigResponse>,
    MCP_FETCH_TIMEOUT_MS,
    `WeCom doc MCP config fetch timed out after ${MCP_FETCH_TIMEOUT_MS}ms`
  );

  if (typeof response.errcode === "number" && response.errcode !== 0) {
    throw new Error(`WeCom doc MCP config request failed: ${response.errcode} ${response.errmsg ?? ""}`.trim());
  }

  const url = readResponseField(response.body, "url");
  if (!url) {
    throw new Error("WeCom doc MCP config response missing url");
  }

  return {
    bizType: DOC_BIZ_TYPE,
    url,
    type: readResponseField(response.body, "type") ?? DEFAULT_DOC_MCP_TYPE,
    isAuthed: readResponseBoolean(response.body, "is_authed"),
    fetchedAt: Date.now(),
  };
}

async function readPersistedConfig(filePath: string): Promise<PersistedWecomMcpFile> {
  try {
    const raw = await fs.readFile(filePath, "utf8");
    const parsed = JSON.parse(raw) as unknown;
    return isRecord(parsed) ? (parsed as PersistedWecomMcpFile) : {};
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return {};
    }
    return {};
  }
}

async function writePersistedConfig(filePath: string, data: PersistedWecomMcpFile): Promise<void> {
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  const tempPath = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  const payload = `${JSON.stringify(data, null, 2)}\n`;
  await fs.writeFile(tempPath, payload, "utf8");
  await fs.rename(tempPath, filePath);
}

async function serializeWrite(filePath: string, action: () => Promise<void>): Promise<void> {
  const previous = writeQueues.get(filePath) ?? Promise.resolve();
  const next = previous.catch(() => undefined).then(action);
  writeQueues.set(filePath, next);
  try {
    await next;
  } finally {
    if (writeQueues.get(filePath) === next) {
      writeQueues.delete(filePath);
    }
  }
}

export async function saveWecomDocMcpConfig(params: {
  accountId: string;
  config: WecomDocMcpConfig;
}): Promise<void> {
  const filePath = resolveWecomMcpConfigPath();
  const docConfig: PersistedDocConfig = {
    type: params.config.type || DEFAULT_DOC_MCP_TYPE,
    url: params.config.url,
  };

  await serializeWrite(filePath, async () => {
    const current = await readPersistedConfig(filePath);
    const currentAccounts = isRecord(current.accounts)
      ? (current.accounts as Record<string, PersistedWecomMcpAccountConfig>)
      : {};
    const existingAccount = isRecord(currentAccounts[params.accountId])
      ? (currentAccounts[params.accountId] as PersistedWecomMcpAccountConfig)
      : {};
    const existingAccountMcpConfig = isRecord(existingAccount.mcpConfig)
      ? (existingAccount.mcpConfig as Record<string, unknown>)
      : {};

    current.updatedAt = new Date(params.config.fetchedAt).toISOString();
    current.mcpConfig = {
      ...(isRecord(current.mcpConfig) ? current.mcpConfig : {}),
      doc: docConfig,
    };
    current.accounts = {
      ...currentAccounts,
      [params.accountId]: {
        ...existingAccount,
        fetchedAt: new Date(params.config.fetchedAt).toISOString(),
        isAuthed: params.config.isAuthed,
        mcpConfig: {
          ...existingAccountMcpConfig,
          doc: docConfig,
        },
      },
    };

    await writePersistedConfig(filePath, current);
  });
}

export async function fetchAndSaveWecomDocMcpConfig(params: {
  client: WSClient;
  accountId: string;
  runtime?: WecomRuntimeEnv;
}): Promise<void> {
  try {
    const config = await fetchWecomDocMcpConfig(params.client);
    await saveWecomDocMcpConfig({
      accountId: params.accountId,
      config,
    });
    params.runtime?.log?.(
      `[wecom] doc MCP config saved for account ${params.accountId} at ${resolveWecomMcpConfigPath()}`
    );
  } catch (error) {
    params.runtime?.error?.(
      `[wecom] failed to fetch/save doc MCP config for account ${params.accountId}: ${String(error)}`
    );
  }
}
