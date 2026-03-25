import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

type WecomManifest = {
  skills?: string[];
};

type WecomPackageJson = {
  files?: string[];
};

const packageRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const manifestPath = join(packageRoot, "openclaw.plugin.json");
const packageJsonPath = join(packageRoot, "package.json");

function readJsonFile<T>(filePath: string): T {
  return JSON.parse(readFileSync(filePath, "utf8")) as T;
}

describe("wecom plugin skill packaging", () => {
  it("declares plugin-local skill directories in the OpenClaw manifest", () => {
    const manifest = readJsonFile<WecomManifest>(manifestPath);

    expect(manifest.skills).toEqual(["./skills"]);
    for (const relativePath of manifest.skills ?? []) {
      expect(existsSync(join(packageRoot, relativePath))).toBe(true);
    }
    expect(existsSync(join(packageRoot, "skills", "wecom-doc", "SKILL.md"))).toBe(true);
  });

  it("includes plugin-local skills in the published package", () => {
    const packageJson = readJsonFile<WecomPackageJson>(packageJsonPath);

    expect(packageJson.files).toContain("skills");
  });
});
