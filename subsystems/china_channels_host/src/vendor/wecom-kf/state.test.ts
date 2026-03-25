import os from "node:os";
import path from "node:path";
import { mkdtemp, rm } from "node:fs/promises";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  flushWecomKfStateForTests,
  getStoredCursor,
  markProcessedMessage,
  setStoredCursor,
  setWecomKfStateFilePathForTests,
} from "./state.js";

let tempDir = "";
let stateFilePath = "";

beforeEach(async () => {
  tempDir = await mkdtemp(path.join(os.tmpdir(), "wecom-kf-state-"));
  stateFilePath = path.join(tempDir, "state.json");
  setWecomKfStateFilePathForTests(stateFilePath);
});

afterEach(async () => {
  await flushWecomKfStateForTests();
  setWecomKfStateFilePathForTests();
  if (tempDir) {
    await rm(tempDir, { recursive: true, force: true });
  }
  tempDir = "";
  stateFilePath = "";
});

describe("wecom-kf state", () => {
  it("persists cursors and processed msgids across reloads", async () => {
    await setStoredCursor("default:wk-test", "cursor-1");
    expect(await markProcessedMessage("msg-1")).toBe(true);
    await flushWecomKfStateForTests();

    setWecomKfStateFilePathForTests(stateFilePath);

    expect(await getStoredCursor("default:wk-test")).toBe("cursor-1");
    expect(await markProcessedMessage("msg-1")).toBe(false);
    expect(await markProcessedMessage("msg-2")).toBe(true);
  });
});
