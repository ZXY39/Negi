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
import {
  clearWecomWsReplyContextsForAccount,
  finishWecomWsMessageContext,
  registerWecomWsMessageContext,
} from "./ws-reply-context.js";
import {
  startWecomWsGateway,
  stopWecomWsGatewayForAccount,
} from "./ws-gateway.js";

async function waitFor(condition: () => boolean, timeoutMs: number = 1_000): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (condition()) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error("Timed out waiting for condition");
}

describe("wecom channel ws native media", () => {
  afterEach(() => {
    clearWecomWsReplyContextsForAccount("default");
    stopWecomWsGatewayForAccount("default");
  });

  it("replies with native ws file media for active contexts", async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "wecom-native-file-"));
    const filePath = path.join(tempDir, "reply.txt");
    await fs.writeFile(filePath, "hello file");

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
            body: { upload_id: "upload-file-1" },
          }));
          return;
        }
        if (frame.cmd === "aibot_upload_media_finish") {
          socket.send(JSON.stringify({
            cmd: frame.cmd,
            headers: { req_id: reqId },
            errcode: 0,
            body: { media_id: "media-file-1", created_at: 123 },
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

    const sent: unknown[] = [];
    registerWecomWsMessageContext({
      accountId: "default",
      reqId: "req-native-file",
      to: "user:alice",
      streamId: "stream-native-file",
      send: async (frame) => {
        sent.push(frame);
      },
    });

    await waitFor(() => server.clients.size === 1);

    const result = await wecomPlugin.outbound.sendMedia({
      cfg,
      to: "user:alice",
      mediaUrl: filePath,
      text: "see attachment",
    });

    expect(result.ok).toBe(true);
    await finishWecomWsMessageContext({
      accountId: "default",
      reqId: "req-native-file",
    });

    expect(sent).toHaveLength(3);
    expect(sent[0]).toMatchObject({
      body: {
        stream: {
          id: "stream-native-file",
          finish: false,
          content: "see attachment",
        },
      },
    });
    expect(sent[1]).toMatchObject({
      body: {
        msgtype: "file",
        file: {
          media_id: "media-file-1",
        },
      },
    });
    expect(sent[2]).toMatchObject({
      body: {
        stream: {
          id: "stream-native-file",
          finish: true,
          content: "see attachment",
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

  it("proactively sends native ws file media after the conversation is activated", async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "wecom-proactive-file-"));
    const filePath = path.join(tempDir, "reply.txt");
    await fs.writeFile(filePath, "hello proactive file");

    const received: Array<{ cmd?: string; body?: unknown }> = [];
    const server = new WebSocketServer({ port: 0 });
    await once(server, "listening");
    server.on("connection", (socket) => {
      socket.on("message", (raw) => {
        const frame = JSON.parse(raw.toString()) as {
          cmd?: string;
          headers?: { req_id?: string };
          body?: unknown;
        };
        received.push({ cmd: frame.cmd, body: frame.body });
        const reqId = frame.headers?.req_id;
        if (!reqId) return;
        if (frame.cmd === "aibot_upload_media_init") {
          socket.send(JSON.stringify({
            cmd: frame.cmd,
            headers: { req_id: reqId },
            errcode: 0,
            body: { upload_id: "upload-file-2" },
          }));
          return;
        }
        if (frame.cmd === "aibot_upload_media_finish") {
          socket.send(JSON.stringify({
            cmd: frame.cmd,
            headers: { req_id: reqId },
            errcode: 0,
            body: { media_id: "media-file-2", created_at: 456 },
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
    const client = [...server.clients][0];
    client?.send(
      JSON.stringify({
        cmd: "aibot_msg_callback",
        headers: {
          req_id: "req-callback-file-1",
        },
        body: {
          msgid: "msg-file-1",
          chattype: "single",
          from: { userid: "alice" },
          msgtype: "text",
          text: { content: "hello" },
        },
      })
    );
    // The callback is sent from the test server to the gateway client, so it never
    // appears in the server-side `received` queue. Give the client loop one turn
    // to record the activated target before exercising proactive media send.
    await new Promise((resolve) => setTimeout(resolve, 50));

    const result = await wecomPlugin.outbound.sendMedia({
      cfg,
      to: "user:alice",
      mediaUrl: filePath,
    });

    expect(result.ok).toBe(true);
    await waitFor(() =>
      received.some((frame) => frame.cmd === "aibot_send_msg" && JSON.stringify(frame.body).includes("\"msgtype\":\"file\""))
    );
    expect(received.find((frame) => frame.cmd === "aibot_send_msg" && JSON.stringify(frame.body).includes("\"msgtype\":\"file\""))?.body).toEqual({
      chatid: "alice",
      msgtype: "file",
      file: {
        media_id: "media-file-2",
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

  it("replies with native ws voice media for active contexts", async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "wecom-native-voice-"));
    const filePath = path.join(tempDir, "reply.amr");
    await fs.writeFile(filePath, "hello voice");

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
            body: { upload_id: "upload-voice-1" },
          }));
          return;
        }
        if (frame.cmd === "aibot_upload_media_finish") {
          socket.send(JSON.stringify({
            cmd: frame.cmd,
            headers: { req_id: reqId },
            errcode: 0,
            body: { media_id: "media-voice-1", created_at: 789 },
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

    const sent: unknown[] = [];
    registerWecomWsMessageContext({
      accountId: "default",
      reqId: "req-native-voice",
      to: "user:alice",
      streamId: "stream-native-voice",
      send: async (frame) => {
        sent.push(frame);
      },
    });

    await waitFor(() => server.clients.size === 1);

    const result = await wecomPlugin.outbound.sendMedia({
      cfg,
      to: "user:alice",
      mediaUrl: filePath,
      mimeType: "audio/amr",
      text: "voice reply",
    });

    expect(result.ok).toBe(true);
    await finishWecomWsMessageContext({
      accountId: "default",
      reqId: "req-native-voice",
    });

    expect(sent).toHaveLength(3);
    expect(sent[0]).toMatchObject({
      body: {
        stream: {
          id: "stream-native-voice",
          finish: false,
          content: "voice reply",
        },
      },
    });
    expect(sent[1]).toMatchObject({
      body: {
        msgtype: "voice",
        voice: {
          media_id: "media-voice-1",
        },
      },
    });
    expect(sent[2]).toMatchObject({
      body: {
        stream: {
          id: "stream-native-voice",
          finish: true,
          content: "voice reply",
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

  it("proactively sends native ws video media after the conversation is activated", async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "wecom-proactive-video-"));
    const filePath = path.join(tempDir, "reply.mp4");
    await fs.writeFile(filePath, "hello proactive video");

    const received: Array<{ cmd?: string; body?: unknown }> = [];
    const server = new WebSocketServer({ port: 0 });
    await once(server, "listening");
    server.on("connection", (socket) => {
      socket.on("message", (raw) => {
        const frame = JSON.parse(raw.toString()) as {
          cmd?: string;
          headers?: { req_id?: string };
          body?: unknown;
        };
        received.push({ cmd: frame.cmd, body: frame.body });
        const reqId = frame.headers?.req_id;
        if (!reqId) return;
        if (frame.cmd === "aibot_upload_media_init") {
          socket.send(JSON.stringify({
            cmd: frame.cmd,
            headers: { req_id: reqId },
            errcode: 0,
            body: { upload_id: "upload-video-1" },
          }));
          return;
        }
        if (frame.cmd === "aibot_upload_media_finish") {
          socket.send(JSON.stringify({
            cmd: frame.cmd,
            headers: { req_id: reqId },
            errcode: 0,
            body: { media_id: "media-video-1", created_at: 987 },
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
    const client = [...server.clients][0];
    client?.send(
      JSON.stringify({
        cmd: "aibot_msg_callback",
        headers: {
          req_id: "req-callback-video-1",
        },
        body: {
          msgid: "msg-video-1",
          chattype: "single",
          from: { userid: "alice" },
          msgtype: "text",
          text: { content: "hello" },
        },
      })
    );
    await new Promise((resolve) => setTimeout(resolve, 50));

    const result = await wecomPlugin.outbound.sendMedia({
      cfg,
      to: "user:alice",
      mediaUrl: filePath,
      mimeType: "video/mp4",
    });

    expect(result.ok).toBe(true);
    await waitFor(() =>
      received.some((frame) => frame.cmd === "aibot_send_msg" && JSON.stringify(frame.body).includes("\"msgtype\":\"video\""))
    );
    expect(received.find((frame) => frame.cmd === "aibot_send_msg" && JSON.stringify(frame.body).includes("\"msgtype\":\"video\""))?.body).toEqual({
      chatid: "alice",
      msgtype: "video",
      video: {
        media_id: "media-video-1",
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
});
