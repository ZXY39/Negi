import type { PluginRuntime } from "./types.js";

let runtime: PluginRuntime | null = null;

export function setWecomKfRuntime(next: PluginRuntime): void {
  runtime = next;
}

export function getWecomKfRuntime(): PluginRuntime {
  if (!runtime) {
    throw new Error("WeCom KF runtime not initialized.");
  }
  return runtime;
}

export function tryGetWecomKfRuntime(): PluginRuntime | null {
  return runtime;
}

export function clearWecomKfRuntime(): void {
  runtime = null;
}
