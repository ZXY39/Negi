import { extractInboundText } from "./bot.js";
import { checkDmPolicy, resolveAllowFrom, resolveDmPolicy } from "./config.js";
import { sendKfTextMessage, summarizeSendResults } from "./api.js";
import { updateAccountState } from "./state.js";
import type { PluginConfig, PluginRuntime, ResolvedWecomKfAccount, SyncMsgItem } from "./types.js";

function createLogger(opts: { log?: (message: string) => void; error?: (message: string) => void }) {
  return {
    info: (message: string) => (opts.log ?? console.log)(`[wecom-kf] ${message}`),
    warn: (message: string) => (opts.log ?? console.log)(`[wecom-kf] [WARN] ${message}`),
    error: (message: string) => (opts.error ?? console.error)(`[wecom-kf] [ERROR] ${message}`),
  };
}

export async function dispatchKfMessage(params: {
  cfg: PluginConfig;
  account: ResolvedWecomKfAccount;
  msg: SyncMsgItem;
  runtime: PluginRuntime;
  log?: (message: string) => void;
  error?: (message: string) => void;
}): Promise<void> {
  const logger = createLogger({ log: params.log, error: params.error });
  const rawText = extractInboundText(params.msg);

  if (!rawText) {
    logger.info(`skip unsupported inbound msgtype=${params.msg.msgtype} msgid=${params.msg.msgid}`);
    return;
  }

  const senderId = (params.msg.external_userid ?? "").trim();
  if (!senderId) {
    logger.warn(`skip inbound msgid=${params.msg.msgid} without external_userid`);
    return;
  }

  const dmPolicy = resolveDmPolicy(params.account.config);
  const policyResult = checkDmPolicy({
    dmPolicy,
    senderId,
    allowFrom: resolveAllowFrom(params.account.config),
  });
  if (!policyResult.allowed) {
    logger.info(`skip sender=${senderId} reason=${policyResult.reason ?? "policy rejected"}`);
    return;
  }

  const channel = params.runtime.channel;
  const resolveAgentRoute = channel?.routing?.resolveAgentRoute;
  const dispatchReply = channel?.reply?.dispatchReplyWithBufferedBlockDispatcher;
  if (!resolveAgentRoute || !dispatchReply) {
    const message = "runtime routing or buffered reply dispatcher unavailable";
    logger.warn(message);
    await updateAccountState(params.account.accountId, { lastError: message });
    return;
  }

  const route = resolveAgentRoute({
    cfg: params.cfg,
    channel: "wecom-kf",
    accountId: params.account.accountId,
    peer: { kind: "dm", id: senderId },
  });

  const fromLabel = `user:${senderId}`;
  const from = `wecom-kf:user:${senderId}`;
  const to = `user:${senderId}`;
  const storePath = channel.session?.resolveStorePath?.(params.cfg.session?.store, {
    agentId: route.agentId,
  });
  const previousTimestamp = channel.session?.readSessionUpdatedAt?.({
    storePath,
    sessionKey: route.sessionKey,
  });
  const envelopeOptions = channel.reply?.resolveEnvelopeFormatOptions?.(params.cfg);
  const body = channel.reply?.formatAgentEnvelope
    ? channel.reply.formatAgentEnvelope({
        channel: "WeCom KF",
        from: fromLabel,
        previousTimestamp: previousTimestamp ?? undefined,
        envelope: envelopeOptions,
        body: rawText,
      })
    : rawText;

  const ctxPayload = (channel.reply?.finalizeInboundContext?.({
    Body: body,
    RawBody: rawText,
    CommandBody: rawText,
    From: from,
    To: to,
    SessionKey: route.sessionKey,
    AccountId: route.accountId ?? params.account.accountId,
    ChatType: "direct",
    ConversationLabel: fromLabel,
    SenderName: senderId,
    SenderId: senderId,
    Provider: "wecom-kf",
    Surface: "wecom-kf",
    MessageSid: params.msg.msgid,
    OriginatingChannel: "wecom-kf",
    OriginatingTo: to,
  }) as Record<string, unknown> | undefined) ?? {
    Body: body,
    RawBody: rawText,
    CommandBody: rawText,
    From: from,
    To: to,
    SessionKey: route.sessionKey,
    AccountId: route.accountId ?? params.account.accountId,
    ChatType: "direct",
    ConversationLabel: fromLabel,
    SenderName: senderId,
    SenderId: senderId,
    Provider: "wecom-kf",
    Surface: "wecom-kf",
    MessageSid: params.msg.msgid,
    OriginatingChannel: "wecom-kf",
    OriginatingTo: to,
  };

  if (channel.session?.recordInboundSession && storePath) {
    await channel.session.recordInboundSession({
      storePath,
      sessionKey: String(ctxPayload.SessionKey ?? route.sessionKey),
      ctx: ctxPayload,
      updateLastRoute: {
        sessionKey: String((route.mainSessionKey ?? route.sessionKey) || route.sessionKey),
        channel: "wecom-kf",
        to,
        accountId: route.accountId ?? params.account.accountId,
      },
      onRecordError: (error) => {
        logger.error(`recordInboundSession failed: ${String(error)}`);
      },
    });
  }

  const convertTables =
    channel.text?.convertMarkdownTables && channel.text?.resolveMarkdownTableMode
      ? (text: string) =>
          channel.text!.convertMarkdownTables!(
            text,
            channel.text!.resolveMarkdownTableMode!({
              cfg: params.cfg,
              channel: "wecom-kf",
              accountId: params.account.accountId,
            })
          )
      : (text: string) => text;

  const responseChunks: string[] = [];

  await dispatchReply({
    ctx: ctxPayload,
    cfg: params.cfg,
    dispatcherOptions: {
      deliver: async (payload) => {
        const text = String(payload.text ?? "").trim();
        if (!text) return;
        responseChunks.push(convertTables(text));
      },
      onError: (error, info) => {
        logger.error(`${info.kind} reply failed: ${String(error)}`);
      },
    },
  });

  const combined = responseChunks.join("\n\n").trim();
  if (!combined) {
    return;
  }

  try {
    const results = await sendKfTextMessage({
      account: params.account,
      externalUserId: senderId,
      text: combined,
      openKfId: params.msg.open_kfid,
    });
    const summary = summarizeSendResults(results);
    if (!summary.ok) {
      await updateAccountState(params.account.accountId, {
        lastError: summary.error ?? "send reply failed",
      });
      logger.error(`reply send failed: ${summary.error ?? "unknown error"}`);
      return;
    }

    await updateAccountState(params.account.accountId, {
      lastOutboundAt: Date.now(),
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await updateAccountState(params.account.accountId, { lastError: message });
    logger.error(`reply send failed: ${message}`);
  }
}
