import {
  DEFAULT_ACCOUNT_ID,
  listWecomKfAccountIds,
  mergeWecomKfAccountConfig,
  resolveDefaultWecomKfAccountId,
} from "./config.js";
import type { PluginConfig, WecomKfConfig } from "./types.js";
import { getAccountState } from "./state.js";

export interface WizardPrompter {
  note: (message: string, title?: string) => Promise<void>;
  text: (opts: {
    message: string;
    placeholder?: string;
    initialValue?: string;
    validate?: (value: string | undefined) => string | undefined;
  }) => Promise<string | symbol>;
  confirm: (opts: { message: string; initialValue?: boolean }) => Promise<boolean>;
}

function isPromptCancelled<T>(value: T | symbol): value is symbol {
  return typeof value === "symbol";
}

function setAccountConfig(params: {
  cfg: PluginConfig;
  accountId: string;
  nextConfig: Record<string, unknown>;
}): PluginConfig {
  const existing = params.cfg.channels?.["wecom-kf"] ?? {};
  if (params.accountId === DEFAULT_ACCOUNT_ID) {
    return {
      ...params.cfg,
      channels: {
        ...params.cfg.channels,
        "wecom-kf": {
          ...existing,
          ...params.nextConfig,
          enabled: true,
        } as WecomKfConfig,
      },
    };
  }

  const accounts = (existing as WecomKfConfig).accounts ?? {};
  return {
    ...params.cfg,
    channels: {
      ...params.cfg.channels,
      "wecom-kf": {
        ...existing,
        enabled: true,
        accounts: {
          ...accounts,
          [params.accountId]: {
            ...accounts[params.accountId],
            ...params.nextConfig,
            enabled: true,
          },
        },
      } as WecomKfConfig,
    },
  };
}

async function noteWecomKfHelp(prompter: WizardPrompter): Promise<void> {
  await prompter.note(
    [
      "1) 在微信客服管理后台开启 API",
      "2) 先配置回调 URL、Token、EncodingAESKey、企业 ID 和 open_kfid",
      "3) 微信客服 Secret 会在最后询问，首次接入可先留空",
      "4) 回调校验通过并点击“开始使用”后，再补微信客服 Secret",
      "5) 将客服账号授权给可调用接口的自建应用",
    ].join("\n"),
    "WeCom KF 配置"
  );
}

export const wecomKfOnboardingAdapter = {
  channel: "wecom-kf" as const,

  getStatus: async (params: { cfg: PluginConfig }) => {
    const accountId = resolveDefaultWecomKfAccountId(params.cfg);
    const merged = mergeWecomKfAccountConfig(params.cfg, accountId);
    const state = await getAccountState(accountId);
    const hasCorpSecret = Boolean(merged.corpSecret?.trim());
    const hasOpenKfId = Boolean(merged.openKfId?.trim());
    const configured = Boolean(
      merged.corpId?.trim() &&
        merged.token?.trim() &&
        merged.encodingAESKey?.trim()
    );

    return {
      channel: "wecom-kf" as const,
      configured,
      statusLines: [
        configured
          ? `WeCom KF: 已配置${accountId !== DEFAULT_ACCOUNT_ID ? ` (${accountId})` : ""}`
          : "WeCom KF: 需要 corpId / token / encodingAESKey",
        `Webhook: ${(merged.webhookPath ?? "/wecom-kf").trim() || "/wecom-kf"}`,
        hasCorpSecret ? "corpSecret: 已配置" : "corpSecret: 未配置（回调校验通过后再补）",
        hasOpenKfId ? "open_kfid: 已配置" : "open_kfid: 未配置（主动发送与启动预热不可用）",
        `Cursor: ${state.hasCursor ? "已建立" : "未建立"}`,
        state.lastError ? `最近错误: ${state.lastError}` : "最近错误: 无",
      ],
      selectionHint: configured
        ? hasCorpSecret
          ? hasOpenKfId
            ? "已配置"
            : "已配置，建议补 open_kfid"
          : "已配置，待补 corpSecret"
        : "需要基础凭证",
      quickstartScore: configured ? (hasCorpSecret && hasOpenKfId ? 2 : 1) : 0,
    };
  },

  configure: async (params: {
    cfg: PluginConfig;
    prompter: WizardPrompter;
    accountOverrides?: Record<string, string>;
  }) => {
    const requestedAccountId = params.accountOverrides?.["wecom-kf"]?.trim();
    const accountIds = listWecomKfAccountIds(params.cfg);
    const accountId =
      requestedAccountId ||
      (accountIds.length > 0 ? resolveDefaultWecomKfAccountId(params.cfg) : DEFAULT_ACCOUNT_ID);
    const merged = mergeWecomKfAccountConfig(params.cfg, accountId);

    await noteWecomKfHelp(params.prompter);

    const webhookPath = await params.prompter.text({
      message: "请输入 webhookPath",
      initialValue: merged.webhookPath ?? "/wecom-kf",
      validate: (value) => (String(value ?? "").trim() ? undefined : "webhookPath 不能为空"),
    });
    if (isPromptCancelled(webhookPath)) return { cfg: params.cfg, accountId };

    const token = await params.prompter.text({
      message: "请输入回调 Token",
      initialValue: merged.token,
      validate: (value) => (String(value ?? "").trim() ? undefined : "token 不能为空"),
    });
    if (isPromptCancelled(token)) return { cfg: params.cfg, accountId };

    const encodingAESKey = await params.prompter.text({
      message: "请输入回调 EncodingAESKey",
      initialValue: merged.encodingAESKey,
      validate: (value) => (String(value ?? "").trim() ? undefined : "encodingAESKey 不能为空"),
    });
    if (isPromptCancelled(encodingAESKey)) return { cfg: params.cfg, accountId };

    const corpId = await params.prompter.text({
      message: "请输入企业 ID (corpId)",
      initialValue: merged.corpId,
      validate: (value) => (String(value ?? "").trim() ? undefined : "corpId 不能为空"),
    });
    if (isPromptCancelled(corpId)) return { cfg: params.cfg, accountId };

    const openKfId = await params.prompter.text({
      message: "请输入客服账号 ID (open_kfid)",
      initialValue: merged.openKfId,
      validate: (value) => (String(value ?? "").trim() ? undefined : "open_kfid 不能为空"),
    });
    if (isPromptCancelled(openKfId)) return { cfg: params.cfg, accountId };

    const corpSecret = await params.prompter.text({
      message: "请输入微信客服 Secret（最后填写；首次接入可先留空）",
      initialValue: merged.corpSecret,
    });
    if (isPromptCancelled(corpSecret)) return { cfg: params.cfg, accountId };

    return {
      cfg: setAccountConfig({
        cfg: params.cfg,
        accountId,
        nextConfig: {
          webhookPath: String(webhookPath).trim(),
          token: String(token).trim(),
          encodingAESKey: String(encodingAESKey).trim(),
          corpId: String(corpId).trim(),
          openKfId: String(openKfId).trim(),
          corpSecret: String(corpSecret).trim() || undefined,
        },
      }),
      accountId,
    };
  },

  disable: (cfg: PluginConfig): PluginConfig => ({
    ...cfg,
    channels: {
      ...cfg.channels,
      "wecom-kf": {
        ...(cfg.channels?.["wecom-kf"] as WecomKfConfig | undefined),
        enabled: false,
      },
    },
  }),
};
