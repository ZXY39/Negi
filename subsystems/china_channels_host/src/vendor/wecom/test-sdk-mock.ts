import { EventEmitter } from "node:events";
import crypto from "node:crypto";
import { createHash } from "node:crypto";

import WebSocket from "ws";

type WsFrame = {
  cmd?: string;
  headers?: {
    req_id?: string;
    [key: string]: unknown;
  };
  body?: unknown;
  errcode?: number;
  errmsg?: string;
};

type PendingResolver = {
  resolve: (frame: WsFrame) => void;
  reject: (err: Error) => void;
};

let mockDisconnectErrorMessage: string | null = null;

export function setMockDisconnectErrorMessage(message: string | null): void {
  mockDisconnectErrorMessage = message?.trim() ? message : null;
}

export function resetMockSdkBehavior(): void {
  mockDisconnectErrorMessage = null;
}

export class WSClient extends EventEmitter {
  private readonly botId: string;
  private readonly secret: string;
  private readonly wsUrl: string;
  private readonly heartbeatInterval: number;
  private socket: WebSocket | null = null;
  private heartbeatTimer: NodeJS.Timeout | null = null;
  private readonly pending = new Map<string, PendingResolver>();

  constructor(options: {
    botId?: string;
    secret?: string;
    wsUrl?: string;
    heartbeatInterval?: number;
  }) {
    super();
    this.botId = String(options.botId ?? "");
    this.secret = String(options.secret ?? "");
    this.wsUrl = String(options.wsUrl ?? "");
    this.heartbeatInterval = Number(options.heartbeatInterval ?? 30_000);
  }

  get isConnected(): boolean {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  connect(): this {
    this.socket = new WebSocket(this.wsUrl);

    this.socket.on("open", () => {
      this.emit("connected");
      void this.sendFrame({
        cmd: "aibot_subscribe",
        headers: {
          req_id: crypto.randomUUID(),
        },
        body: {
          bot_id: this.botId,
          secret: this.secret,
        },
      }).then(() => {
        this.emit("authenticated");
      }).catch((err) => {
        this.emit("error", err);
      });

      if (this.heartbeatInterval > 0) {
        this.heartbeatTimer = setInterval(() => {
          void this.sendFrame({
            cmd: "ping",
            headers: {
              req_id: crypto.randomUUID(),
            },
          }).catch(() => undefined);
        }, this.heartbeatInterval);
      }
    });

    this.socket.on("message", (raw) => {
      const frame = JSON.parse(raw.toString()) as WsFrame;
      const cmd = String(frame.cmd ?? "").trim();
      if (cmd === "aibot_msg_callback") {
        this.emit("message", frame);
        return;
      }
      if (cmd === "aibot_event_callback") {
        this.emit("event", frame);
        return;
      }

      const reqId = String(frame.headers?.req_id ?? "").trim();
      if (!reqId) return;
      const pending = this.pending.get(reqId);
      if (!pending) return;
      this.pending.delete(reqId);
      pending.resolve(frame);
    });

    this.socket.on("close", () => {
      this.clearHeartbeat();
      this.emit("disconnected", "closed");
    });

    this.socket.on("error", (err) => {
      this.emit("error", err instanceof Error ? err : new Error(String(err)));
    });

    return this;
  }

  disconnect(): void {
    this.clearHeartbeat();
    if (mockDisconnectErrorMessage) {
      queueMicrotask(() => {
        this.emit("error", new Error(mockDisconnectErrorMessage ?? "mock disconnect error"));
      });
    }
    this.socket?.close();
    this.socket = null;
  }

  async reply(frame: { headers?: { req_id?: string } }, body: Record<string, unknown>, cmd = "aibot_respond_msg"): Promise<WsFrame> {
    const reqId = String(frame.headers?.req_id ?? "").trim();
    if (!reqId) {
      throw new Error("mock WSClient.reply requires req_id");
    }
    return this.sendFrame({
      cmd,
      headers: {
        req_id: reqId,
      },
      body,
    });
  }

  async replyWelcome(frame: { headers?: { req_id?: string } }, body: Record<string, unknown>): Promise<WsFrame> {
    return this.reply(frame, body, "aibot_respond_welcome_msg");
  }

  async sendMessage(chatid: string, body: Record<string, unknown>): Promise<WsFrame> {
    return this.sendFrame({
      cmd: "aibot_send_msg",
      headers: {
        req_id: crypto.randomUUID(),
      },
      body: {
        chatid,
        ...body,
      },
    });
  }

  async replyMedia(
    frame: { headers?: { req_id?: string } },
    mediaType: "file" | "image" | "voice" | "video",
    mediaId: string
  ): Promise<WsFrame> {
    return this.reply(frame, {
      msgtype: mediaType,
      [mediaType]: {
        media_id: mediaId,
      },
    });
  }

  async sendMediaMessage(
    chatid: string,
    mediaType: "file" | "image" | "voice" | "video",
    mediaId: string
  ): Promise<WsFrame> {
    return this.sendMessage(chatid, {
      msgtype: mediaType,
      [mediaType]: {
        media_id: mediaId,
      },
    });
  }

  async uploadMedia(
    fileBuffer: Buffer,
    options: { type: "file" | "image" | "voice" | "video"; filename: string }
  ): Promise<{ type: string; media_id: string; created_at: number }> {
    const md5 = createHash("md5").update(fileBuffer).digest("hex");
    const initFrame = await this.sendFrame({
      cmd: "aibot_upload_media_init",
      headers: {
        req_id: crypto.randomUUID(),
      },
      body: {
        type: options.type,
        filename: options.filename,
        total_size: fileBuffer.length,
        total_chunks: 1,
        md5,
      },
    });
    const uploadId =
      String((initFrame.body as { upload_id?: string } | undefined)?.upload_id ?? "").trim() ||
      `upload-${crypto.randomUUID()}`;
    await this.sendFrame({
      cmd: "aibot_upload_media_chunk",
      headers: {
        req_id: crypto.randomUUID(),
      },
      body: {
        upload_id: uploadId,
        chunk_index: 0,
        total_chunks: 1,
        data: fileBuffer.toString("base64"),
      },
    });
    const finishFrame = await this.sendFrame({
      cmd: "aibot_upload_media_finish",
      headers: {
        req_id: crypto.randomUUID(),
      },
      body: {
        upload_id: uploadId,
        md5,
      },
    });
    const body = (finishFrame.body as { media_id?: string; created_at?: number } | undefined) ?? {};
    return {
      type: options.type,
      media_id: String(body.media_id ?? "").trim() || `media-${crypto.randomUUID()}`,
      created_at: typeof body.created_at === "number" ? body.created_at : Date.now(),
    };
  }

  async downloadFile(url: string, _aesKey?: string): Promise<{ buffer: Buffer; filename?: string }> {
    let filename: string | undefined;
    try {
      const parsed = new URL(url);
      filename = parsed.pathname.split("/").filter(Boolean).pop();
    } catch {
      filename = undefined;
    }
    return {
      buffer: Buffer.from(`downloaded:${url}`, "utf8"),
      filename,
    };
  }

  private async sendFrame(frame: WsFrame): Promise<WsFrame> {
    const socket = this.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      throw new Error("mock WSClient socket is not connected");
    }

    const reqId = String(frame.headers?.req_id ?? "").trim();
    return new Promise<WsFrame>((resolve, reject) => {
      if (reqId) {
        this.pending.set(reqId, { resolve, reject });
      }

      socket.send(JSON.stringify(frame), (err) => {
        if (!err) return;
        if (reqId) {
          this.pending.delete(reqId);
        }
        reject(err instanceof Error ? err : new Error(String(err)));
      });
    });
  }

  private clearHeartbeat(): void {
    if (!this.heartbeatTimer) return;
    clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = null;
  }
}

const AiBot = {
  WSClient,
};

export default AiBot;
