import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { extractQQBotReplyMedia } from "./bot.js";

function createTempImageFile(): { dir: string; filePath: string } {
  const dir = mkdtempSync(join(tmpdir(), "qqbot-media-test-"));
  const filePath = join(dir, "evidence.jpeg");
  writeFileSync(filePath, "image");
  return { dir, filePath };
}

describe("extractQQBotReplyMedia", () => {
  it("turns bare local image paths into media by default", () => {
    const { dir, filePath } = createTempImageFile();

    try {
      const result = extractQQBotReplyMedia({
        text: `基于你发来的图片 ${filePath}`,
        autoSendLocalPathMedia: true,
      });

      expect(result.text).toBe("基于你发来的图片");
      expect(result.mediaUrls).toEqual([filePath]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("keeps bare local image paths as text when auto send is disabled", () => {
    const { dir, filePath } = createTempImageFile();

    try {
      const result = extractQQBotReplyMedia({
        text: `基于你发来的图片 ${filePath}`,
        autoSendLocalPathMedia: false,
      });

      expect(result.text).toBe(`基于你发来的图片 ${filePath}`);
      expect(result.mediaUrls).toEqual([]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("still honors explicit MEDIA lines when auto send is disabled", () => {
    const { dir, filePath } = createTempImageFile();

    try {
      const result = extractQQBotReplyMedia({
        text: `证据如下\nMEDIA: ${filePath}`,
        autoSendLocalPathMedia: false,
      });

      expect(result.text).toBe("证据如下");
      expect(result.mediaUrls).toEqual([filePath]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
