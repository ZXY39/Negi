import type {
  PluginConfig,
  ResolvedWecomKfAccount,
  WecomKfAccountConfig,
  WecomKfConfig,
  WecomKfDmPolicy,
} from "./types.js";

export const DEFAULT_ACCOUNT_ID = "default";
const DEFAULT_API_BASE_URL = "https://qyapi.weixin.qq.com";

function getChannelConfig(cfg: PluginConfig): WecomKfConfig | undefined {
  return cfg.channels?.["wecom-kf"];
}

export function resolveApiBaseUrl(config: WecomKfAccountConfig): string {
  const raw = (config.apiBaseUrl ?? "").trim();
  return raw ? raw.replace(/\/+$/, "") : DEFAULT_API_BASE_URL;
}

export function resolveDmPolicy(config: WecomKfAccountConfig): WecomKfDmPolicy {
  return config.dmPolicy ?? "open";
}

export function resolveAllowFrom(config: WecomKfAccountConfig): string[] {
  return (config.allowFrom ?? [])
    .map((entry) => entry.trim().toLowerCase())
    .filter(Boolean);
}

export function checkDmPolicy(params: {
  dmPolicy: WecomKfDmPolicy;
  senderId: string;
  allowFrom?: string[];
}): { allowed: boolean; reason?: string } {
  const senderId = params.senderId.trim().toLowerCase();
  const allowFrom = (params.allowFrom ?? []).map((entry) => entry.trim().toLowerCase());

  switch (params.dmPolicy) {
    case "disabled":
      return { allowed: false, reason: "dm disabled" };
    case "allowlist":
      return allowFrom.includes(senderId)
        ? { allowed: true }
        : { allowed: false, reason: `sender ${senderId} not in allowlist` };
    case "pairing":
    case "open":
    default:
      return { allowed: true };
  }
}

export function listWecomKfAccountIds(cfg: PluginConfig): string[] {
  const channelCfg = getChannelConfig(cfg);
  if (!channelCfg) return [];
  const ids = [DEFAULT_ACCOUNT_ID];
  for (const accountId of Object.keys(channelCfg.accounts ?? {})) {
    if (accountId !== DEFAULT_ACCOUNT_ID) {
      ids.push(accountId);
    }
  }
  return ids;
}

export function resolveDefaultWecomKfAccountId(cfg: PluginConfig): string {
  return getChannelConfig(cfg)?.defaultAccount?.trim() || DEFAULT_ACCOUNT_ID;
}

export function mergeWecomKfAccountConfig(
  cfg: PluginConfig,
  accountId: string
): WecomKfAccountConfig {
  const channelCfg = getChannelConfig(cfg);
  const topLevel = (channelCfg ?? {}) as WecomKfAccountConfig;
  const accountCfg = (channelCfg?.accounts?.[accountId] ?? {}) as WecomKfAccountConfig;
  return { ...topLevel, ...accountCfg };
}

export function resolveWecomKfAccount(params: {
  cfg: PluginConfig;
  accountId?: string;
}): ResolvedWecomKfAccount {
  const accountId = params.accountId?.trim() || resolveDefaultWecomKfAccountId(params.cfg);
  const merged = mergeWecomKfAccountConfig(params.cfg, accountId);
  const baseEnabled = getChannelConfig(params.cfg)?.enabled !== false;
  const enabled = baseEnabled && merged.enabled !== false;
  const corpId =
    merged.corpId?.trim() ||
    (accountId === DEFAULT_ACCOUNT_ID ? process.env.WECOM_KF_CORP_ID?.trim() : undefined);
  const corpSecret =
    merged.corpSecret?.trim() ||
    (accountId === DEFAULT_ACCOUNT_ID ? process.env.WECOM_KF_CORP_SECRET?.trim() : undefined);
  const openKfId =
    merged.openKfId?.trim() ||
    (accountId === DEFAULT_ACCOUNT_ID ? process.env.WECOM_KF_OPEN_KF_ID?.trim() : undefined);
  const token =
    merged.token?.trim() ||
    (accountId === DEFAULT_ACCOUNT_ID ? process.env.WECOM_KF_TOKEN?.trim() : undefined);
  const encodingAESKey =
    merged.encodingAESKey?.trim() ||
    (accountId === DEFAULT_ACCOUNT_ID
      ? process.env.WECOM_KF_ENCODING_AES_KEY?.trim()
      : undefined);

  return {
    accountId,
    name: merged.name?.trim() || undefined,
    enabled,
    configured: Boolean(corpId && token && encodingAESKey),
    token,
    encodingAESKey,
    corpId,
    corpSecret,
    openKfId,
    canSendActive: Boolean(corpId && corpSecret),
    config: merged,
  };
}
