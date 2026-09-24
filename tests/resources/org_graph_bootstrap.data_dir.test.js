import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import test from "node:test";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const BOOTSTRAP_SCRIPT = path.join(REPO_ROOT, "g3ku", "web", "frontend", "org_graph_bootstrap.js");

// boot 脚本把 refreshStatus() 以 void 起出去，等两轮宏任务让那条 Promise 链落地。
async function flush() {
  for (let index = 0; index < 3; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

function makeElement(id) {
  const classes = new Set();
  return {
    id,
    hidden: false,
    value: "",
    checked: false,
    disabled: false,
    placeholder: "",
    textContent: "",
    listeners: {},
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
      contains: (name) => classes.has(name),
      classes,
    },
    setAttribute() {},
    getAttribute() {
      return null;
    },
    addEventListener(type, handler) {
      (this.listeners[type] ||= []).push(handler);
    },
    dispatch(type, event = {}) {
      for (const handler of this.listeners[type] || []) handler(event);
    },
  };
}

function loadBoot({ status, pickedDir = null }) {
  const elements = new Map();
  const document = {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement(id));
      return elements.get(id);
    },
    addEventListener(type, handler) {
      (this.listeners ||= {});
      document.listeners[type] = handler;
    },
  };
  const calls = { setup: [] };
  const sandbox = {
    document,
    window: {},
    ApiClient: {
      async getBootstrapStatus() {
        return status;
      },
      async setupBootstrap(payload) {
        calls.setup.push(payload);
        return { ...status, mode: "locked" };
      },
      async unlockBootstrap() {
        return status;
      },
      async pickBootstrapDataDir() {
        calls.pickCount = (calls.pickCount || 0) + 1;
        return pickedDir;
      },
    },
    CustomEvent: class {
      constructor(name, detail) {
        this.name = name;
        this.detail = detail;
      }
    },
    setTimeout,
  };
  sandbox.window = { dispatchEvent: () => {}, lucide: null };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(BOOTSTRAP_SCRIPT, "utf8"), sandbox);
  document.listeners.DOMContentLoaded();
  return { elements: document.getElementById, calls, sandbox };
}

test("setup 表单带数据目录输入，并把 data_dir 一并提交", async () => {
  const { elements, calls } = loadBoot({
    status: {
      mode: "setup",
      data_root: { source: "default", data_root: "C:\\install", default_root: "C:\\install" },
    },
  });
  await flush();
  const dataDir = elements("boot-setup-data-dir");

  assert.equal(dataDir.hidden, false);
  assert.equal(dataDir.placeholder, "默认：C:\\install");

  dataDir.value = "  D:\\G3KU-Data  ";
  elements("boot-setup-form").dispatch("submit", { preventDefault() {} });
  await flush();

  assert.equal(calls.setup.length, 1);
  assert.equal(calls.setup[0].data_dir, "D:\\G3KU-Data");
});

test("已指定过数据目录时输入框回填当前根，占位仍提示默认根", async () => {
  const { elements } = loadBoot({
    status: {
      mode: "setup",
      data_root: { source: "pointer", data_root: "D:\\G3KU-Data", default_root: "C:\\install" },
    },
  });
  await flush();
  const dataDir = elements("boot-setup-data-dir");

  assert.equal(dataDir.value, "D:\\G3KU-Data");
  assert.match(dataDir.placeholder, /^默认：C:/);
});

test("解锁态不渲染数据目录输入", async () => {
  const { elements } = loadBoot({
    status: {
      mode: "unlocked",
      data_root: { source: "default", data_root: "C:\\install", default_root: "C:\\install" },
    },
  });
  await flush();
  const dataDir = elements("boot-setup-data-dir");

  assert.equal(dataDir.placeholder, "");
  assert.equal(elements("boot-setup-form").hidden, true);
});

test("服务端说可用时才露出「选择」按钮", async () => {
  const hidden = loadBoot({
    status: {
      mode: "setup",
      data_root: { source: "default", default_root: "C:\\install" },
      dir_picker: { available: false },
    },
  });
  const shown = loadBoot({
    status: {
      mode: "setup",
      data_root: { source: "default", default_root: "C:\\install" },
      dir_picker: { available: true },
    },
  });
  await flush();

  assert.equal(hidden.elements("boot-setup-data-dir-pick").hidden, true);
  assert.equal(shown.elements("boot-setup-data-dir-pick").hidden, false);
});

test("点「选择」把本机目录框返回的路径填进输入框", async () => {
  const { elements, calls } = loadBoot({
    status: {
      mode: "setup",
      data_root: { source: "default", default_root: "C:\\install" },
      dir_picker: { available: true },
    },
    pickedDir: { path: "D:\\G3KU-Data", cancelled: false },
  });
  await flush();
  const pick = elements("boot-setup-data-dir-pick");

  pick.dispatch("click");
  await flush();

  assert.equal(calls.pickCount, 1);
  assert.equal(elements("boot-setup-data-dir").value, "D:\\G3KU-Data");
});

test("取消目录框不动输入框", async () => {
  const { elements } = loadBoot({
    status: {
      mode: "setup",
      data_root: { source: "default", default_root: "C:\\install" },
      dir_picker: { available: true },
    },
    pickedDir: { path: "", cancelled: true },
  });
  await flush();
  const dataDir = elements("boot-setup-data-dir");
  dataDir.value = "C:\\keep-me";

  elements("boot-setup-data-dir-pick").dispatch("click");
  await flush();

  assert.equal(dataDir.value, "C:\\keep-me");
});

test("首屏文案为「项目数据地址设置」，按钮是 folder 图标且无可见中文", () => {
  const html = fs.readFileSync(path.join(REPO_ROOT, "g3ku", "web", "frontend", "org_graph.html"), "utf8");
  const script = fs.readFileSync(BOOTSTRAP_SCRIPT, "utf8");
  const button = html.match(/<button id="boot-setup-data-dir-pick"[\s\S]*?<\/button>/);

  assert.ok(button, "缺少 boot-setup-data-dir-pick 按钮");
  assert.match(html, /<label class="resource-field-label" for="boot-setup-data-dir">项目数据地址设置<\/label>/);
  assert.match(button[0], /data-lucide="folder"/);
  assert.match(button[0], /aria-label="选择数据目录"/);
  assert.doesNotMatch(button[0], />\s*选择\s*</);
  assert.match(script, /getElementById\("boot-setup-data-dir-pick"\)/);
  assert.match(script, /addEventListener\("click", handlePickDataDir\)/);
});

test("按钮高度跟随输入框，不写死 min-height", () => {
  const css = fs.readFileSync(path.join(REPO_ROOT, "g3ku", "web", "frontend", "org_graph.css"), "utf8");
  const row = css.match(/\.boot-dir-row \{[\s\S]*?\}/)[0];
  const button = css.match(/\.boot-dir-pick \{[\s\S]*?\}/)[0];

  assert.match(row, /align-items:\s*stretch/);
  assert.doesNotMatch(button, /min-height/);
});
