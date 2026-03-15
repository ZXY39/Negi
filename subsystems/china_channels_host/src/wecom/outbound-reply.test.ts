import os from "node:os";
import path from "node:path";
import { promises as fs } from "node:fs";

import { afterEach, describe, expect, it } from "vitest";

import {
  buildTempMediaUrl,
  clearOutboundReplyState,
  getAccountPublicBaseUrl,
  registerTempLocalMedia,
  setAccountPublicBaseUrl,
} from "./outbound-reply.js";

describe("wecom outbound reply helpers", () => {
  afterEach(() => {
    clearOutboundReplyState();
  });

  it("normalizes configured public base urls", () => {
    setAccountPublicBaseUrl("default", "https://bot.example.com/base/");
    expect(getAccountPublicBaseUrl("default")).toBe("https://bot.example.com/base");
  });

  it("builds temp media urls from a registered local file", async () => {
    const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "wecom-media-"));
    const filePath = path.join(tempDir, "report.txt");
    await fs.writeFile(filePath, "hello", "utf8");

    const temp = await registerTempLocalMedia({
      filePath,
    });
    const mediaUrl = buildTempMediaUrl({
      baseUrl: "https://bot.example.com/",
      id: temp.id,
      token: temp.token,
      fileName: temp.fileName,
    });

    expect(temp.fileName).toBe("report.txt");
    expect(mediaUrl).toContain(`/wecom-media/${temp.id}/report.txt`);
    expect(mediaUrl).toContain(`token=${temp.token}`);
  });
});
