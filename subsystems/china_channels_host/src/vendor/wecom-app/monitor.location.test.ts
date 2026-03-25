import { Readable } from "node:stream";
import type { IncomingMessage, ServerResponse } from "node:http";
import { afterEach, describe, expect, it, vi } from "vitest";

const { dispatchWecomAppMessageMock } = vi.hoisted(() => ({
  dispatchWecomAppMessageMock: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("./bot.js", () => ({
  dispatchWecomAppMessage: dispatchWecomAppMessageMock,
}));

vi.mock("./runtime.js", () => ({
  tryGetWecomAppRuntime: () => ({}),
}));

import { computeWecomAppMsgSignature, encryptWecomAppPlaintext } from "./crypto.js";
import { handleWecomAppWebhookRequest, registerWecomAppWebhookTarget } from "./monitor.js";
import type { ResolvedWecomAppAccount } from "./types.js";

const token = "token123";
const encodingAESKey = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG";

function createRequest(method: string, url: string, body?: string): IncomingMessage {
  const stream = new Readable({
    read() {
      return;
    },
  });
  if (body) stream.push(body);
  stream.push(null);
  (stream as IncomingMessage).method = method;
  (stream as IncomingMessage).url = url;
  return stream as IncomingMessage;
}

function createResponseRecorder() {
  const chunks: Buffer[] = [];
  const headers: Record<string, string> = {};
  const res = {
    statusCode: 200,
    setHeader: (name: string, value: string) => {
      headers[name.toLowerCase()] = value;
    },
    end: (data?: string | Buffer) => {
      if (data === undefined) return;
      chunks.push(Buffer.isBuffer(data) ? data : Buffer.from(String(data)));
    },
  } as unknown as ServerResponse;

  return {
    res,
    headers,
    getBody: () => Buffer.concat(chunks).toString("utf8"),
  };
}

function buildAccount(params: {
  accountId: string;
  agentId?: number;
  receiveId: string;
}): ResolvedWecomAppAccount {
  return {
    accountId: params.accountId,
    enabled: true,
    configured: true,
    token,
    encodingAESKey,
    receiveId: params.receiveId,
    agentId: params.agentId,
    canSendActive: false,
    config: {
      webhookPath: "/wecom-app",
      agentId: params.agentId,
    },
  };
}

afterEach(() => {
  dispatchWecomAppMessageMock.mockClear();
});

describe("wecom-app location parsing", () => {
  it("maps XML location fields before dispatch", async () => {
    const unregister = registerWecomAppWebhookTarget({
      account: buildAccount({ accountId: "app", agentId: 1001, receiveId: "corp123" }),
      config: { channels: { "wecom-app": {} } },
      runtime: {},
      path: "/wecom-app",
    });

    const locationXml = [
      "<xml>",
      "<ToUserName><![CDATA[toUser]]></ToUserName>",
      "<FromUserName><![CDATA[user1]]></FromUserName>",
      "<CreateTime>1700000001</CreateTime>",
      "<MsgType><![CDATA[location]]></MsgType>",
      "<Location_X>31.2304</Location_X>",
      "<Location_Y>121.4737</Location_Y>",
      "<Scale>15</Scale>",
      "<Label><![CDATA[上海外滩]]></Label>",
      "<MsgId>msg-location-1</MsgId>",
      "<AgentID>1001</AgentID>",
      "</xml>",
    ].join("");

    const encrypt = encryptWecomAppPlaintext({
      encodingAESKey,
      receiveId: "corp123",
      plaintext: locationXml,
    });

    const timestamp = "1700000001";
    const nonce = "nonce-location-1";
    const signature = computeWecomAppMsgSignature({
      token,
      timestamp,
      nonce,
      encrypt,
    });
    const params = new URLSearchParams({
      timestamp,
      nonce,
      msg_signature: signature,
    });

    const req = createRequest("POST", `/wecom-app?${params.toString()}`, JSON.stringify({ encrypt }));
    const recorder = createResponseRecorder();
    const handled = await handleWecomAppWebhookRequest(req, recorder.res);

    expect(handled).toBe(true);
    expect(dispatchWecomAppMessageMock).toHaveBeenCalledTimes(1);

    const firstCall = dispatchWecomAppMessageMock.mock.calls[0]?.[0] as
      | { msg?: Record<string, unknown> }
      | undefined;
    expect(firstCall?.msg?.msgtype).toBe("location");
    expect(firstCall?.msg?.Location_X).toBe("31.2304");
    expect(firstCall?.msg?.Location_Y).toBe("121.4737");
    expect(firstCall?.msg?.Scale).toBe("15");
    expect(firstCall?.msg?.Label).toBe("上海外滩");

    unregister();
  });
});
