import {
  DEFAULT_ACCOUNT_ID,
  listWecomKfAccountIds,
  resolveAllowFrom,
  resolveDefaultWecomKfAccountId,
  resolveWecomKfAccount,
} from "./config.js";
import { getAccountState, updateAccountState } from "./state.js";
import { probeWecomKfAccount } from "./probe.js";
import { setWecomKfRuntime } from "./runtime.js";
import { registerWecomKfWebhookTarget, primeWecomKfCursor } from "./webhook.js";
import { sendKfTextMessage, summarizeSendResults } from "./api.js";
import { wecomKfOnboardingAdapter } from "./onboarding.js";
import type { PluginConfig, ResolvedWecomKfAccount, WecomKfConfig } from "./types.js";

export { DEFAULT_ACCOUNT_ID } from "./config.js";

type ParsedDirectTarget = {
  accountId?: string;
  userId: string;
};

const unregisterHooks = new Map<string, () => void>();

function parseDirectTarget(rawTarget: string): ParsedDirectTarget | null {
  let raw = String(rawTarget ?? "").trim();
  if (!raw) return null;
  if (/^wecom-kf:/i.test(raw)) {
    raw = raw.slice("wecom-kf:".length);
  }
  let accountId: string | undefined;
  const atIndex = raw.lastIndexOf("@");
  if (atIndex > 0 && atIndex < raw.length - 1) {
    accountId = raw.slice(atIndex + 1).trim();
    raw = raw.slice(0, atIndex);
  }
  if (/^user:/i.test(raw)) {
    raw = raw.slice("user:".length);
  }
  const userId = raw.trim();
  return userId ? { accountId, userId } : null;
}

const wecomKfConfigSchema = {
  type: "object",
  additionalProperties: false,
  properties: {
    enabled: { type: "boolean" },
    name: { type: "string" },
    defaultAccount: { type: "string" },
    webhookPath: { type: "string" },
    token: { type: "string" },
    encodingAESKey: { type: "string" },
    corpId: { type: "string" },
    corpSecret: { type: "string" },
    openKfId: { type: "string" },
    apiBaseUrl: { type: "string" },
    welcomeText: { type: "string" },
    dmPolicy: { type: "string", enum: ["open", "pairing", "allowlist", "disabled"] },
    allowFrom: { type: "array", items: { type: "string" } },
    accounts: {
      type: "object",
      additionalProperties: {
        type: "object",
        additionalProperties: false,
        properties: {
          name: { type: "string" },
          enabled: { type: "boolean" },
          webhookPath: { type: "string" },
          token: { type: "string" },
          encodingAESKey: { type: "string" },
          corpId: { type: "string" },
          corpSecret: { type: "string" },
          openKfId: { type: "string" },
          apiBaseUrl: { type: "string" },
          welcomeText: { type: "string" },
          dmPolicy: { type: "string", enum: ["open", "pairing", "allowlist", "disabled"] },
          allowFrom: { type: "array", items: { type: "string" } },
        },
      },
    },
  },
};

const meta = {
  id: "wecom-kf",
  label: "WeCom KF",
  selectionLabel: "WeCom Customer Service (微信客服)",
  docsPath: "/channels/wecom-kf",
  docsLabel: "wecom-kf",
  blurb: "微信客服渠道，支持外部微信用户咨询",
  aliases: ["weixin-kf", "微信客服", "企微客服"],
  order: 83,
} as const;

export const wecomKfPlugin = {
  id: "wecom-kf",

  meta: { ...meta },

  capabilities: {
    chatTypes: ["direct"] as const,
    media: false,
    reactions: false,
    threads: false,
    edit: false,
    reply: true,
    polls: false,
    activeSend: true,
  },

  messaging: {
    normalizeTarget: (raw: string): string | undefined => {
      const parsed = parseDirectTarget(raw);
      return parsed ? `user:${parsed.userId}${parsed.accountId ? `@${parsed.accountId}` : ""}` : undefined;
    },
    targetResolver: {
      looksLikeId: (raw: string, normalized?: string) => Boolean(parseDirectTarget(normalized ?? raw)),
      hint: "Use external_userid only: user:<external_userid> (optional @accountId).",
    },
    formatTargetDisplay: (params: { target: string; display?: string }) => {
      const parsed = parseDirectTarget(params.target);
      return parsed ? `user:${parsed.userId}` : params.display?.trim() || params.target;
    },
  },

  configSchema: {
    schema: wecomKfConfigSchema,
  },

  reload: { configPrefixes: ["channels.wecom-kf"] },

  onboarding: wecomKfOnboardingAdapter,

  config: {
    listAccountIds: (cfg: PluginConfig): string[] => listWecomKfAccountIds(cfg),
    resolveAccount: (cfg: PluginConfig, accountId?: string): ResolvedWecomKfAccount =>
      resolveWecomKfAccount({ cfg, accountId }),
    defaultAccountId: (cfg: PluginConfig): string => resolveDefaultWecomKfAccountId(cfg),
    setAccountEnabled: (params: {
      cfg: PluginConfig;
      accountId?: string;
      enabled: boolean;
    }): PluginConfig => {
      const accountId = params.accountId ?? DEFAULT_ACCOUNT_ID;
      const existing = (params.cfg.channels?.["wecom-kf"] ?? {}) as WecomKfConfig;
      const hasDedicatedAccount = Boolean(existing.accounts?.[accountId]);

      if (!hasDedicatedAccount || accountId === DEFAULT_ACCOUNT_ID) {
        return {
          ...params.cfg,
          channels: {
            ...params.cfg.channels,
            "wecom-kf": {
              ...existing,
              enabled: params.enabled,
            } as WecomKfConfig,
          },
        };
      }

      return {
        ...params.cfg,
        channels: {
          ...params.cfg.channels,
          "wecom-kf": {
            ...existing,
            accounts: {
              ...(existing.accounts ?? {}),
              [accountId]: {
                ...(existing.accounts?.[accountId] ?? {}),
                enabled: params.enabled,
              },
            },
          } as WecomKfConfig,
        },
      };
    },
    deleteAccount: (params: { cfg: PluginConfig; accountId?: string }): PluginConfig => {
      const accountId = params.accountId ?? DEFAULT_ACCOUNT_ID;
      const existing = (params.cfg.channels?.["wecom-kf"] ?? undefined) as WecomKfConfig | undefined;
      if (!existing) return params.cfg;

      if (accountId === DEFAULT_ACCOUNT_ID) {
        return {
          ...params.cfg,
          channels: {
            ...params.cfg.channels,
            "wecom-kf": {
              ...existing,
              enabled: false,
            } as WecomKfConfig,
          },
        };
      }

      const nextAccounts = { ...(existing.accounts ?? {}) };
      delete nextAccounts[accountId];
      return {
        ...params.cfg,
        channels: {
          ...params.cfg.channels,
          "wecom-kf": {
            ...existing,
            accounts: Object.keys(nextAccounts).length > 0 ? nextAccounts : undefined,
          } as WecomKfConfig,
        },
      };
    },
    isConfigured: (account: ResolvedWecomKfAccount): boolean => account.configured,
    describeAccount: (account: ResolvedWecomKfAccount) => ({
      accountId: account.accountId,
      name: account.name,
      enabled: account.enabled,
      configured: account.configured,
      canSendActive: account.canSendActive,
      webhookPath: account.config.webhookPath ?? "/wecom-kf",
    }),
    resolveAllowFrom: (params: { cfg: PluginConfig; accountId?: string }): string[] =>
      resolveAllowFrom(resolveWecomKfAccount({ cfg: params.cfg, accountId: params.accountId }).config),
    formatAllowFrom: (params: { allowFrom: (string | number)[] }): string[] =>
      params.allowFrom
        .map((entry) => String(entry).trim().toLowerCase())
        .filter(Boolean),
  },

  setup: {
    resolveAccountId: (params: { cfg: PluginConfig; accountId?: string }): string =>
      params.accountId?.trim() || resolveDefaultWecomKfAccountId(params.cfg),
    applyAccountConfig: (params: {
      cfg: PluginConfig;
      accountId?: string;
      config?: Record<string, unknown>;
    }): PluginConfig => {
      const accountId = params.accountId ?? DEFAULT_ACCOUNT_ID;
      const existing = (params.cfg.channels?.["wecom-kf"] ?? {}) as WecomKfConfig;
      if (accountId === DEFAULT_ACCOUNT_ID) {
        return {
          ...params.cfg,
          channels: {
            ...params.cfg.channels,
            "wecom-kf": {
              ...existing,
              ...params.config,
              enabled: true,
            } as WecomKfConfig,
          },
        };
      }

      return {
        ...params.cfg,
        channels: {
          ...params.cfg.channels,
          "wecom-kf": {
            ...existing,
            enabled: true,
            accounts: {
              ...(existing.accounts ?? {}),
              [accountId]: {
                ...(existing.accounts?.[accountId] ?? {}),
                ...params.config,
                enabled: true,
              },
            },
          } as WecomKfConfig,
        },
      };
    },
  },

  directory: {
    canResolve: (params: { target: string }) => Boolean(parseDirectTarget(params.target)),
    resolveTarget: (params: { cfg: PluginConfig; target: string }) => {
      const parsed = parseDirectTarget(params.target);
      return parsed
        ? {
            channel: "wecom-kf",
            accountId: parsed.accountId,
            to: parsed.userId,
          }
        : null;
    },
    resolveTargets: (params: { cfg: PluginConfig; targets: string[] }) => {
      const results: Array<{ channel: string; accountId?: string; to: string }> = [];
      for (const target of params.targets) {
        const resolved = wecomKfPlugin.directory.resolveTarget({ cfg: params.cfg, target });
        if (resolved) {
          results.push(resolved);
        }
      }
      return results;
    },
    getTargetFormats: () => ["wecom-kf:user:<external_userid>", "user:<external_userid>", "<external_userid>"],
  },

  outbound: {
    deliveryMode: "direct",
    sendText: async (params: {
      cfg: PluginConfig;
      accountId?: string;
      to: string;
      text: string;
    }) => {
      const parsed = parseDirectTarget(params.to);
      if (!parsed) {
        return {
          channel: "wecom-kf",
          ok: false,
          messageId: "",
          error: new Error(`Unsupported target for WeCom KF: ${params.to}`),
        };
      }

      const account = resolveWecomKfAccount({
        cfg: params.cfg,
        accountId: parsed.accountId ?? params.accountId,
      });
      if (!account.canSendActive) {
        return {
          channel: "wecom-kf",
          ok: false,
          messageId: "",
          error: new Error("Account not configured for active sending (missing corpId/corpSecret)"),
        };
      }

      try {
        const results = await sendKfTextMessage({
          account,
          externalUserId: parsed.userId,
          text: params.text,
        });
        const summary = summarizeSendResults(results);
        return {
          channel: "wecom-kf",
          ok: summary.ok,
          messageId: summary.msgid ?? "",
          error: summary.ok ? undefined : new Error(summary.error ?? "send failed"),
        };
      } catch (error) {
        return {
          channel: "wecom-kf",
          ok: false,
          messageId: "",
          error: error instanceof Error ? error : new Error(String(error)),
        };
      }
    },
  },

  gateway: {
    startAccount: async (ctx: {
      cfg: PluginConfig;
      runtime?: unknown;
      abortSignal?: AbortSignal;
      accountId: string;
      setStatus?: (status: Record<string, unknown>) => void;
      log?: { info: (message: string) => void; error: (message: string) => void };
    }): Promise<void> => {
      const account = resolveWecomKfAccount({ cfg: ctx.cfg, accountId: ctx.accountId });
      const path = (account.config.webhookPath ?? "/wecom-kf").trim() || "/wecom-kf";
      const runtimeCandidate = ctx.runtime as {
        channel?: {
          routing?: { resolveAgentRoute?: unknown };
          reply?: { dispatchReplyWithBufferedBlockDispatcher?: unknown };
        };
      } | undefined;
      if (
        runtimeCandidate?.channel?.routing?.resolveAgentRoute &&
        runtimeCandidate.channel.reply?.dispatchReplyWithBufferedBlockDispatcher
      ) {
        setWecomKfRuntime(ctx.runtime as Record<string, unknown>);
      }

      await updateAccountState(account.accountId, {
        configured: account.configured,
        webhookPath: path,
        running: false,
      });

      if (!account.configured) {
        ctx.log?.info(`[wecom-kf] account ${account.accountId} not configured; webhook not registered`);
        ctx.setStatus?.({ accountId: account.accountId, running: false, configured: false, webhookPath: path });
        return;
      }

      const registerTarget = {
        account,
        config: ctx.cfg,
        runtime: {
          log: ctx.log?.info ?? console.log,
          error: ctx.log?.error ?? console.error,
        },
        path,
        statusSink: (patch: Record<string, unknown>) =>
          ctx.setStatus?.({ accountId: account.accountId, ...patch }),
      };
      const unregister = registerWecomKfWebhookTarget(registerTarget);
      const previous = unregisterHooks.get(account.accountId);
      if (previous) previous();
      unregisterHooks.set(account.accountId, unregister);

      if (account.canSendActive) {
        await primeWecomKfCursor(registerTarget);
      } else {
        ctx.log?.info(
          `[wecom-kf] account ${account.accountId} missing corpSecret; webhook registered, finish callback verification, then configure corpSecret and restart to enable sync_msg/send_msg`
        );
      }

      const state = await getAccountState(account.accountId);
      const lastStartAt = Date.now();
      await updateAccountState(account.accountId, {
        running: true,
        lastStartAt,
        configured: true,
        webhookPath: path,
      });
      ctx.setStatus?.({
        accountId: account.accountId,
        running: true,
        configured: true,
        canSendActive: account.canSendActive,
        webhookPath: path,
        hasCursor: state.hasCursor,
        lastStartAt,
      });

      if (ctx.abortSignal) {
        await new Promise<void>((resolve) => {
          if (ctx.abortSignal?.aborted) {
            resolve();
            return;
          }
          ctx.abortSignal?.addEventListener("abort", () => resolve(), { once: true });
        });
      }
    },
    stopAccount: async (ctx: {
      accountId: string;
      setStatus?: (status: Record<string, unknown>) => void;
    }): Promise<void> => {
      const unregister = unregisterHooks.get(ctx.accountId);
      if (unregister) {
        unregister();
        unregisterHooks.delete(ctx.accountId);
      }
      const lastStopAt = Date.now();
      await updateAccountState(ctx.accountId, {
        running: false,
        lastStopAt,
      });
      ctx.setStatus?.({ accountId: ctx.accountId, running: false, lastStopAt });
    },
    getStatus: () => ({ connected: true }),
  },

  status: {
    probeAccount: async (params: { cfg: PluginConfig; accountId?: string }) =>
      probeWecomKfAccount({ cfg: params.cfg, accountId: params.accountId }),
  },
};
