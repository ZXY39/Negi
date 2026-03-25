/**
 * Property-Based Tests for Feishu Config Schema
 *
 * Feature: feishu-integration
 * Property 1: 配置 Schema 验证
 */

import { describe, it, expect } from "vitest";
import * as fc from "fast-check";
import { FeishuConfigSchema, isConfigured, resolveFeishuCredentials } from "./config.js";

describe("Feature: feishu-integration, Property 1: 配置 Schema 验证", () => {
  it("should parse valid configs and apply defaults", () => {
    const validConfigArb = fc.record({
      enabled: fc.option(fc.boolean(), { nil: undefined }),
      appId: fc.option(fc.string({ minLength: 1 }), { nil: undefined }),
      appSecret: fc.option(fc.string({ minLength: 1 }), { nil: undefined }),
      connectionMode: fc.option(fc.constantFrom("websocket"), { nil: undefined }),
      dmPolicy: fc.option(fc.constantFrom("open", "pairing", "allowlist"), { nil: undefined }),
      groupPolicy: fc.option(fc.constantFrom("open", "allowlist", "disabled"), { nil: undefined }),
      requireMention: fc.option(fc.boolean(), { nil: undefined }),
      allowFrom: fc.option(fc.array(fc.string()), { nil: undefined }),
      groupAllowFrom: fc.option(fc.array(fc.string()), { nil: undefined }),
      sendMarkdownAsCard: fc.option(fc.boolean(), { nil: undefined }),
      historyLimit: fc.option(fc.integer({ min: 0, max: 100 }), { nil: undefined }),
      textChunkLimit: fc.option(fc.integer({ min: 1, max: 10000 }), { nil: undefined }),
    });

    fc.assert(
      fc.property(validConfigArb, (config) => {
        const result = FeishuConfigSchema.safeParse(config);
        expect(result.success).toBe(true);

        if (result.success) {
          expect(typeof result.data.enabled).toBe("boolean");
          expect(typeof result.data.dmPolicy).toBe("string");
          expect(typeof result.data.groupPolicy).toBe("string");
          expect(typeof result.data.requireMention).toBe("boolean");
          expect(typeof result.data.historyLimit).toBe("number");
          expect(typeof result.data.textChunkLimit).toBe("number");
          expect(typeof result.data.connectionMode).toBe("string");
          expect(typeof result.data.sendMarkdownAsCard).toBe("boolean");

          if (config.enabled === undefined) {
            expect(result.data.enabled).toBe(true);
          }
          if (config.dmPolicy === undefined) {
            expect(result.data.dmPolicy).toBe("open");
          }
          if (config.groupPolicy === undefined) {
            expect(result.data.groupPolicy).toBe("open");
          }
          if (config.requireMention === undefined) {
            expect(result.data.requireMention).toBe(true);
          }
          if (config.historyLimit === undefined) {
            expect(result.data.historyLimit).toBe(20);
          }
          if (config.textChunkLimit === undefined) {
            expect(result.data.textChunkLimit).toBe(4000);
          }
          if (config.connectionMode === undefined) {
            expect(result.data.connectionMode).toBe("websocket");
          }
          if (config.sendMarkdownAsCard === undefined) {
            expect(result.data.sendMarkdownAsCard).toBe(true);
          }
        }
      }),
      { numRuns: 100 }
    );
  });

  it("should return false for isConfigured when credentials are missing", () => {
    const missingCredentialsArb = fc.oneof(
      fc.record({
        appId: fc.constant(undefined),
        appSecret: fc.option(fc.string({ minLength: 1 }), { nil: undefined }),
      }),
      fc.record({
        appId: fc.option(fc.string({ minLength: 1 }), { nil: undefined }),
        appSecret: fc.constant(undefined),
      }),
      fc.record({
        appId: fc.constant(undefined),
        appSecret: fc.constant(undefined),
      }),
      fc.record({
        appId: fc.constant(""),
        appSecret: fc.string({ minLength: 1 }),
      }),
      fc.record({
        appId: fc.string({ minLength: 1 }),
        appSecret: fc.constant(""),
      })
    );

    fc.assert(
      fc.property(missingCredentialsArb, (config) => {
        const parsed = FeishuConfigSchema.safeParse(config);
        if (parsed.success) {
          expect(isConfigured(parsed.data)).toBe(false);
        }
      }),
      { numRuns: 100 }
    );
  });

  it("should return true for isConfigured when credentials are present", () => {
    const configWithCredentialsArb = fc.record({
      appId: fc.string({ minLength: 1 }),
      appSecret: fc.string({ minLength: 1 }),
      dmPolicy: fc.option(fc.constantFrom("open", "pairing", "allowlist"), { nil: undefined }),
      groupPolicy: fc.option(fc.constantFrom("open", "allowlist", "disabled"), { nil: undefined }),
    });

    fc.assert(
      fc.property(configWithCredentialsArb, (config) => {
        const parsed = FeishuConfigSchema.safeParse(config);
        expect(parsed.success).toBe(true);

        if (parsed.success) {
          expect(isConfigured(parsed.data)).toBe(true);
          const credentials = resolveFeishuCredentials(parsed.data);
          expect(credentials).toBeDefined();
          expect(credentials?.appId).toBe(config.appId);
          expect(credentials?.appSecret).toBe(config.appSecret);
        }
      }),
      { numRuns: 100 }
    );
  });

  it("should reject invalid policy values", () => {
    const invalidPolicyArb = fc.record({
      dmPolicy: fc.string().filter((s) => !["open", "pairing", "allowlist"].includes(s)),
    });

    fc.assert(
      fc.property(invalidPolicyArb, (config) => {
        const result = FeishuConfigSchema.safeParse(config);
        expect(result.success).toBe(false);
      }),
      { numRuns: 50 }
    );
  });

  it("should reject negative historyLimit values", () => {
    const negativeHistoryArb = fc.record({
      historyLimit: fc.integer({ max: -1 }),
    });

    fc.assert(
      fc.property(negativeHistoryArb, (config) => {
        const result = FeishuConfigSchema.safeParse(config);
        expect(result.success).toBe(false);
      }),
      { numRuns: 50 }
    );
  });

  it("should reject non-positive textChunkLimit values", () => {
    const nonPositiveChunkArb = fc.record({
      textChunkLimit: fc.integer({ max: 0 }),
    });

    fc.assert(
      fc.property(nonPositiveChunkArb, (config) => {
        const result = FeishuConfigSchema.safeParse(config);
        expect(result.success).toBe(false);
      }),
      { numRuns: 50 }
    );
  });
});
