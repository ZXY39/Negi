/**
 * Unit Tests for DingTalk Media Error Types
 * 
 * Feature: dingtalk-media-receive
 * Validates: Requirements 1.4, 1.5, 1.6
 */

import { describe, it, expect } from "vitest";
import { FileSizeLimitError, TimeoutError } from "./media.js";

describe("FileSizeLimitError", () => {
  it("should create error with correct properties", () => {
    const error = new FileSizeLimitError(15_000_000, 10_000_000, "picture");
    
    expect(error).toBeInstanceOf(Error);
    expect(error).toBeInstanceOf(FileSizeLimitError);
    expect(error.name).toBe("FileSizeLimitError");
    expect(error.actualSize).toBe(15_000_000);
    expect(error.limitSize).toBe(10_000_000);
    expect(error.msgType).toBe("picture");
    expect(error.message).toBe("File size 15000000 bytes exceeds limit 10000000 bytes for picture");
  });

  it("should have proper stack trace", () => {
    const error = new FileSizeLimitError(100, 50, "audio");
    expect(error.stack).toBeDefined();
  });
});

describe("TimeoutError", () => {
  it("should create error with correct properties", () => {
    const error = new TimeoutError(120000);
    
    expect(error).toBeInstanceOf(Error);
    expect(error).toBeInstanceOf(TimeoutError);
    expect(error.name).toBe("TimeoutError");
    expect(error.timeoutMs).toBe(120000);
    expect(error.message).toBe("Download timed out after 120000ms");
  });

  it("should have proper stack trace", () => {
    const error = new TimeoutError(30000);
    expect(error.stack).toBeDefined();
  });
});
