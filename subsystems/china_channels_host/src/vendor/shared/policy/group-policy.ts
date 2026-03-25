/**
 * 群组策略引擎
 *
 * 实现 open/allowlist/disabled 策略检查
 */

import type { PolicyCheckResult } from "./dm-policy.js";

/**
 * 群组策略类型
 * - open: 允许所有群聊消息
 * - allowlist: 仅允许白名单中的群组
 * - disabled: 禁用所有群聊消息
 */
export type GroupPolicyType = "open" | "allowlist" | "disabled";

/**
 * 群组策略检查参数
 */
export interface GroupPolicyCheckParams {
  /** 群组策略类型 */
  groupPolicy: GroupPolicyType;
  /** 会话 ID（群组 ID） */
  conversationId: string;
  /** 群组白名单（allowlist 策略时使用） */
  groupAllowFrom?: string[];
  /** 是否要求 @提及机器人 */
  requireMention: boolean;
  /** 是否 @提及了机器人 */
  mentionedBot: boolean;
}

/**
 * 检查群聊策略
 *
 * @param params 检查参数
 * @returns 策略检查结果
 *
 * @example
 * ```ts
 * // 禁用策略
 * checkGroupPolicy({ groupPolicy: "disabled", conversationId: "g1", requireMention: false, mentionedBot: false });
 * // => { allowed: false, reason: "group messages disabled" }
 *
 * // 开放策略 + 要求 @提及
 * checkGroupPolicy({ groupPolicy: "open", conversationId: "g1", requireMention: true, mentionedBot: false });
 * // => { allowed: false, reason: "message did not mention bot" }
 *
 * // 白名单策略
 * checkGroupPolicy({ groupPolicy: "allowlist", conversationId: "g1", groupAllowFrom: ["g1"], requireMention: false, mentionedBot: false });
 * // => { allowed: true }
 * ```
 */
export function checkGroupPolicy(params: GroupPolicyCheckParams): PolicyCheckResult {
  const { groupPolicy, conversationId, groupAllowFrom = [], requireMention, mentionedBot } = params;

  // 首先检查群聊策略
  switch (groupPolicy) {
    case "disabled":
      // 禁用策略：拒绝所有群聊消息
      return {
        allowed: false,
        reason: "group messages disabled",
      };

    case "allowlist":
      // 白名单策略：仅允许 groupAllowFrom 中的群组
      if (!groupAllowFrom.includes(conversationId)) {
        return {
          allowed: false,
          reason: `group ${conversationId} not in allowlist`,
        };
      }
      break;

    case "open":
      // 开放策略：允许所有群聊
      break;

    default:
      break;
  }

  // 然后检查 @提及要求
  if (requireMention && !mentionedBot) {
    return {
      allowed: false,
      reason: "message did not mention bot",
    };
  }

  return { allowed: true };
}
