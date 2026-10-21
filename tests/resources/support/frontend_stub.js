"use strict";

// 共享 DOM 桩：只提供浏览器语义里各测试都用得到的那一层，不试图替代真实 DOM。
// 测试文件按需继承并覆盖 querySelector/querySelectorAll 等钩子；默认行为必须
// 保持“找不到就返回 null/[]”，否则原本期望空结果的用例会被桩凭空造出节点。

function makeClassList(element) {
    const read = () => new Set(String(element.className || "").split(/\s+/).filter(Boolean));
    const write = (classes) => {
        element.className = [...classes].join(" ");
    };
    return {
        add: (...tokens) => {
            const classes = read();
            tokens.forEach((token) => classes.add(token));
            write(classes);
        },
        remove: (...tokens) => {
            const classes = read();
            tokens.forEach((token) => classes.delete(token));
            write(classes);
        },
        contains: (token) => read().has(token),
        toggle: (token, force) => {
            const classes = read();
            const has = classes.has(token);
            const next = typeof force === "boolean" ? force : !has;
            if (next) classes.add(token);
            else classes.delete(token);
            write(classes);
            return next;
        },
        replace: (oldToken, newToken) => {
            const classes = read();
            if (!classes.delete(oldToken)) return false;
            classes.add(newToken);
            write(classes);
            return true;
        },
        item: (index) => [...read()][index] || null,
        get length() {
            return read().size;
        },
        toString: () => [...read()].join(" "),
    };
}

function datasetKey(name) {
    return name
        .slice(5)
        .replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
}

class StubElement {}

class StubHTMLElement extends StubElement {
    constructor(tag = "DIV", attrs = {}) {
        super();
        if (tag && typeof tag === "object") {
            attrs = tag;
            tag = attrs.tagName || "DIV";
        }
        this.tagName = String(tag || "DIV").toUpperCase();
        this.className = "";
        this.id = "";
        this.hidden = false;
        this.open = false;
        this.disabled = false;
        this.checked = false;
        this.value = "";
        this.textContent = "";
        this.dataset = {};
        this.attributes = { ...attrs };
        this.children = [];
        this.style = {};
        this.scrollTop = 0;
        this.scrollLeft = 0;
        this.scrollHeight = 0;
        this.scrollWidth = 0;
        this.clientHeight = 0;
        this.clientWidth = 0;
        this._innerHTML = "";
        this._listeners = {};
        this._qs = {};
        this._qsAll = {};
        this.parentElement = null;
        this.parentNode = null;
        this.classList = makeClassList(this);
    }

    get innerHTML() {
        return this._innerHTML;
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
    }

    get lastElementChild() {
        return this.children.length ? this.children[this.children.length - 1] : null;
    }

    get firstElementChild() {
        return this.children[0] || null;
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === "id") this.id = String(value);
        if (name.startsWith("data-")) this.dataset[datasetKey(name)] = String(value);
    }

    getAttribute(name) {
        if (name === "class" && !Object.prototype.hasOwnProperty.call(this.attributes, name)) {
            return this.className || null;
        }
        return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }

    hasAttribute(name) {
        return Object.prototype.hasOwnProperty.call(this.attributes, name);
    }

    removeAttribute(name) {
        delete this.attributes[name];
        if (name.startsWith("data-")) delete this.dataset[datasetKey(name)];
    }

    toggleAttribute(name, force) {
        const has = this.hasAttribute(name);
        const next = typeof force === "boolean" ? force : !has;
        if (next) this.setAttribute(name, "");
        else this.removeAttribute(name);
        return next;
    }

    appendChild(child) {
        if (child && typeof child === "object") {
            if (child.parentNode && child.parentNode !== this && typeof child.parentNode.removeChild === "function") {
                child.parentNode.removeChild(child);
            }
            child.parentNode = this;
            child.parentElement = this;
        }
        this.children.push(child);
        return child;
    }

    insertBefore(child, ref) {
        if (!ref) return this.appendChild(child);
        const index = this.children.indexOf(ref);
        if (index < 0) return this.appendChild(child);
        if (child && typeof child === "object") {
            if (child.parentNode && child.parentNode !== this && typeof child.parentNode.removeChild === "function") {
                child.parentNode.removeChild(child);
            }
            child.parentNode = this;
            child.parentElement = this;
        }
        this.children.splice(index, 0, child);
        return child;
    }

    removeChild(child) {
        const index = this.children.indexOf(child);
        if (index >= 0) this.children.splice(index, 1);
        if (child && typeof child === "object") {
            if (child.parentNode === this) child.parentNode = null;
            if (child.parentElement === this) child.parentElement = null;
        }
        return child;
    }

    remove() {
        if (this.parentNode && typeof this.parentNode.removeChild === "function") {
            this.parentNode.removeChild(this);
        }
    }

    querySelector() {
        return null;
    }

    querySelectorAll() {
        return [];
    }

    getBoundingClientRect() {
        return { top: 0, left: 0, right: 0, bottom: 10, width: 0, height: 10 };
    }

    closest(selector) {
        const needle = String(selector || "").replace(/^\./, "");
        let node = this;
        while (node) {
            const classes = String(node.className || "").split(/\s+/);
            if (classes.includes(needle) || String(node.tagName || "").toLowerCase() === needle.toLowerCase()) {
                return node;
            }
            node = node.parentElement;
        }
        return null;
    }

    matches(selector) {
        const needle = String(selector || "").replace(/^\./, "");
        return String(this.className || "").split(/\s+/).includes(needle)
            || String(this.tagName || "").toLowerCase() === needle.toLowerCase();
    }

    contains(node) {
        if (node === this) return true;
        return this.children.some((child) => child && typeof child.contains === "function" && child.contains(node));
    }

    addEventListener(type, handler) {
        (this._listeners[type] = this._listeners[type] || []).push(handler);
    }

    removeEventListener(type, handler) {
        const listeners = this._listeners[type] || [];
        this._listeners[type] = listeners.filter((item) => item !== handler);
    }

    dispatchEvent(event) {
        const type = event && event.type;
        (this._listeners[type] || []).forEach((handler) => handler(event));
        return true;
    }

    click() {
        this.dispatchEvent({ type: "click", target: this });
    }

    focus() {
        this._focused = true;
    }

    blur() {
        this._focused = false;
    }

    scrollIntoView() {}

    insertAdjacentHTML() {}
}

class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}
class StubHTMLSelectElement extends StubHTMLElement {}

class StubDocument {
    constructor() {
        this.body = new StubHTMLElement("BODY");
        this.documentElement = new StubHTMLElement("HTML");
        this.activeElement = null;
        this._byId = {};
        this._listeners = {};
        this._qs = {};
        this._qsAll = {};
    }

    getElementById(id) {
        return this._byId[id] || null;
    }

    querySelector(selector) {
        return this._qs[selector] || null;
    }

    querySelectorAll(selector) {
        return this._qsAll[selector] || [];
    }

    createElement(tag) {
        return new StubHTMLElement(String(tag || "div").toUpperCase());
    }

    createTextNode(text) {
        const node = new StubHTMLElement("#text");
        node.textContent = String(text);
        return node;
    }

    addEventListener(type, handler) {
        (this._listeners[type] = this._listeners[type] || []).push(handler);
    }

    removeEventListener(type, handler) {
        const listeners = this._listeners[type] || [];
        this._listeners[type] = listeners.filter((item) => item !== handler);
    }

    dispatchEvent(event) {
        const type = event && event.type;
        (this._listeners[type] || []).forEach((handler) => handler(event));
        return true;
    }
}

module.exports = {
    StubElement,
    StubHTMLElement,
    StubDocument,
    StubHTMLButtonElement,
    StubHTMLInputElement,
    StubHTMLTextAreaElement,
    StubHTMLSelectElement,
    makeClassList,
};
