// @ts-nocheck
import { randomUUID } from "node:crypto";
import { WebSocket, WebSocketServer } from "ws";

import { createLogger } from "./logger.js";
import { type BridgeFrame, safeJsonParse } from "./protocol.js";
import { buildSessionKey, normalizeAccountId } from "./session_keys.js";

type PendingTurn = {
  eventId: string;
  resolve: (value: { queuedFinal: boolean; counts: { final: number } }) => void;
  reject: (error: Error) => void;
  deliver: (payload: unknown, info?: { kind?: string }) => Promise<void> | void;
  onError?: (err: unknown, info: { kind: string }) => void;
  counts: { final: number };
};

type LateDeliverRoute = {
  deliver: (payload: unknown, info?: { kind?: string }) => Promise<void> | void;
  onError?: (err: unknown, info: { kind: string }) => void;
};

type RuntimeBridgeOptions = {
  host: string;
  port: number;
  token: string;
  version: string;
  channelsConfig: Record<string, any>;
};

function normalizeInboundAttachments(value: unknown): Array<Record<string, unknown>> {
  const items: Array<Record<string, unknown>> = [];
  for (const raw of Array.isArray(value) ? value : []) {
    if (!raw || typeof raw !== "object") continue;
    const entry = raw as Record<string, unknown>;
    const next: Record<string, unknown> = {};
    for (const key of ["kind", "url", "path", "mime_type", "file_name", "size_bytes"]) {
      const field = entry[key];
      if (field === undefined || field === null || field === "") {
        continue;
      }
      next[key] = field;
    }
    if (Object.keys(next).length === 0) {
      continue;
    }
    items.push(next);
  }
  return items;
}

function splitText(text: string, limit: number): string[] {
  const source = String(text || "");
  if (!source || source.length <= limit) return [source];
  const chunks: string[] = [];
  let remaining = source;
  while (remaining.length > limit) {
    let cut = remaining.lastIndexOf("\n", limit);
    if (cut <= 0) cut = remaining.lastIndexOf(" ", limit);
    if (cut <= 0) cut = limit;
    chunks.push(remaining.slice(0, cut).trim());
    remaining = remaining.slice(cut).trim();
  }
  if (remaining) chunks.push(remaining);
  return chunks.filter(Boolean);
}

export class G3kuRuntimeBridge {
  private logger = createLogger("runtime");
  private wss: WebSocketServer | null = null;
  private client: WebSocket | null = null;
  private pending = new Map<string, PendingTurn>();
  private lateDeliverRoutes = new Map<string, LateDeliverRoute>();
  private readonly channelsConfig: Record<string, any>;
  readonly runtime: Record<string, unknown>;
  readonly channelRuntime: Record<string, unknown>;

  constructor(private opts: RuntimeBridgeOptions) {
    this.channelsConfig = opts.channelsConfig;
    this.channelRuntime = this.createChannelRuntime();
    this.runtime = {
      log: (msg: string) => this.logger.info(msg),
      error: (msg: string) => this.logger.error(msg),
      channel: this.channelRuntime,
    };
  }

  async start(): Promise<void> {
    if (this.wss) return;
    this.wss = new WebSocketServer({ host: this.opts.host, port: this.opts.port });
    this.wss.on("connection", (ws) => this.handleConnection(ws));
    this.logger.info(`control ws listening on ws://${this.opts.host}:${this.opts.port}`);
  }

  async stop(): Promise<void> {
    for (const pending of this.pending.values()) {
      pending.reject(new Error("runtime bridge stopped"));
    }
    this.pending.clear();
    if (this.client) {
      try {
        this.client.close();
      } catch {
        // ignore
      }
      this.client = null;
    }
    if (this.wss) {
      await new Promise<void>((resolve) => this.wss?.close(() => resolve()));
      this.wss = null;
    }
  }

  private handleConnection(ws: WebSocket): void {
    let authed = false;
    ws.on("message", async (buf) => {
      const frame = safeJsonParse(buf.toString());
      if (!frame) {
        ws.close();
        return;
      }
      if (!authed) {
        if (frame.type === "auth" && frame.token === this.opts.token) {
          authed = true;
          this.client = ws;
          ws.send(JSON.stringify({ type: "auth_ok", server: "china_channels_host", version: this.opts.version }));
          return;
        }
        ws.close();
        return;
      }
      await this.handleFrame(frame);
    });
    ws.on("close", () => {
      if (this.client === ws) {
        this.client = null;
      }
    });
  }

  private async handleFrame(frame: BridgeFrame): Promise<void> {
    if (frame.type === "deliver_message") {
      const mode = String(frame.payload?.mode || "progress");
      if (mode !== "final") return;
      const pending = this.pending.get(frame.event_id);
      if (!pending) {
        const lateRoute = this.resolveLateDeliverRoute(frame);
        if (!lateRoute) return;
        try {
          await lateRoute.deliver(
            {
              text: frame.payload?.text,
              mediaUrls: undefined,
              mediaUrl: undefined,
            },
            { kind: "final" },
          );
        } catch (err) {
          lateRoute.onError?.(err, { kind: "final" });
        }
        return;
      }
      const info = { kind: "final" as const };
      pending.counts.final += 1;
      try {
        await pending.deliver({
          text: frame.payload?.text,
          mediaUrls: undefined,
          mediaUrl: undefined,
        }, info);
      } catch (err) {
        pending.onError?.(err, { kind: info.kind });
      }
      return;
    }
    if (frame.type === "turn_complete") {
      const pending = this.pending.get(frame.event_id);
      if (!pending) return;
      this.pending.delete(frame.event_id);
      pending.resolve({ queuedFinal: pending.counts.final > 0, counts: pending.counts });
      return;
    }
    if (frame.type === "turn_error") {
      const pending = this.pending.get(frame.event_id);
      if (!pending) return;
      this.pending.delete(frame.event_id);
      pending.reject(new Error(String(frame.error || "unknown error")));
    }
  }

  private send(frame: Record<string, unknown>): void {
    if (!this.client || this.client.readyState !== WebSocket.OPEN) {
      throw new Error("python bridge client not connected");
    }
    this.client.send(JSON.stringify(frame));
  }

  private createChannelRuntime(): Record<string, unknown> {
    return {
      routing: {
        resolveAgentRoute: (params: { channel: string; accountId?: string; peer: { kind: string; id: string } }) => ({
          sessionKey: buildSessionKey({
            channel: params.channel,
            accountId: params.accountId,
            peer: params.peer,
          }),
          accountId: normalizeAccountId(params.accountId),
        }),
      },
      reply: {
        resolveHumanDelayConfig: () => undefined,
        dispatchReplyWithDispatcher: async (params: any) => this.dispatchTurn(params),
        dispatchReplyWithBufferedBlockDispatcher: async (params: any) => this.dispatchTurn(params),
        createReplyDispatcher: (params: any) => params,
        createReplyDispatcherWithTyping: (params: any) => ({ dispatcher: params, replyOptions: {}, markDispatchIdle: () => undefined }),
        dispatchReplyFromConfig: async (params: any) =>
          this.dispatchTurn({
            ctx: params.ctx,
            cfg: params.cfg,
            dispatcherOptions: params.dispatcher,
            replyOptions: params.replyOptions,
          }),
      },
      session: {
        resolveStorePath: () => undefined,
        readSessionUpdatedAt: () => null,
        recordSessionMetaFromInbound: async () => undefined,
        updateLastRoute: async () => undefined,
        recordInboundSession: async () => undefined,
      },
      text: {
        resolveTextChunkLimit: (params: { cfg?: any; channel: string; defaultLimit?: number }) => {
          const perChannel = this.channelsConfig[params.channel] ?? {};
          const raw = Number(perChannel.textChunkLimit ?? params.defaultLimit ?? 1800);
          return Number.isFinite(raw) && raw > 0 ? raw : 1800;
        },
        resolveChunkMode: () => "length",
        resolveMarkdownTableMode: () => undefined,
        convertMarkdownTables: (text: string) => text,
        chunkMarkdownText: (text: string, limit: number) => splitText(text, limit),
        chunkTextWithMode: (text: string, limit: number) => splitText(text, limit),
      },
    };
  }

  private async dispatchTurn(params: {
    ctx: Record<string, any>;
    cfg?: unknown;
    dispatcherOptions?: {
      deliver?: (payload: unknown, info?: { kind?: string }) => Promise<void> | void;
      onError?: (err: unknown, info: { kind: string }) => void;
    };
  }): Promise<{ queuedFinal: boolean; counts: { final: number } }> {
    const deliver = params.dispatcherOptions?.deliver;
    if (!deliver) {
      throw new Error("dispatch requires a deliver callback")
    }
    const ctx = params.ctx ?? {};
    const eventId = randomUUID();
    const channel = String(ctx.OriginatingChannel || ctx.channel || "").trim();
    const accountId = String(ctx.AccountId || ctx.accountId || "default").trim() || "default";
    const to = String(ctx.OriginatingTo || ctx.To || "").trim();
    const threadId = String(ctx.ThreadId || ctx.threadId || "").trim() || undefined;
    const peer = to.startsWith("user:")
      ? { kind: "user", id: to.slice(5) }
      : to.startsWith("chat:") || to.startsWith("group:")
        ? { kind: "group", id: to.split(":", 2)[1] || to }
        : { kind: ctx.ChatType === "group" ? "group" : "user", id: to || String(ctx.From || "unknown") };
    const text = String(ctx.BodyForAgent || ctx.Body || ctx.RawBody || "");
    const messageId = String(ctx.MessageSid || ctx.messageId || "").trim() || undefined;
    const attachments = normalizeInboundAttachments(ctx.AgentAttachments);
    const pending = new Promise<{ queuedFinal: boolean; counts: { final: number } }>((resolve, reject) => {
      this.pending.set(eventId, {
        eventId,
        resolve,
        reject,
        deliver,
        onError: params.dispatcherOptions?.onError,
        counts: { final: 0 },
      });
    });
    this.rememberLateDeliverRoute({
      ctx,
      channel,
      accountId,
      peer,
      threadId,
      deliver,
      onError: params.dispatcherOptions?.onError,
    });
    this.send({
      type: "inbound_message",
      event_id: eventId,
      channel,
      account_id: accountId,
      peer,
      ...(threadId ? { thread_id: threadId } : {}),
      message: {
        id: messageId,
        text,
        attachments,
      },
      metadata: {
        platform_ctx: ctx,
        reply_to: messageId,
        account_id: accountId,
      },
    });
    return pending;
  }

  private rememberLateDeliverRoute(params: {
    ctx: Record<string, any>;
    channel: string;
    accountId: string;
    peer: { kind: string; id: string };
    threadId?: string;
    deliver: (payload: unknown, info?: { kind?: string }) => Promise<void> | void;
    onError?: (err: unknown, info: { kind: string }) => void;
  }): void {
    const keys = this.collectSessionKeys(params);
    if (keys.length === 0) return;
    const route: LateDeliverRoute = {
      deliver: params.deliver,
      onError: params.onError,
    };
    for (const key of keys) {
      this.lateDeliverRoutes.set(key, route);
    }
  }

  private resolveLateDeliverRoute(frame: Extract<BridgeFrame, { type: "deliver_message" }>): LateDeliverRoute | null {
    const metadata = frame.metadata && typeof frame.metadata === "object" ? frame.metadata : {};
    const metadataSessionKey = String((metadata as Record<string, unknown>).session_key || "").trim();
    if (metadataSessionKey) {
      const direct = this.lateDeliverRoutes.get(metadataSessionKey);
      if (direct) return direct;
    }
    const canonicalKey = this.buildCanonicalSessionKey({
      channel: String(frame.channel || "").trim(),
      accountId: String(frame.account_id || "default").trim() || "default",
      peer: {
        kind: String(frame.target?.kind || "user").trim() || "user",
        id: String(frame.target?.id || "").trim(),
      },
      threadId: String((metadata as Record<string, unknown>).thread_id || (metadata as Record<string, unknown>)._china_thread_id || "").trim() || undefined,
    });
    if (!canonicalKey) return null;
    return this.lateDeliverRoutes.get(canonicalKey) ?? null;
  }

  private collectSessionKeys(params: {
    ctx: Record<string, any>;
    channel: string;
    accountId: string;
    peer: { kind: string; id: string };
    threadId?: string;
  }): string[] {
    const keys = new Set<string>();
    const sessionKey = String(params.ctx.SessionKey || params.ctx.sessionKey || "").trim();
    if (sessionKey) keys.add(sessionKey);
    const mainSessionKey = String(params.ctx.MainSessionKey || params.ctx.mainSessionKey || "").trim();
    if (mainSessionKey) keys.add(mainSessionKey);
    const canonicalKey = this.buildCanonicalSessionKey({
      channel: params.channel,
      accountId: params.accountId,
      peer: params.peer,
      threadId: params.threadId,
    });
    if (canonicalKey) keys.add(canonicalKey);
    return [...keys];
  }

  private buildCanonicalSessionKey(params: {
    channel: string;
    accountId: string;
    peer: { kind: string; id: string };
    threadId?: string;
  }): string {
    const channel = String(params.channel || "").trim();
    const accountId = String(params.accountId || "default").trim() || "default";
    const rawKind = String(params.peer?.kind || "user").trim().toLowerCase();
    const isGroup = rawKind === "group" || rawKind === "chat" || rawKind === "channel";
    const threadSuffix = params.threadId ? `:thread:${params.threadId}` : "";
    if (!channel) return "";
    if (!isGroup) {
      return `china:${channel}:${accountId}:dm${threadSuffix}`;
    }
    const peerId = String(params.peer?.id || "").trim();
    if (!peerId) return "";
    return `china:${channel}:${accountId}:group:${peerId}${threadSuffix}`;
  }
}
