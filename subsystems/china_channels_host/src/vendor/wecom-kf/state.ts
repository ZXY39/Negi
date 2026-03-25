import { mkdir, readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

import type { WecomKfAccountState, WecomKfPersistedState } from "./types.js";

const DEDUP_TTL_MS = 10 * 60 * 1000;
const DEFAULT_STATE_FILE = join(homedir(), ".openclaw", "wecom-kf", "data", "state.json");

let stateFilePath = DEFAULT_STATE_FILE;
let cachedState: WecomKfPersistedState | null = null;
let loadingState: Promise<WecomKfPersistedState> | null = null;
let saveTimer: ReturnType<typeof setTimeout> | null = null;

function createEmptyState(): WecomKfPersistedState {
  return {
    version: 1,
    cursors: {},
    processedMsgIds: {},
    accounts: {},
  };
}

function pruneState(state: WecomKfPersistedState): void {
  const cutoff = Date.now() - DEDUP_TTL_MS;
  for (const [msgid, timestamp] of Object.entries(state.processedMsgIds)) {
    if (timestamp < cutoff) {
      delete state.processedMsgIds[msgid];
    }
  }
}

async function saveState(): Promise<void> {
  if (!cachedState) return;
  pruneState(cachedState);
  await mkdir(dirname(stateFilePath), { recursive: true });
  await writeFile(stateFilePath, `${JSON.stringify(cachedState, null, 2)}\n`, "utf8");
}

function scheduleSave(): void {
  if (saveTimer) return;
  saveTimer = setTimeout(() => {
    saveTimer = null;
    saveState().catch(() => {
      // Best-effort persistence.
    });
  }, 50);
}

async function loadState(): Promise<WecomKfPersistedState> {
  if (cachedState) return cachedState;
  if (loadingState) return loadingState;

  loadingState = (async () => {
    try {
      const raw = await readFile(stateFilePath, "utf8");
      const parsed = JSON.parse(raw) as Partial<WecomKfPersistedState>;
      cachedState = {
        version: 1,
        cursors: parsed.cursors ?? {},
        processedMsgIds: parsed.processedMsgIds ?? {},
        accounts: parsed.accounts ?? {},
      };
    } catch {
      cachedState = createEmptyState();
    }
    pruneState(cachedState);
    return cachedState;
  })();

  try {
    return await loadingState;
  } finally {
    loadingState = null;
  }
}

export async function getStoredCursor(key: string): Promise<string | undefined> {
  const state = await loadState();
  return state.cursors[key];
}

export async function setStoredCursor(key: string, cursor: string): Promise<void> {
  const state = await loadState();
  state.cursors[key] = cursor;
  scheduleSave();
}

export async function hasStoredCursor(key: string): Promise<boolean> {
  return Boolean(await getStoredCursor(key));
}

export async function markProcessedMessage(msgid: string): Promise<boolean> {
  const normalized = msgid.trim();
  if (!normalized) return false;
  const state = await loadState();
  pruneState(state);
  if (state.processedMsgIds[normalized]) {
    return false;
  }
  state.processedMsgIds[normalized] = Date.now();
  scheduleSave();
  return true;
}

export async function getAccountState(accountId: string): Promise<WecomKfAccountState> {
  const state = await loadState();
  return { ...(state.accounts[accountId] ?? {}) };
}

export async function updateAccountState(
  accountId: string,
  patch: Partial<WecomKfAccountState>
): Promise<WecomKfAccountState> {
  const state = await loadState();
  const current = state.accounts[accountId] ?? {};
  const next = { ...current, ...patch };
  state.accounts[accountId] = next;
  scheduleSave();
  return next;
}

export async function flushWecomKfStateForTests(): Promise<void> {
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = null;
  }
  await saveState();
}

export function setWecomKfStateFilePathForTests(nextPath?: string): void {
  stateFilePath = nextPath?.trim() || DEFAULT_STATE_FILE;
  cachedState = null;
  loadingState = null;
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = null;
  }
}
