import { afterEach, describe, expect, it, vi } from "vitest";

import {
  clearAllAccessTokenCache,
  sendKfTextMessage,
  splitMessageByBytes,
  stripMarkdown,
  syncMessages,
} from "./api.js";
import type { ResolvedWecomKfAccount } from "./types.js";

function createAccount(apiBaseUrl?: string): ResolvedWecomKfAccount {
  return {
    accountId: "default",
    enabled: true,
    configured: true,
    token: "callback-token",
    encodingAESKey: "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
    corpId: "ww-test-corp",
    corpSecret: "kf-secret",
    openKfId: "wk-test",
    canSendActive: true,
    config: {
      webhookPath: "/wecom-kf",
      token: "callback-token",
      encodingAESKey: "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
      corpId: "ww-test-corp",
      corpSecret: "kf-secret",
      openKfId: "wk-test",
      apiBaseUrl,
    },
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

afterEach(() => {
  clearAllAccessTokenCache();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("wecom-kf api", () => {
  it("refreshes access token and retries sync_msg when token is invalidated", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        mockJsonResponse({ errcode: 0, errmsg: "ok", access_token: "token-a", expires_in: 7200 })
      )
      .mockResolvedValueOnce(mockJsonResponse({ errcode: 40001, errmsg: "invalid credential" }))
      .mockResolvedValueOnce(
        mockJsonResponse({ errcode: 0, errmsg: "ok", access_token: "token-b", expires_in: 7200 })
      )
      .mockResolvedValueOnce(
        mockJsonResponse({
          errcode: 0,
          errmsg: "ok",
          next_cursor: "cursor-2",
          has_more: 0,
          msg_list: [],
        })
      );
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const result = await syncMessages(createAccount("https://proxy.wecom.local/"), {
      open_kfid: "wk-test",
      token: "sync-token",
    });

    expect(result.next_cursor).toBe("cursor-2");
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "https://proxy.wecom.local/cgi-bin/gettoken?corpid=ww-test-corp&corpsecret=kf-secret"
    );
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      "https://proxy.wecom.local/cgi-bin/kf/sync_msg?access_token=token-a"
    );
    expect(fetchMock.mock.calls[2]?.[0]).toBe(
      "https://proxy.wecom.local/cgi-bin/gettoken?corpid=ww-test-corp&corpsecret=kf-secret"
    );
    expect(fetchMock.mock.calls[3]?.[0]).toBe(
      "https://proxy.wecom.local/cgi-bin/kf/sync_msg?access_token=token-b"
    );
  });

  it("sanitizes markdown and splits outbound text into 2048-byte chunks", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        mockJsonResponse({ errcode: 0, errmsg: "ok", access_token: "token-send", expires_in: 7200 })
      )
      .mockResolvedValueOnce(mockJsonResponse({ errcode: 0, errmsg: "ok", msgid: "msg-1" }))
      .mockResolvedValueOnce(mockJsonResponse({ errcode: 0, errmsg: "ok", msgid: "msg-2" }));
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const text = `# 标题\n\n**${"a".repeat(2200)}**`;
    const results = await sendKfTextMessage({
      account: createAccount(),
      externalUserId: "wx-user-1",
      text,
    });

    const payloads = fetchMock.mock.calls.slice(1).map((call) => {
      const init = call[1] as RequestInit | undefined;
      return JSON.parse(String(init?.body ?? "{}")) as {
        text?: { content?: string };
      };
    });

    expect(results).toHaveLength(2);
    expect(payloads).toHaveLength(2);
    expect(payloads[0]?.text?.content?.startsWith("【标题】")).toBe(true);
    expect(payloads[0]?.text?.content).not.toContain("**");
    expect(payloads[0]?.text?.content).not.toContain("# ");
    for (const payload of payloads) {
      expect(Buffer.byteLength(payload.text?.content ?? "", "utf8")).toBeLessThanOrEqual(2048);
    }

    const normalized = stripMarkdown(text);
    expect(splitMessageByBytes(normalized, 2048)).toHaveLength(2);
  });
});
