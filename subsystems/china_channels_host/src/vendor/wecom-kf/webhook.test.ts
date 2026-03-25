import crypto from "node:crypto";
import os from "node:os";
import path from "node:path";
import { IncomingMessage, ServerResponse } from "node:http";
import { mkdtemp, rm } from "node:fs/promises";
import { Socket } from "node:net";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearAllAccessTokenCache } from "./api.js";
import { computeWecomMsgSignature } from "./crypto.js";
import { clearWecomKfRuntime, setWecomKfRuntime } from "./runtime.js";
import {
  flushWecomKfStateForTests,
  getStoredCursor,
  setWecomKfStateFilePathForTests,
} from "./state.js";
import type { PluginConfig, ResolvedWecomKfAccount, WebhookTarget } from "./types.js";
import {
  handleWecomKfWebhookRequest,
  primeWecomKfCursor,
  registerWecomKfWebhookTarget,
} from "./webhook.js";

const PKCS7_BLOCK_SIZE = 32;
const token = "callback-token";
const encodingAESKey = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG";
const corpId = "ww-test-corp";

function decodeEncodingAESKey(raw: string): Buffer {
  return Buffer.from(raw.endsWith("=") ? raw : `${raw}=`, "base64");
}

function pkcs7Pad(buffer: Buffer, blockSize: number): Buffer {
  const mod = buffer.length % blockSize;
  const pad = mod === 0 ? blockSize : blockSize - mod;
  return Buffer.concat([buffer, Buffer.alloc(pad, pad)]);
}

function encryptWecomPlaintext(params: {
  encodingAESKey: string;
  receiveId: string;
  plaintext: string;
}): string {
  const aesKey = decodeEncodingAESKey(params.encodingAESKey);
  const iv = aesKey.subarray(0, 16);
  const random16 = Buffer.from("1234567890abcdef", "utf8");
  const plaintext = Buffer.from(params.plaintext, "utf8");
  const msgLength = Buffer.alloc(4);
  msgLength.writeUInt32BE(plaintext.length, 0);
  const payload = pkcs7Pad(
    Buffer.concat([random16, msgLength, plaintext, Buffer.from(params.receiveId, "utf8")]),
    PKCS7_BLOCK_SIZE
  );
  const cipher = crypto.createCipheriv("aes-256-cbc", aesKey, iv);
  cipher.setAutoPadding(false);
  return Buffer.concat([cipher.update(payload), cipher.final()]).toString("base64");
}

function createMockRequest(params: {
  method: "GET" | "POST";
  url: string;
  body?: unknown;
}): IncomingMessage {
  const socket = new Socket();
  const req = new IncomingMessage(socket);
  req.method = params.method;
  req.url = params.url;
  if (params.method === "POST") {
    req.push(JSON.stringify(params.body ?? {}));
  }
  req.push(null);
  return req;
}

function createMockResponse(): ServerResponse & {
  _getData: () => string;
  _getStatusCode: () => number;
} {
  const req = new IncomingMessage(new Socket());
  const res = new ServerResponse(req);
  const mutableRes = res as unknown as {
    write: (...args: unknown[]) => boolean;
    end: (...args: unknown[]) => ServerResponse;
  };
  let data = "";
  mutableRes.write = (chunk?: unknown) => {
    data += String(chunk);
    return true;
  };
  mutableRes.end = (chunk?: unknown) => {
    if (chunk) data += String(chunk);
    return res;
  };
  return Object.assign(res, {
    _getData: () => data,
    _getStatusCode: () => res.statusCode,
  });
}

function createAccount(overrides?: Partial<ResolvedWecomKfAccount>): ResolvedWecomKfAccount {
  return {
    accountId: "default",
    enabled: true,
    configured: true,
    token,
    encodingAESKey,
    corpId,
    corpSecret: "kf-secret",
    openKfId: "wk-test",
    canSendActive: true,
    config: {
      webhookPath: "/wecom-kf",
      token,
      encodingAESKey,
      corpId,
      corpSecret: "kf-secret",
      openKfId: "wk-test",
      welcomeText: "你好，欢迎来到微信客服。",
    },
    ...overrides,
  };
}

function createTarget(params?: {
  account?: Partial<ResolvedWecomKfAccount>;
  cfg?: PluginConfig;
}): WebhookTarget {
  return {
    account: createAccount(params?.account),
    config: params?.cfg ?? {},
    runtime: {
      log: vi.fn(),
      error: vi.fn(),
    },
    path: params?.account?.config?.webhookPath ?? "/wecom-kf",
  };
}

function mockJsonResponse(payload: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: vi.fn().mockResolvedValue(payload),
  } as unknown as Response;
}

let tempDir = "";
let stateFilePath = "";

beforeEach(async () => {
  tempDir = await mkdtemp(path.join(os.tmpdir(), "wecom-kf-webhook-"));
  stateFilePath = path.join(tempDir, "state.json");
  setWecomKfStateFilePathForTests(stateFilePath);
});

afterEach(async () => {
  await flushWecomKfStateForTests();
  clearAllAccessTokenCache();
  clearWecomKfRuntime();
  setWecomKfStateFilePathForTests();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  if (tempDir) {
    await rm(tempDir, { recursive: true, force: true });
  }
  tempDir = "";
  stateFilePath = "";
});

describe("wecom-kf webhook", () => {
  it("handles GET webhook verification", async () => {
    const target = createTarget({
      account: {
        config: {
          webhookPath: "/wecom-kf-verify",
          token,
          encodingAESKey,
          corpId,
          corpSecret: "kf-secret",
          openKfId: "wk-test",
        },
      },
    });
    target.account = {
      ...target.account,
      config: {
        ...target.account.config,
        webhookPath: "/wecom-kf-verify",
      },
    };

    const unregister = registerWecomKfWebhookTarget({
      ...target,
      path: "/wecom-kf-verify",
    });

    try {
      const timestamp = "1710000000";
      const nonce = "nonce-verify";
      const echostr = encryptWecomPlaintext({
        encodingAESKey,
        receiveId: corpId,
        plaintext: "ping",
      });
      const signature = computeWecomMsgSignature({
        token,
        timestamp,
        nonce,
        encrypt: echostr,
      });
      const req = createMockRequest({
        method: "GET",
        url: `/wecom-kf-verify?msg_signature=${encodeURIComponent(signature)}&timestamp=${encodeURIComponent(timestamp)}&nonce=${encodeURIComponent(nonce)}&echostr=${encodeURIComponent(echostr)}`,
      });
      const res = createMockResponse();

      const handled = await handleWecomKfWebhookRequest(req, res);

      expect(handled).toBe(true);
      expect(res._getStatusCode()).toBe(200);
      expect(res._getData()).toBe("ping");
    } finally {
      unregister();
    }
  });

  it("paginates sync_msg, suppresses duplicates, and sends enter_session welcome messages", async () => {
    const dispatchReplyWithBufferedBlockDispatcher = vi.fn(async () => undefined);
    setWecomKfRuntime({
      channel: {
        routing: {
          resolveAgentRoute: () => ({
            sessionKey: "session-1",
            accountId: "default",
            agentId: "agent-1",
          }),
        },
        reply: {
          dispatchReplyWithBufferedBlockDispatcher,
        },
      },
    });

    let syncCallCount = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/cgi-bin/gettoken")) {
        return mockJsonResponse({
          errcode: 0,
          errmsg: "ok",
          access_token: "token-1",
          expires_in: 7200,
        });
      }

      if (url.includes("/cgi-bin/kf/sync_msg")) {
        syncCallCount += 1;
        const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        if (syncCallCount === 1) {
          expect(body.token).toBe("callback-sync-token");
          expect(body.open_kfid).toBe("wk-test");
          expect(body.cursor).toBeUndefined();
          return mockJsonResponse({
            errcode: 0,
            errmsg: "ok",
            next_cursor: "cursor-1",
            has_more: 1,
            msg_list: [
              {
                msgid: "msg-1",
                msgtype: "text",
                origin: 3,
                open_kfid: "wk-test",
                external_userid: "wx-user-1",
                send_time: 1710000000,
                text: {
                  content: "hello",
                },
              },
            ],
          });
        }

        expect(body.cursor).toBe("cursor-1");
        return mockJsonResponse({
          errcode: 0,
          errmsg: "ok",
          next_cursor: "cursor-2",
          has_more: 0,
          msg_list: [
            {
              msgid: "msg-1",
              msgtype: "text",
              origin: 3,
              open_kfid: "wk-test",
              external_userid: "wx-user-1",
              send_time: 1710000001,
              text: {
                content: "hello again",
              },
            },
            {
              msgid: "evt-1",
              msgtype: "event",
              origin: 4,
              open_kfid: "wk-test",
              external_userid: "wx-user-1",
              send_time: 1710000002,
              event: {
                event_type: "enter_session",
                welcome_code: "welcome-1",
              },
            },
          ],
        });
      }

      if (url.includes("/cgi-bin/kf/send_msg_on_event")) {
        const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
        expect(body.code).toBe("welcome-1");
        return mockJsonResponse({
          errcode: 0,
          errmsg: "ok",
          msgid: "welcome-msg-1",
        });
      }

      throw new Error(`Unexpected fetch URL: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const unregister = registerWecomKfWebhookTarget(createTarget());

    try {
      const timestamp = "1710000000";
      const nonce = "nonce-post";
      const callbackPayload = encryptWecomPlaintext({
        encodingAESKey,
        receiveId: corpId,
        plaintext: JSON.stringify({
          Token: "callback-sync-token",
          OpenKfId: "wk-test",
        }),
      });
      const signature = computeWecomMsgSignature({
        token,
        timestamp,
        nonce,
        encrypt: callbackPayload,
      });
      const req = createMockRequest({
        method: "POST",
        url: `/wecom-kf?msg_signature=${encodeURIComponent(signature)}&timestamp=${encodeURIComponent(timestamp)}&nonce=${encodeURIComponent(nonce)}`,
        body: {
          encrypt: callbackPayload,
        },
      });
      const res = createMockResponse();

      const handled = await handleWecomKfWebhookRequest(req, res);

      expect(handled).toBe(true);
      expect(res._getStatusCode()).toBe(200);
      expect(res._getData()).toBe("success");

      await vi.waitFor(() => {
        expect(dispatchReplyWithBufferedBlockDispatcher).toHaveBeenCalledTimes(1);
      });
      await vi.waitFor(() => {
        expect(fetchMock).toHaveBeenCalledTimes(4);
      });

      expect(await getStoredCursor("default:wk-test")).toBe("cursor-2");
    } finally {
      unregister();
    }
  });

  it("primes the cursor on cold start without replaying historical messages", async () => {
    let syncCallCount = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/cgi-bin/gettoken")) {
        return mockJsonResponse({
          errcode: 0,
          errmsg: "ok",
          access_token: "token-prime",
          expires_in: 7200,
        });
      }
      if (url.includes("/cgi-bin/kf/sync_msg")) {
        syncCallCount += 1;
        return mockJsonResponse(
          syncCallCount === 1
            ? {
                errcode: 0,
                errmsg: "ok",
                next_cursor: "cursor-prime-1",
                has_more: 1,
                msg_list: [
                  {
                    msgid: "history-1",
                    msgtype: "text",
                    origin: 3,
                    open_kfid: "wk-test",
                    external_userid: "wx-user-1",
                    send_time: 1710000000,
                    text: {
                      content: "history",
                    },
                  },
                ],
              }
            : {
                errcode: 0,
                errmsg: "ok",
                next_cursor: "cursor-prime-2",
                has_more: 0,
                msg_list: [],
              }
        );
      }

      throw new Error(`Unexpected fetch URL: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    await primeWecomKfCursor(createTarget());

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(await getStoredCursor("default:wk-test")).toBe("cursor-prime-2");
  });

  it("skips cursor priming until corpSecret is configured", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    await primeWecomKfCursor(
      createTarget({
        account: {
          corpSecret: undefined,
          canSendActive: false,
          config: {
            webhookPath: "/wecom-kf",
            token,
            encodingAESKey,
            corpId,
            openKfId: "wk-test",
          },
        },
      })
    );

    expect(fetchMock).not.toHaveBeenCalled();
    expect(await getStoredCursor("default:wk-test")).toBeUndefined();
  });
});
