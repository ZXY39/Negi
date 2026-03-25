import { resolveWecomKfAccount } from "./config.js";
import { getAccessToken } from "./api.js";
import { getAccountState } from "./state.js";
import type { PluginConfig } from "./types.js";

export async function probeWecomKfAccount(params: {
  cfg: PluginConfig;
  accountId?: string;
}): Promise<{
  channel: "wecom-kf";
  accountId: string;
  configured: boolean;
  canSendActive: boolean;
  webhookPath?: string;
  hasCursor?: boolean;
  authOk: boolean;
  error?: string;
}> {
  const account = resolveWecomKfAccount({ cfg: params.cfg, accountId: params.accountId });
  const state = await getAccountState(account.accountId);

  if (!account.configured) {
    return {
      channel: "wecom-kf",
      accountId: account.accountId,
      configured: false,
      canSendActive: account.canSendActive,
      webhookPath: account.config.webhookPath ?? "/wecom-kf",
      hasCursor: state.hasCursor,
      authOk: false,
      error: "missing corpId/token/encodingAESKey",
    };
  }

  if (!account.canSendActive) {
    return {
      channel: "wecom-kf",
      accountId: account.accountId,
      configured: true,
      canSendActive: false,
      webhookPath: account.config.webhookPath ?? "/wecom-kf",
      hasCursor: state.hasCursor,
      authOk: false,
      error: "corpSecret not configured yet; finish callback verification, then configure corpSecret",
    };
  }

  try {
    await getAccessToken(account);
    return {
      channel: "wecom-kf",
      accountId: account.accountId,
      configured: true,
      canSendActive: account.canSendActive,
      webhookPath: account.config.webhookPath ?? "/wecom-kf",
      hasCursor: state.hasCursor,
      authOk: true,
    };
  } catch (error) {
    return {
      channel: "wecom-kf",
      accountId: account.accountId,
      configured: true,
      canSendActive: account.canSendActive,
      webhookPath: account.config.webhookPath ?? "/wecom-kf",
      hasCursor: state.hasCursor,
      authOk: false,
      error: error instanceof Error ? error.message : String(error),
    };
  }
}
