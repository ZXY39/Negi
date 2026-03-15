/**
 * Unit Tests for DingTalk Media Parsing Functions
 * 
 * Feature: dingtalk-media-receive
 * Validates: Requirements 2.1-2.6, 3.1-3.6
 */

import { describe, it, expect } from "vitest";
import { extractFileFromMessage, parseRichTextMessage } from "./media.js";

describe("extractFileFromMessage", () => {
  it("should extract downloadCode from picture message", () => {
    const msg = {
      msgtype: "picture",
      content: { downloadCode: "pic-code-123" },
    };
    const result = extractFileFromMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.downloadCode).toBe("pic-code-123");
    expect(result?.msgType).toBe("picture");
  });

  it("should use pictureDownloadCode as fallback for picture", () => {
    const msg = {
      msgtype: "picture",
      content: { pictureDownloadCode: "pic-fallback-456" },
    };
    const result = extractFileFromMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.downloadCode).toBe("pic-fallback-456");
  });

  it("should extract downloadCode from video message with duration", () => {
    const msg = {
      msgtype: "video",
      content: { downloadCode: "video-code-789", duration: 5000 },
    };
    const result = extractFileFromMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.downloadCode).toBe("video-code-789");
    expect(result?.msgType).toBe("video");
    expect(result?.duration).toBe(5000);
  });

  it("should use videoDownloadCode as fallback for video", () => {
    const msg = {
      msgtype: "video",
      content: { videoDownloadCode: "video-fallback-101" },
    };
    const result = extractFileFromMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.downloadCode).toBe("video-fallback-101");
  });

  it("should extract audio message with recognition", () => {
    const msg = {
      msgtype: "audio",
      content: { downloadCode: "audio-code-111", duration: 3000, recognition: "Hello world" },
    };
    const result = extractFileFromMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.downloadCode).toBe("audio-code-111");
    expect(result?.msgType).toBe("audio");
    expect(result?.duration).toBe(3000);
    expect(result?.recognition).toBe("Hello world");
  });

  it("should extract file message with fileName and fileSize", () => {
    const msg = {
      msgtype: "file",
      content: { downloadCode: "file-code-222", fileName: "document.pdf", fileSize: 102400 },
    };
    const result = extractFileFromMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.downloadCode).toBe("file-code-222");
    expect(result?.msgType).toBe("file");
    expect(result?.fileName).toBe("document.pdf");
    expect(result?.fileSize).toBe(102400);
  });

  it("should parse JSON string content", () => {
    const msg = {
      msgtype: "picture",
      content: JSON.stringify({ downloadCode: "json-pic-333" }),
    };
    const result = extractFileFromMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.downloadCode).toBe("json-pic-333");
  });

  it("should return null for unsupported message types", () => {
    const msg = {
      msgtype: "text",
      text: { content: "Hello" },
    };
    const result = extractFileFromMessage(msg);
    expect(result).toBeNull();
  });

  it("should return null for missing downloadCode", () => {
    const msg = {
      msgtype: "picture",
      content: {},
    };
    const result = extractFileFromMessage(msg);
    expect(result).toBeNull();
  });

  it("should return null for invalid input", () => {
    expect(extractFileFromMessage(null)).toBeNull();
    expect(extractFileFromMessage(undefined)).toBeNull();
    expect(extractFileFromMessage("string")).toBeNull();
    expect(extractFileFromMessage(123)).toBeNull();
  });
});

describe("parseRichTextMessage", () => {
  it("should extract text parts from richText", () => {
    const msg = {
      msgtype: "richText",
      content: {
        richText: [
          { type: "text", text: "Hello" },
          { type: "text", text: "World" },
        ],
      },
    };
    const result = parseRichTextMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.textParts).toEqual(["Hello", "World"]);
  });

  it("should treat text elements without type as text", () => {
    const msg = {
      msgtype: "richText",
      content: {
        richText: [
          { text: "Line 1" },
          { text: "Line 2" },
        ],
      },
    };
    const result = parseRichTextMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.textParts).toEqual(["Line 1", "Line 2"]);
  });

  it("should extract image codes from richText", () => {
    const msg = {
      msgtype: "richText",
      content: {
        richText: [
          { type: "picture", downloadCode: "img-code-1" },
          { type: "picture", pictureDownloadCode: "img-code-2" },
        ],
      },
    };
    const result = parseRichTextMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.imageCodes).toEqual(["img-code-1", "img-code-2"]);
  });

  it("should extract mentions from richText", () => {
    const msg = {
      msgtype: "richText",
      content: {
        richText: [
          { type: "at", userId: "user-123" },
          { type: "at", userId: "user-456" },
        ],
      },
    };
    const result = parseRichTextMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.mentions).toEqual(["user-123", "user-456"]);
  });

  it("should handle mixed richText elements", () => {
    const msg = {
      msgtype: "richText",
      content: {
        richText: [
          { type: "text", text: "Hello" },
          { type: "at", userId: "user-789" },
          { type: "picture", downloadCode: "img-code-3" },
          { type: "text", text: "World" },
        ],
      },
    };
    const result = parseRichTextMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.textParts).toEqual(["Hello", "World"]);
    expect(result?.imageCodes).toEqual(["img-code-3"]);
    expect(result?.mentions).toEqual(["user-789"]);
  });

  it("should parse JSON string content", () => {
    const msg = {
      msgtype: "richText",
      content: JSON.stringify({
        richText: [{ type: "text", text: "JSON content" }],
      }),
    };
    const result = parseRichTextMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.textParts).toEqual(["JSON content"]);
  });

  it("should parse JSON string richText array", () => {
    const msg = {
      msgtype: "richText",
      content: {
        richText: JSON.stringify([{ type: "text", text: "Nested JSON" }]),
      },
    };
    const result = parseRichTextMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.textParts).toEqual(["Nested JSON"]);
  });

  it("should return null for non-richText messages", () => {
    const msg = {
      msgtype: "text",
      text: { content: "Hello" },
    };
    const result = parseRichTextMessage(msg);
    expect(result).toBeNull();
  });

  it("should return null for empty richText array", () => {
    const msg = {
      msgtype: "richText",
      content: { richText: [] },
    };
    const result = parseRichTextMessage(msg);
    expect(result).toBeNull();
  });

  it("should return empty imageCodes for text-only richText", () => {
    const msg = {
      msgtype: "richText",
      content: {
        richText: [
          { type: "text", text: "Only text" },
          { type: "text", text: "No images" },
        ],
      },
    };
    const result = parseRichTextMessage(msg);
    expect(result).not.toBeNull();
    expect(result?.textParts).toEqual(["Only text", "No images"]);
    expect(result?.imageCodes).toEqual([]);
    expect(result?.mentions).toEqual([]);
  });

  it("should return null for invalid input", () => {
    expect(parseRichTextMessage(null)).toBeNull();
    expect(parseRichTextMessage(undefined)).toBeNull();
    expect(parseRichTextMessage("string")).toBeNull();
  });
});
