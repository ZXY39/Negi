import {
  DEFAULT_ACCOUNT_ID,
  listQQBotAccountIds,
  mergeQQBotAccountConfig,
  resolveDefaultQQBotAccountId,
  resolveQQBotCredentials,
  type PluginConfig,
  type QQBotConfig,
} from "./config.js";

export interface WizardPrompter {
  note: (message: string, title?: string) => Promise<void>;
  text: (opts: {
    message: string;
    placeholder?: string;
    initialValue?: string;
    validate?: (value: string | undefined) => string | undefined;
  }) => Promise<string | symbol>;
  confirm: (opts: { message: string; initialValue?: boolean }) => Promise<boolean>;
  select: <T>(opts: {
    message: string;
    options: Array<{ value: T; label: string }>;
    initialValue?: T;
  }) => Promise<T | symbol>;
}

function isPromptCancelled<T>(value: T | symbol): value is symbol {
  return typeof value === "symbol";
}

function setQQBotCredentials(params: {
  cfg: PluginConfig;
  accountId: string;
  appId: string;
  clientSecret: string;
}): PluginConfig {
  const existing = params.cfg.channels?.qqbot ?? {};

  if (params.accountId === DEFAULT_ACCOUNT_ID) {
    return {
      ...params.cfg,
      channels: {
        ...params.cfg.channels,
        qqbot: {
          ...existing,
          enabled: true,
          appId: params.appId,
          clientSecret: params.clientSecret,
        } as QQBotConfig,
      },
    };
  }

  const accounts = (existing as QQBotConfig).accounts ?? {};
  return {
    ...params.cfg,
    channels: {
      ...params.cfg.channels,
      qqbot: {
        ...existing,
        enabled: true,
        accounts: {
          ...accounts,
          [params.accountId]: {
            ...accounts[params.accountId],
            enabled: true,
            appId: params.appId,
            clientSecret: params.clientSecret,
          },
        },
      } as QQBotConfig,
    },
  };
}

async function noteQQBotCredentialHelp(prompter: WizardPrompter): Promise<void> {
  await prompter.note(
    [
      "1) 打开 QQ 开放平台 (https://q.qq.com/)",
      "2) 创建机器人应用，获取 AppID 和 ClientSecret",
      "3) 在开发设置中配置沙箱成员或测试群",
      "4) 配置完成后可使用 openclaw gateway 启动连接",
      "",
      "命令行也支持：openclaw channels add --channel qqbot --token \"AppID:ClientSecret\"",
    ].join("\n"),
    "QQ Bot 配置"
  );
}

function resolveOnboardingAccountId(params: {
  cfg: PluginConfig;
  prompter: WizardPrompter;
  shouldPromptAccountIds?: boolean;
  accountOverrides?: Record<string, string> | undefined;
}): Promise<string> | string {
  const override = params.accountOverrides?.qqbot?.trim();
  if (override) return override;

  const defaultAccountId = resolveDefaultQQBotAccountId(params.cfg);
  const accountIds = listQQBotAccountIds(params.cfg);

  if (!params.shouldPromptAccountIds || accountIds.length <= 1) {
    return defaultAccountId;
  }

  return params.prompter
    .select({
      message: "选择要配置的 QQ Bot 账户",
      options: accountIds.map((accountId) => ({
        value: accountId,
        label: accountId === DEFAULT_ACCOUNT_ID ? "默认账户" : accountId,
      })),
      initialValue: defaultAccountId,
    })
    .then((selected) => (isPromptCancelled(selected) ? defaultAccountId : selected));
}

export const qqbotOnboardingAdapter = {
  channel: "qqbot" as const,

  getStatus: async (params: { cfg: PluginConfig }) => {
    const accountIds = listQQBotAccountIds(params.cfg);
    const configuredAccountId = accountIds.find((accountId) =>
      Boolean(resolveQQBotCredentials(mergeQQBotAccountConfig(params.cfg, accountId)))
    );
    const configured = Boolean(configuredAccountId);
    const defaultAccountId = resolveDefaultQQBotAccountId(params.cfg);

    const statusLines = configured
      ? [
          configuredAccountId && configuredAccountId !== DEFAULT_ACCOUNT_ID
            ? `QQ Bot: 已配置 (${configuredAccountId})`
            : `QQ Bot: 已配置${defaultAccountId !== DEFAULT_ACCOUNT_ID ? ` (default=${defaultAccountId})` : ""}`,
        ]
      : ["QQ Bot: 需要 AppID 和 ClientSecret"];

    return {
      channel: "qqbot" as const,
      configured,
      statusLines,
      selectionHint: configured ? "已配置" : "需要 AppID 和 ClientSecret",
      quickstartScore: configured ? 2 : 0,
    };
  },

  configure: async (params: {
    cfg: PluginConfig;
    prompter: WizardPrompter;
    shouldPromptAccountIds?: boolean;
    accountOverrides?: Record<string, string>;
  }) => {
    const accountId = await resolveOnboardingAccountId(params);
    const merged = mergeQQBotAccountConfig(params.cfg, accountId);
    const configured = Boolean(resolveQQBotCredentials(merged));

    let next = params.cfg;
    let appId: string | null = null;
    let clientSecret: string | null = null;

    if (!configured) {
      await noteQQBotCredentialHelp(params.prompter);
    } else {
      const keepCurrent = await params.prompter.confirm({
        message:
          accountId === DEFAULT_ACCOUNT_ID
            ? "QQ Bot 凭证已配置，是否保留当前配置？"
            : `账户 ${accountId} 的 QQ Bot 凭证已配置，是否保留当前配置？`,
        initialValue: true,
      });

      if (keepCurrent) {
        return { cfg: next, accountId };
      }
    }

    const nextAppId = await params.prompter.text({
      message: "请输入 QQ Bot AppID",
      placeholder: "例如: 102146862",
      initialValue: typeof merged.appId === "string" ? merged.appId : undefined,
      validate: (value) => (String(value ?? "").trim() ? undefined : "AppID 不能为空"),
    });
    if (isPromptCancelled(nextAppId)) {
      return { cfg: next, accountId };
    }
    appId = String(nextAppId).trim();

    const nextClientSecret = await params.prompter.text({
      message: "请输入 QQ Bot ClientSecret",
      placeholder: "你的 ClientSecret",
      validate: (value) => (String(value ?? "").trim() ? undefined : "ClientSecret 不能为空"),
    });
    if (isPromptCancelled(nextClientSecret)) {
      return { cfg: next, accountId };
    }
    clientSecret = String(nextClientSecret).trim();

    if (appId && clientSecret) {
      next = setQQBotCredentials({
        cfg: next,
        accountId,
        appId,
        clientSecret,
      });
    }

    return { cfg: next, accountId };
  },

  disable: (cfg: PluginConfig): PluginConfig => ({
    ...cfg,
    channels: {
      ...cfg.channels,
      qqbot: {
        ...(cfg.channels?.qqbot ?? {}),
        enabled: false,
      } as QQBotConfig,
    },
  }),
};
