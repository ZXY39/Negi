/**
 * DM 策略引擎
 *
 * 实现 open/pairing/allowlist 策略检查
 */

/**
 * DM 策略类型
 * - open: 允许所有单聊消息
 * - pairing: 配对模式（允许所有，配对逻辑由上层处理）
 * - allowlist: 仅允许白名单中的发送者
 */
export type DmPolicyType = "open" | "pairing" | "allowlist";

/**
 * 策略检查结果
 */
export interface PolicyCheckResult {
  /** 是否允许处理该消息 */
  allowed: boolean;
  /** 拒绝原因（如果被拒绝） */
  reason?: string;
}

/**
 * DM 策略检查参数
 */
export interface DmPolicyCheckParams {
  /** DM 策略类型 */
  dmPolicy: DmPolicyType;
  /** 发送者 ID */
  senderId: string;
  /** 白名单（allowlist 策略时使用） */
  allowFrom?: string[];
}

/**
 * 检查单聊策略
 *
 * @param params 检查参数
 * @returns 策略检查结果
 *
 * @example
 * ```ts
 * // 开放策略
 * checkDmPolicy({ dmPolicy: "open", senderId: "user1" });
 * // => { allowed: true }
 *
 * // 白名单策略
 * checkDmPolicy({ dmPolicy: "allowlist", senderId: "user1", allowFrom: ["user1", "user2"] });
 * // => { allowed: true }
 *
 * checkDmPolicy({ dmPolicy: "allowlist", senderId: "user3", allowFrom: ["user1", "user2"] });
 * // => { allowed: false, reason: "sender user3 not in DM allowlist" }
 * ```
 */
export function checkDmPolicy(params: DmPolicyCheckParams): PolicyCheckResult {
  const { dmPolicy, senderId, allowFrom = [] } = params;

  switch (dmPolicy) {
    case "open":
      // 开放策略：允许所有单聊消息
      return { allowed: true };

    case "pairing":
      // 配对策略：允许所有单聊消息（配对逻辑由上层处理）
      return { allowed: true };

    case "allowlist":
      // 白名单策略：仅允许 allowFrom 中的发送者
      if (allowFrom.includes(senderId)) {
        return { allowed: true };
      }
      return {
        allowed: false,
        reason: `sender ${senderId} not in DM allowlist`,
      };

    default:
      return { allowed: true };
  }
}
