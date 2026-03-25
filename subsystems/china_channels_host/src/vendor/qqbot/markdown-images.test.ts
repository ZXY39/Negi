import { describe, expect, it, vi } from "vitest";
import {
  normalizeQQBotMarkdownImages,
} from "./markdown-images.js";

describe("normalizeQQBotMarkdownImages", () => {
  it("adds QQBot image sizes to markdown http images", async () => {
    const resolveImageSize = vi.fn().mockResolvedValue({ width: 640, height: 480 });

    const result = await normalizeQQBotMarkdownImages({
      text: "![diagram](https://example.com/a.png)",
      resolveImageSize,
    });

    expect(result).toBe("![#640px #480px](https://example.com/a.png)");
  });

  it("appends bare http image urls as QQBot markdown images", async () => {
    const resolveImageSize = vi.fn().mockResolvedValue({ width: 320, height: 180 });

    const result = await normalizeQQBotMarkdownImages({
      text: "说明文字",
      appendImageUrls: ["https://example.com/b.jpg"],
      resolveImageSize,
    });

    expect(result).toBe("说明文字\n\n![#320px #180px](https://example.com/b.jpg)");
  });

  it("keeps already sized QQBot markdown images unchanged", async () => {
    const resolveImageSize = vi.fn();

    const result = await normalizeQQBotMarkdownImages({
      text: "![#800px #600px](https://example.com/c.webp)",
      resolveImageSize,
    });

    expect(result).toBe("![#800px #600px](https://example.com/c.webp)");
    expect(resolveImageSize).not.toHaveBeenCalled();
  });

  it("falls back to 512x512 when image size probing fails", async () => {
    const resolveImageSize = vi.fn().mockRejectedValue(new Error("boom"));

    const result = await normalizeQQBotMarkdownImages({
      text: "![poster](https://example.com/d.gif)",
      resolveImageSize,
    });

    expect(result).toBe("![#512px #512px](https://example.com/d.gif)");
  });

  it("does not rewrite markdown images inside fenced code blocks", async () => {
    const resolveImageSize = vi.fn().mockResolvedValue({ width: 1024, height: 768 });
    const text = "```md\n![demo](https://example.com/e.png)\n```\n\nhttps://example.com/f.png";

    const result = await normalizeQQBotMarkdownImages({
      text,
      resolveImageSize,
    });

    expect(result).toBe(
      "```md\n![demo](https://example.com/e.png)\n```\n\n![#1024px #768px](https://example.com/f.png)"
    );
  });
});
