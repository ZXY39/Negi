import os from "node:os";
import path from "node:path";
import { promises as fs } from "node:fs";
import { once } from "node:events";
import type { AddressInfo } from "node:net";

import { afterEach, describe, expect, it, vi } from "vitest";
import { WebSocketServer } from "ws";

vi.mock("@wecom/aibot-node-sdk", async () => await import("./test-sdk-mock.js"));

import { wecomPlugin } from "./channel.js";
import type { PluginConfig } from "./config.js";
import { clearOutboundReplyState, setAccountPublicBaseUrl } from "./outbound-reply.js";
import {
  clearWecomWsReplyContextsForAccount,
  finishWecomWsMessageContext,
  registerWecomWsMessageContext,
  registerWecomWsPendingAutoImagePaths,
} from "./ws-reply-context.js";
import { startWecomWsGateway, stopWecomWsGatewayForAccount } from "./ws-gateway.js";

const ONE_BY_ONE_PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9pG8L1cAAAAASUVORK5CYII=";

async function waitFor(condition: () => boolean, timeoutMs: number = 1_000): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (condition()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("Timed out waiting for condition");
}

describe("wecom channel ws native image reply", () => {
  afterEach(() => {
    clearWecomWsReplyContextsForAccount("default");
    clearOutboundReplyState();
    stopWecomWsGatewayForAccount("default");
  });

  it("sends local png through native ws media reply without requiring a public url", async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "wecom-native-image-"));
    const imagePath = path.join(tempDir, "reply.png");
    await fs.writeFile(imagePath, Buffer.from(ONE_BY_ONE_PNG_BASE64, "base64"));

    const server = new WebSocketServer({ port: 0 });
    await once(server, "listening");
    server.on("connection", (socket) => {
      socket.on("message", (raw) => {
        const frame = JSON.parse(raw.toString()) as {
          cmd?: string;
          headers?: { req_id?: string };
        };
        const reqId = frame.headers?.req_id;
        if (!reqId) return;
        if (frame.cmd === "aibot_upload_media_init") {
          socket.send(JSON.stringify({
            cmd: frame.cmd,
            headers: { req_id: reqId },
            errcode: 0,
            body: { upload_id: "upload-image-1" },
          }));
          return;
        }
        if (frame.cmd === "aibot_upload_media_finish") {
          socket.send(JSON.stringify({
            cmd: frame.cmd,
            headers: { req_id: reqId },
            errcode: 0,
            body: { media_id: "media-image-1", created_at: 123 },
          }));
          return;
        }
        socket.send(JSON.stringify({
          cmd: frame.cmd,
          headers: { req_id: reqId },
          errcode: 0,
        }));
      });
    });

    const sent: unknown[] = [];
    registerWecomWsMessageContext({
      accountId: "default",
      reqId: "req-native-image",
      to: "user:alice",
      streamId: "stream-native-image",
      send: async (frame) => {
        sent.push(frame);
      },
    });

    const { port } = server.address() as AddressInfo;
    const cfg: PluginConfig = {
      channels: {
        wecom: {
          mode: "ws",
          botId: "bot-1",
          secret: "secret-1",
          wsUrl: `ws://127.0.0.1:${port}`,
          heartbeatIntervalMs: 20,
        },
      },
    };
    const controller = new AbortController();
    const gatewayPromise = startWecomWsGateway({
      cfg,
      account: {
        accountId: "default",
        name: "default",
        enabled: true,
        configured: true,
        mode: "ws",
        receiveId: "",
        botId: "bot-1",
        secret: "secret-1",
        wsUrl: `ws://127.0.0.1:${port}`,
        heartbeatIntervalMs: 20,
        reconnectInitialDelayMs: 1_000,
        reconnectMaxDelayMs: 30_000,
        wsImageReplyMode: "native",
        config: {
          mode: "ws",
          botId: "bot-1",
          secret: "secret-1",
          wsUrl: `ws://127.0.0.1:${port}`,
        },
      },
      abortSignal: controller.signal,
      runtime: {
        log: () => {},
        error: () => {},
      },
    });

    await waitFor(() => server.clients.size === 1);

    const result = await wecomPlugin.outbound.sendMedia({
      cfg,
      to: "user:alice",
      mediaUrl: imagePath,
      text: "caption",
    });

    expect(result.ok).toBe(true);
    await finishWecomWsMessageContext({
      accountId: "default",
      reqId: "req-native-image",
    });

    expect(sent).toHaveLength(3);
    expect(sent[0]).toMatchObject({
      body: {
        stream: {
          id: "stream-native-image",
          finish: false,
          content: "caption",
        },
      },
    });
    expect(sent[1]).toMatchObject({
      body: {
        msgtype: "image",
        image: {
          media_id: "media-image-1",
        },
      },
    });
    expect(sent[2]).toMatchObject({
      body: {
        stream: {
          id: "stream-native-image",
          finish: true,
          content: "caption",
        },
      },
    });

    controller.abort();
    await expect(gatewayPromise).resolves.toBeUndefined();
    await new Promise<void>((resolve, reject) => {
      server.close((err) => {
        if (err) reject(err);
        else resolve();
      });
    });
  });

  it("can force local ws image replies through markdown urls", async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "wecom-markdown-image-"));
    const imagePath = path.join(tempDir, "reply.png");
    await fs.writeFile(imagePath, Buffer.from(ONE_BY_ONE_PNG_BASE64, "base64"));

    const sent: unknown[] = [];
    registerWecomWsMessageContext({
      accountId: "default",
      reqId: "req-markdown-image",
      to: "user:alice",
      streamId: "stream-markdown-image",
      send: async (frame) => {
        sent.push(frame);
      },
    });
    setAccountPublicBaseUrl("default", "https://example.test");

    const cfg: PluginConfig = {
      channels: {
        wecom: {
          mode: "ws",
          botId: "bot-1",
          secret: "secret-1",
          wsImageReplyMode: "markdown-url",
        },
      },
    };

    const result = await wecomPlugin.outbound.sendMedia({
      cfg,
      to: "user:alice",
      mediaUrl: imagePath,
      text: "caption",
    });

    expect(result.ok).toBe(true);
    await finishWecomWsMessageContext({
      accountId: "default",
      reqId: "req-markdown-image",
    });

    expect(sent).toHaveLength(2);
    expect(sent[0]).toMatchObject({
      body: {
        stream: {
          id: "stream-markdown-image",
          finish: false,
        },
      },
    });
    expect(JSON.stringify(sent[0])).toContain("caption");
    expect(JSON.stringify(sent[0])).toContain("![](https://example.test/wecom-media/");
    expect(sent[1]).toMatchObject({
      body: {
        stream: {
          id: "stream-markdown-image",
          finish: true,
        },
      },
    });
    expect(JSON.stringify(sent[1])).not.toContain("\"msg_item\"");
  });

  it("auto-attaches pending inbound images when the agent only sends text", async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "wecom-auto-image-"));
    const imagePath = path.join(tempDir, "reply.png");
    await fs.writeFile(imagePath, Buffer.from(ONE_BY_ONE_PNG_BASE64, "base64"));

    const sent: unknown[] = [];
    registerWecomWsMessageContext({
      accountId: "default",
      reqId: "req-auto-image",
      to: "user:alice",
      streamId: "stream-auto-image",
      send: async (frame) => {
        sent.push(frame);
      },
    });
    registerWecomWsPendingAutoImagePaths({
      accountId: "default",
      to: "user:alice",
      imagePaths: [imagePath],
    });

    const cfg: PluginConfig = {
      channels: {
        wecom: {
          mode: "ws",
          botId: "bot-1",
          secret: "secret-1",
        },
      },
    };

    const result = await wecomPlugin.outbound.sendText({
      cfg,
      to: "user:alice",
      text: "caption",
    });

    expect(result.ok).toBe(true);
    await finishWecomWsMessageContext({
      accountId: "default",
      reqId: "req-auto-image",
    });

    expect(sent).toHaveLength(2);
    expect(sent[0]).toMatchObject({
      body: {
        stream: {
          id: "stream-auto-image",
          finish: false,
          content: "caption",
        },
      },
    });
    expect(sent[1]).toMatchObject({
      body: {
        stream: {
          id: "stream-auto-image",
          finish: true,
          content: "caption",
          msg_item: [
            {
              msgtype: "image",
              image: {
                base64: expect.any(String),
                md5: expect.any(String),
              },
            },
          ],
        },
      },
    });
  });
});
