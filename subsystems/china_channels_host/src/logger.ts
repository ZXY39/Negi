// @ts-nocheck
export type HostLogger = {
  info: (msg: string) => void;
  warn: (msg: string) => void;
  error: (msg: string) => void;
  debug: (msg: string) => void;
};

export function createLogger(scope: string): HostLogger {
  const prefix = `[china-host:${scope}]`;
  return {
    info: (msg) => console.log(`${prefix} ${msg}`),
    warn: (msg) => console.warn(`${prefix} ${msg}`),
    error: (msg) => console.error(`${prefix} ${msg}`),
    debug: (msg) => console.debug(`${prefix} ${msg}`),
  };
}
