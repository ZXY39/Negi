/**
 * Property-Based Tests for DingTalk Config Schema
 * 
 * Feature: dingtalk-integration
 * Property 1: 配置 Schema 验证
 */

import { describe, it, expect } from "vitest";
import * as fc from "fast-check";
import {
  DEFAULT_ACCOUNT_ID,
  DingtalkConfigSchema,
  isConfigured,
  listDingtalkAccountIds,
  mergeDingtalkAccountConfig,
  moveDingtalkSingleAccountConfigToDefaultAccount,
  resolveDingtalkAccountId,
  resolveDefaultDingtalkAccountId,
  resolveDingtalkCredentials,
  resolveInboundMediaDir,
  resolveInboundMediaKeepDays,
} from "./config.js";

describe("Feature: dingtalk-integration, Property 1: 配置 Schema 验证", () => {
  const nonBlankStringArb = fc.string({ minLength: 1 }).filter((value) => value.trim().length > 0);

  /**
   * Property: For any valid DingTalk config object, Zod schema parsing should succeed
   * and return a config with all required default values.
   */
  it("should parse valid configs and apply defaults", () => {
    // Arbitrary for valid config objects
    const validConfigArb = fc.record({
      enabled: fc.option(fc.boolean(), { nil: undefined }),
      clientId: fc.option(nonBlankStringArb, { nil: undefined }),
      clientSecret: fc.option(nonBlankStringArb, { nil: undefined }),
      dmPolicy: fc.option(fc.constantFrom("open", "pairing", "allowlist"), { nil: undefined }),
      groupPolicy: fc.option(fc.constantFrom("open", "allowlist", "disabled"), { nil: undefined }),
      requireMention: fc.option(fc.boolean(), { nil: undefined }),
      allowFrom: fc.option(fc.array(fc.string()), { nil: undefined }),
      groupAllowFrom: fc.option(fc.array(fc.string()), { nil: undefined }),
      historyLimit: fc.option(fc.integer({ min: 0, max: 100 }), { nil: undefined }),
      textChunkLimit: fc.option(fc.integer({ min: 1, max: 10000 }), { nil: undefined }),
      longTaskNoticeDelayMs: fc.option(fc.integer({ min: 0, max: 300000 }), { nil: undefined }),
    });

    fc.assert(
      fc.property(validConfigArb, (config) => {
        const result = DingtalkConfigSchema.safeParse(config);
        
        // Schema should parse successfully
        expect(result.success).toBe(true);
        
        if (result.success) {
          // Default values should be applied
          expect(typeof result.data.enabled).toBe("boolean");
          expect(typeof result.data.dmPolicy).toBe("string");
          expect(typeof result.data.groupPolicy).toBe("string");
          expect(typeof result.data.requireMention).toBe("boolean");
          expect(typeof result.data.historyLimit).toBe("number");
          expect(typeof result.data.textChunkLimit).toBe("number");
          expect(typeof result.data.longTaskNoticeDelayMs).toBe("number");
          
          // Verify default values when not provided
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
            expect(result.data.historyLimit).toBe(10);
          }
          if (config.textChunkLimit === undefined) {
            expect(result.data.textChunkLimit).toBe(4000);
          }
          if (config.longTaskNoticeDelayMs === undefined) {
            expect(result.data.longTaskNoticeDelayMs).toBe(30000);
          }
        }
      }),
      { numRuns: 100 }
    );
  });

  /**
   * Property: For any config missing clientId or clientSecret,
   * isConfigured function should return false.
   */
  it("should return false for isConfigured when credentials are missing", () => {
    // Arbitrary for configs with missing credentials
    const missingCredentialsArb = fc.oneof(
      // Missing clientId
      fc.record({
        enabled: fc.option(fc.boolean(), { nil: undefined }),
        clientId: fc.constant(undefined),
        clientSecret: fc.option(nonBlankStringArb, { nil: undefined }),
      }),
      // Missing clientSecret
      fc.record({
        enabled: fc.option(fc.boolean(), { nil: undefined }),
        clientId: fc.option(nonBlankStringArb, { nil: undefined }),
        clientSecret: fc.constant(undefined),
      }),
      // Both missing
      fc.record({
        enabled: fc.option(fc.boolean(), { nil: undefined }),
        clientId: fc.constant(undefined),
        clientSecret: fc.constant(undefined),
      }),
      // Empty strings
      fc.record({
        enabled: fc.option(fc.boolean(), { nil: undefined }),
        clientId: fc.constant(""),
        clientSecret: nonBlankStringArb,
      }),
      fc.record({
        enabled: fc.option(fc.boolean(), { nil: undefined }),
        clientId: nonBlankStringArb,
        clientSecret: fc.constant(""),
      })
    );

    fc.assert(
      fc.property(missingCredentialsArb, (config) => {
        const parsed = DingtalkConfigSchema.safeParse(config);
        if (parsed.success) {
          expect(isConfigured(parsed.data)).toBe(false);
        }
      }),
      { numRuns: 100 }
    );
  });

  /**
   * Property: For any config with both clientId and clientSecret present,
   * isConfigured should return true and resolveDingtalkCredentials should return credentials.
   */
  it("should return true for isConfigured when credentials are present", () => {
    const configWithCredentialsArb = fc.record({
      enabled: fc.option(fc.boolean(), { nil: undefined }),
      clientId: nonBlankStringArb,
      clientSecret: nonBlankStringArb,
      dmPolicy: fc.option(fc.constantFrom("open", "pairing", "allowlist"), { nil: undefined }),
      groupPolicy: fc.option(fc.constantFrom("open", "allowlist", "disabled"), { nil: undefined }),
    });

    fc.assert(
      fc.property(configWithCredentialsArb, (config) => {
        const parsed = DingtalkConfigSchema.safeParse(config);
        expect(parsed.success).toBe(true);
        
        if (parsed.success) {
          expect(isConfigured(parsed.data)).toBe(true);
          
          const credentials = resolveDingtalkCredentials(parsed.data);
          expect(credentials).toBeDefined();
          expect(credentials?.clientId).toBe(config.clientId.trim());
          expect(credentials?.clientSecret).toBe(config.clientSecret.trim());
        }
      }),
      { numRuns: 100 }
    );
  });

  /**
   * Property: Schema should reject invalid policy values
   */
  it("should reject invalid policy values", () => {
    const invalidPolicyArb = fc.record({
      dmPolicy: fc.string().filter(s => !["open", "pairing", "allowlist"].includes(s)),
    });

    fc.assert(
      fc.property(invalidPolicyArb, (config) => {
        const result = DingtalkConfigSchema.safeParse(config);
        expect(result.success).toBe(false);
      }),
      { numRuns: 50 }
    );
  });

  /**
   * Property: historyLimit should reject negative values
   */
  it("should reject negative historyLimit values", () => {
    const negativeHistoryArb = fc.record({
      historyLimit: fc.integer({ max: -1 }),
    });

    fc.assert(
      fc.property(negativeHistoryArb, (config) => {
        const result = DingtalkConfigSchema.safeParse(config);
        expect(result.success).toBe(false);
      }),
      { numRuns: 50 }
    );
  });

  /**
   * Property: textChunkLimit should reject non-positive values
   */
  it("should reject non-positive textChunkLimit values", () => {
    const nonPositiveChunkArb = fc.record({
      textChunkLimit: fc.integer({ max: 0 }),
    });

    fc.assert(
      fc.property(nonPositiveChunkArb, (config) => {
        const result = DingtalkConfigSchema.safeParse(config);
        expect(result.success).toBe(false);
      }),
      { numRuns: 50 }
    );
  });

  /**
   * Property: longTaskNoticeDelayMs should reject negative values
   */
  it("should reject negative longTaskNoticeDelayMs values", () => {
    const negativeDelayArb = fc.record({
      longTaskNoticeDelayMs: fc.integer({ max: -1 }),
    });

    fc.assert(
      fc.property(negativeDelayArb, (config) => {
        const result = DingtalkConfigSchema.safeParse(config);
        expect(result.success).toBe(false);
      }),
      { numRuns: 50 }
    );
  });
});

describe("inboundMedia retention config", () => {
  it("uses default keepDays=7", () => {
    expect(resolveInboundMediaKeepDays(undefined)).toBe(7);
  });

  it("resolves keepDays and dir from config", () => {
    const cfg = DingtalkConfigSchema.parse({
      inboundMedia: {
        dir: "/tmp/custom-inbound",
        keepDays: 3,
      },
    });
    expect(resolveInboundMediaKeepDays(cfg)).toBe(3);
    expect(resolveInboundMediaDir(cfg)).toBe("/tmp/custom-inbound");
  });
});

describe("multi-account helpers", () => {
  it("lists configured accounts and falls back to default", () => {
    expect(listDingtalkAccountIds({})).toEqual([DEFAULT_ACCOUNT_ID]);
    expect(
      listDingtalkAccountIds({
        channels: {
          dingtalk: {
            accounts: {
              bot2: { clientId: "two", clientSecret: "secret-2" },
              bot1: { clientId: "one", clientSecret: "secret-1" },
            },
          },
        },
      })
    ).toEqual(["bot1", "bot2"]);
  });

  it("resolves default account with explicit value or first configured account", () => {
    expect(
      resolveDefaultDingtalkAccountId({
        channels: {
          dingtalk: {
            defaultAccount: "work",
            accounts: {
              work: { clientId: "work-id", clientSecret: "work-secret" },
              personal: { clientId: "personal-id", clientSecret: "personal-secret" },
            },
          },
        },
      })
    ).toBe("work");

    expect(
      resolveDefaultDingtalkAccountId({
        channels: {
          dingtalk: {
            accounts: {
              zebra: { clientId: "zebra-id", clientSecret: "zebra-secret" },
              alpha: { clientId: "alpha-id", clientSecret: "alpha-secret" },
            },
          },
        },
      })
    ).toBe("alpha");
  });

  it("ignores invalid preferred default account and falls back to a configured id", () => {
    expect(
      resolveDefaultDingtalkAccountId({
        channels: {
          dingtalk: {
            defaultAccount: "missing",
            accounts: {
              zebra: { clientId: "zebra-id", clientSecret: "zebra-secret" },
              alpha: { clientId: "alpha-id", clientSecret: "alpha-secret" },
            },
          },
        },
      })
    ).toBe("alpha");
  });

  it("includes default account for mixed configs that still keep base credentials", () => {
    expect(
      listDingtalkAccountIds({
        channels: {
          dingtalk: {
            clientId: "base-id",
            clientSecret: "base-secret",
            accounts: {
              work: { clientId: "work-id", clientSecret: "work-secret" },
            },
          },
        },
      })
    ).toEqual([DEFAULT_ACCOUNT_ID, "work"]);
  });

  it("resolves omitted account ids through the validated default account", () => {
    const cfg = {
      channels: {
        dingtalk: {
          defaultAccount: "work",
          accounts: {
            work: { clientId: "work-id", clientSecret: "work-secret" },
            other: { clientId: "other-id", clientSecret: "other-secret" },
          },
        },
      },
    };

    expect(resolveDingtalkAccountId(cfg, undefined)).toBe("work");
    expect(resolveDingtalkAccountId(cfg, "  ")).toBe("work");
    expect(resolveDingtalkAccountId(cfg, "other")).toBe("other");
  });

  it("merges top-level defaults with account overrides", () => {
    const merged = mergeDingtalkAccountConfig(
      {
        channels: {
          dingtalk: {
            enabled: true,
            clientId: "base-id",
            clientSecret: "base-secret",
            dmPolicy: "allowlist",
            allowFrom: ["u1"],
            textChunkLimit: 4000,
            accounts: {
              work: {
                clientId: "work-id",
                clientSecret: "work-secret",
                textChunkLimit: 2000,
              },
            },
          },
        },
      },
      "work"
    );

    expect(merged.clientId).toBe("work-id");
    expect(merged.clientSecret).toBe("work-secret");
    expect(merged.dmPolicy).toBe("allowlist");
    expect(merged.allowFrom).toEqual(["u1"]);
    expect(merged.textChunkLimit).toBe(2000);
  });

  it("promotes legacy single-account root config into accounts.default", () => {
    const migrated = moveDingtalkSingleAccountConfigToDefaultAccount({
      channels: {
        dingtalk: {
          enabled: true,
          clientId: "base-id",
          clientSecret: "base-secret",
          enableAICard: false,
        },
      },
    });

    expect(migrated.channels?.dingtalk).toEqual({
      enabled: true,
      accounts: {
        default: {
          clientId: "base-id",
          clientSecret: "base-secret",
          enableAICard: false,
        },
      },
    });
  });
});
