import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const hostRoot = path.resolve(__dirname, "..");
const distRoot = path.join(hostRoot, "dist");
const sharedRoot = path.join(distRoot, "vendor", "shared");
const SHARED_SPECIFIER = "@openclaw-china/shared";
const SHARED_IMPORT_RE = /(["'])(@openclaw-china\/shared(?:\/[^"'\\\r\n]+)?)\1/g;

function isFile(filePath) {
  try {
    return fs.statSync(filePath).isFile();
  } catch {
    return false;
  }
}

function toImportPath(filePath) {
  return filePath.split(path.sep).join("/");
}

function resolveSharedTarget(specifier) {
  if (specifier === SHARED_SPECIFIER) {
    return path.join(sharedRoot, "index.js");
  }

  const suffix = specifier.slice(SHARED_SPECIFIER.length + 1);
  const base = path.join(sharedRoot, ...suffix.split("/"));
  const candidates = path.extname(base)
    ? [base]
    : [`${base}.js`, path.join(base, "index.js"), base];

  for (const candidate of candidates) {
    if (isFile(candidate)) {
      return candidate;
    }
  }

  throw new Error(`Unable to resolve ${specifier} inside ${sharedRoot}`);
}

function walkJsFiles(rootDir) {
  const files = [];
  const stack = [rootDir];

  while (stack.length > 0) {
    const current = stack.pop();
    if (!current || !fs.existsSync(current)) {
      continue;
    }
    for (const entry of fs.readdirSync(current, { withFileTypes: true })) {
      const entryPath = path.join(current, entry.name);
      if (entry.isDirectory()) {
        stack.push(entryPath);
        continue;
      }
      if (entry.isFile() && entry.name.endsWith(".js")) {
        files.push(entryPath);
      }
    }
  }

  return files;
}

function rewriteFile(filePath) {
  const normalizedSharedRoot = `${path.resolve(sharedRoot)}${path.sep}`;
  if (path.resolve(filePath).startsWith(normalizedSharedRoot)) {
    return 0;
  }

  const original = fs.readFileSync(filePath, "utf8");
  let replacements = 0;
  const rewritten = original.replace(SHARED_IMPORT_RE, (_match, quote, specifier) => {
    const target = resolveSharedTarget(specifier);
    let relativeTarget = path.relative(path.dirname(filePath), target);
    if (!relativeTarget.startsWith(".")) {
      relativeTarget = `./${relativeTarget}`;
    }
    replacements += 1;
    return `${quote}${toImportPath(relativeTarget)}${quote}`;
  });

  if (replacements > 0 && rewritten !== original) {
    fs.writeFileSync(filePath, rewritten, "utf8");
  }

  return replacements;
}

function main() {
  if (!isFile(path.join(distRoot, "index.js"))) {
    throw new Error(`Missing build output at ${path.join(distRoot, "index.js")}`);
  }
  if (!isFile(path.join(sharedRoot, "index.js"))) {
    throw new Error(`Missing shared runtime entry at ${path.join(sharedRoot, "index.js")}`);
  }

  let rewrittenFiles = 0;
  let rewrittenImports = 0;

  for (const filePath of walkJsFiles(distRoot)) {
    const replacements = rewriteFile(filePath);
    if (replacements <= 0) {
      continue;
    }
    rewrittenFiles += 1;
    rewrittenImports += replacements;
  }

  console.log(
    `[rewrite-shared-imports] rewrote ${rewrittenImports} import(s) across ${rewrittenFiles} file(s)`
  );
}

main();
