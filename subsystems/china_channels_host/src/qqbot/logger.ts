// @ts-nocheck
/**
 * QQ Bot 插件日志工具
 */

export { createLogger, type Logger, type LogLevel, type LoggerOptions } from "../shared/index.js";

import { createLogger } from "../shared/index.js";

export const qqbotLogger = createLogger("qqbot");
