/**
 * Unit Tests for File Utilities
 * 
 * Feature: dingtalk-media-receive
 * Validates: Requirements 5.1-5.8, 6.1-6.6
 */

import { describe, it, expect } from "vitest";
import { resolveFileCategory, resolveExtension } from "./file-utils.js";

describe("resolveFileCategory", () => {
  // Image categorization (Requirement 5.1)
  it("should categorize image MIME types", () => {
    expect(resolveFileCategory("image/jpeg")).toBe("image");
    expect(resolveFileCategory("image/png")).toBe("image");
    expect(resolveFileCategory("image/gif")).toBe("image");
    expect(resolveFileCategory("image/webp")).toBe("image");
    expect(resolveFileCategory("image/bmp")).toBe("image");
  });

  // Audio categorization (Requirement 5.2)
  it("should categorize audio MIME types", () => {
    expect(resolveFileCategory("audio/mpeg")).toBe("audio");
    expect(resolveFileCategory("audio/wav")).toBe("audio");
    expect(resolveFileCategory("audio/ogg")).toBe("audio");
    expect(resolveFileCategory("audio/amr")).toBe("audio");
  });

  // Video categorization (Requirement 5.3)
  it("should categorize video MIME types", () => {
    expect(resolveFileCategory("video/mp4")).toBe("video");
    expect(resolveFileCategory("video/quicktime")).toBe("video");
    expect(resolveFileCategory("video/webm")).toBe("video");
  });

  // Document categorization (Requirement 5.4)
  it("should categorize document MIME types", () => {
    expect(resolveFileCategory("application/pdf")).toBe("document");
    expect(resolveFileCategory("application/msword")).toBe("document");
    expect(resolveFileCategory("text/plain")).toBe("document");
    expect(resolveFileCategory("text/markdown")).toBe("document");
  });

  // Archive categorization (Requirement 5.5)
  it("should categorize archive MIME types", () => {
    expect(resolveFileCategory("application/zip")).toBe("archive");
    expect(resolveFileCategory("application/x-rar-compressed")).toBe("archive");
    expect(resolveFileCategory("application/x-7z-compressed")).toBe("archive");
  });

  // Code categorization (Requirement 5.6)
  it("should categorize code MIME types", () => {
    expect(resolveFileCategory("application/json")).toBe("code");
    expect(resolveFileCategory("text/html")).toBe("code");
    expect(resolveFileCategory("text/css")).toBe("code");
    expect(resolveFileCategory("text/javascript")).toBe("code");
  });

  // Extension fallback (Requirement 5.8)
  it("should use extension fallback when MIME type is unknown", () => {
    expect(resolveFileCategory("application/octet-stream", "photo.jpg")).toBe("image");
    expect(resolveFileCategory("application/octet-stream", "song.mp3")).toBe("audio");
    expect(resolveFileCategory("application/octet-stream", "movie.mp4")).toBe("video");
    expect(resolveFileCategory("application/octet-stream", "doc.pdf")).toBe("document");
    expect(resolveFileCategory("application/octet-stream", "archive.zip")).toBe("archive");
    expect(resolveFileCategory("application/octet-stream", "script.py")).toBe("code");
  });

  // Other category (Requirement 5.7)
  it("should return 'other' for unknown types", () => {
    expect(resolveFileCategory("application/octet-stream")).toBe("other");
    expect(resolveFileCategory("application/unknown")).toBe("other");
    expect(resolveFileCategory("application/octet-stream", "file.xyz")).toBe("other");
  });

  // MIME type normalization
  it("should handle MIME types with parameters", () => {
    expect(resolveFileCategory("image/jpeg; charset=utf-8")).toBe("image");
    expect(resolveFileCategory("text/plain; charset=utf-8")).toBe("document");
  });
});

describe("resolveExtension", () => {
  // Image extensions (Requirement 6.1)
  it("should resolve image MIME types to extensions", () => {
    expect(resolveExtension("image/jpeg")).toBe(".jpg");
    expect(resolveExtension("image/png")).toBe(".png");
    expect(resolveExtension("image/gif")).toBe(".gif");
    expect(resolveExtension("image/webp")).toBe(".webp");
    expect(resolveExtension("image/bmp")).toBe(".bmp");
  });

  // Audio extensions (Requirement 6.2)
  it("should resolve audio MIME types to extensions", () => {
    expect(resolveExtension("audio/mpeg")).toBe(".mp3");
    expect(resolveExtension("audio/wav")).toBe(".wav");
    expect(resolveExtension("audio/ogg")).toBe(".ogg");
    expect(resolveExtension("audio/amr")).toBe(".amr");
    expect(resolveExtension("audio/x-m4a")).toBe(".m4a");
  });

  // Video extensions (Requirement 6.3)
  it("should resolve video MIME types to extensions", () => {
    expect(resolveExtension("video/mp4")).toBe(".mp4");
    expect(resolveExtension("video/quicktime")).toBe(".mov");
    expect(resolveExtension("video/x-msvideo")).toBe(".avi");
    expect(resolveExtension("video/webm")).toBe(".webm");
  });

  // Document extensions (Requirement 6.4)
  it("should resolve document MIME types to extensions", () => {
    expect(resolveExtension("application/pdf")).toBe(".pdf");
    expect(resolveExtension("application/msword")).toBe(".doc");
    expect(resolveExtension("application/vnd.openxmlformats-officedocument.wordprocessingml.document")).toBe(".docx");
  });

  // Default extension (Requirement 6.5)
  it("should return .bin for unknown MIME types", () => {
    expect(resolveExtension("application/unknown")).toBe(".bin");
    expect(resolveExtension("application/octet-stream")).toBe(".bin");
  });

  // fileName precedence (Requirement 6.6)
  it("should use fileName extension when provided", () => {
    expect(resolveExtension("application/octet-stream", "photo.jpg")).toBe(".jpg");
    expect(resolveExtension("image/png", "custom.jpeg")).toBe(".jpeg");
    expect(resolveExtension("application/unknown", "document.pdf")).toBe(".pdf");
  });

  // MIME type normalization
  it("should handle MIME types with parameters", () => {
    expect(resolveExtension("image/jpeg; charset=utf-8")).toBe(".jpg");
    expect(resolveExtension("audio/mpeg; bitrate=320")).toBe(".mp3");
  });

  // Edge cases
  it("should handle fileName without extension", () => {
    expect(resolveExtension("image/jpeg", "photo")).toBe(".jpg");
    expect(resolveExtension("application/unknown", "noext")).toBe(".bin");
  });
});
