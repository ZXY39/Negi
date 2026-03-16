// @ts-nocheck
/**
 * 飞书插件日志工具
 *
 * 从 ../shared/index.js 重新导出，保持一致
 */

export { createLogger, type Logger, type LogLevel, type LoggerOptions } from "../shared/index.js";

import { createLogger } from "../shared/index.js";

/** 默认飞书日志器 */
export const feishuLogger = createLogger("feishu");
