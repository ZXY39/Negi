import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { prepareDingtalkReplyContent } from "./bot-handler.js";

describe("prepareDingtalkReplyContent", () => {
  let tempDir: string | undefined;

  afterEach(() => {
    if (tempDir) {
      fs.rmSync(tempDir, { recursive: true, force: true });
      tempDir = undefined;
    }
  });

  it("removes local media syntax from reply text while keeping the media queue", () => {
    tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "dingtalk-reply-"));
    const imagePath = path.join(tempDir, "reply.png");
    const filePath = path.join(tempDir, "notes.pdf");
    fs.writeFileSync(imagePath, "image");
    fs.writeFileSync(filePath, "file");

    const text = [
      "Before",
      `MEDIA: ${imagePath}`,
      `![inline](${imagePath})`,
      `See [file](${filePath})`,
      "After",
    ].join("\n");

    const result = prepareDingtalkReplyContent({ text });

    expect(result.text).toBe("Before\n\nSee [文件: notes.pdf]\nAfter");
    expect(result.mediaUrls).toEqual([imagePath, filePath]);
  });

  it("keeps remote markdown images in text", () => {
    const text = "Before\n![remote](https://example.com/reply.png)\nAfter";

    const result = prepareDingtalkReplyContent({ text });

    expect(result.text).toBe(text);
    expect(result.mediaUrls).toEqual([]);
  });

  it("removes linked local markdown images without leaving an empty link shell", () => {
    tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "dingtalk-reply-"));
    const imagePath = path.join(tempDir, "reply.png");
    fs.writeFileSync(imagePath, "image");
    const text = `Before\n[![inline](${imagePath})](https://example.com/view)\nAfter`;

    const result = prepareDingtalkReplyContent({ text });

    expect(result.text).toBe("Before\n\nAfter");
    expect(result.mediaUrls).toEqual([imagePath]);
  });

  it("removes MEDIA lines with remote urls from the text payload", () => {
    const mediaUrl = "https://example.com/reply.png";
    const text = `Before\nMEDIA: ${mediaUrl}\nAfter`;

    const result = prepareDingtalkReplyContent({ text });

    expect(result.text).toBe("Before\nAfter");
    expect(result.mediaUrls).toEqual([mediaUrl]);
  });
});
