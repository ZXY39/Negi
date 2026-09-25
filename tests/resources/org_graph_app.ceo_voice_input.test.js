const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const APP_PATH = "g3ku/web/frontend/org_graph_app.js";
const HTML_PATH = "g3ku/web/frontend/org_graph.html";
// Windows 上经 autocrlf 检出为 CRLF；断言按 LF 匹配，读入后统一归一化行尾。
const APP_CODE = fs.readFileSync(APP_PATH, "utf8").replace(/\r\n/g, "\n");
const APP_HTML = fs.readFileSync(HTML_PATH, "utf8").replace(/\r\n/g, "\n");

class StubElement {
    constructor() {
        // 类名断言要看得见，所以用 Set 记账而不是空实现。
        this.classes = new Set();
    }
}

class StubHTMLElement extends StubElement {
    constructor() {
        super();
        this.hidden = false;
        this.disabled = false;
        this.value = "";
        this.textContent = "";
        this.dataset = {};
        this.style = {
            setProperty(name, value) {
                this[name] = String(value);
            },
            removeProperty(name) {
                delete this[name];
            },
        };
        this.attributes = {};
        this.classList = {
            add: (...tokens) => tokens.forEach((token) => this.classes.add(token)),
            remove: (...tokens) => tokens.forEach((token) => this.classes.delete(token)),
            contains: (token) => this.classes.has(token),
            toggle: (token, force) => {
                const shouldAdd = force == null ? !this.classes.has(token) : !!force;
                if (shouldAdd) this.classes.add(token);
                else this.classes.delete(token);
                return shouldAdd;
            },
        };
    }
    addEventListener() {}
    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }
    getAttribute(name) {
        return this.attributes[name] ?? null;
    }
    focus() {}
}

class StubHTMLButtonElement extends StubHTMLElement {}
class StubHTMLInputElement extends StubHTMLElement {}
class StubHTMLTextAreaElement extends StubHTMLElement {}

class StubDocument {
    getElementById() {
        return null;
    }
    querySelector() {
        return null;
    }
    querySelectorAll() {
        return [];
    }
    addEventListener() {}
}

// Blob 把分片留着，测试就能真的读回 WAV 头。
// vm 里的 ArrayBuffer 与宿主不同 realm，instanceof 不成立，只能按内部标签判类型。
class StubBlob {
    constructor(parts, options = {}) {
        this.parts = parts;
        this.type = options.type || "";
        const chunks = parts.map((part) =>
            Object.prototype.toString.call(part) === "[object ArrayBuffer]"
                ? new Uint8Array(part)
                : new TextEncoder().encode(String(part))
        );
        this.size = chunks.reduce((total, chunk) => total + chunk.length, 0);
        this.bytes = new Uint8Array(this.size);
        let offset = 0;
        for (const chunk of chunks) {
            this.bytes.set(chunk, offset);
            offset += chunk.length;
        }
    }
}

function loadApp(contextExtra = {}) {
    const context = {
        console,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        queueMicrotask,
        navigator: { clipboard: { writeText: async () => {} } },
        location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        document: new StubDocument(),
        window: {},
        Element: StubElement,
        HTMLElement: StubHTMLElement,
        HTMLButtonElement: StubHTMLButtonElement,
        HTMLInputElement: StubHTMLInputElement,
        HTMLTextAreaElement: StubHTMLTextAreaElement,
        URLSearchParams,
        URL,
        AbortController,
        Blob: StubBlob,
        TextEncoder,
        // 另册加载的模块，真实页面里是全局；草稿同步会用到它。
        ApiClient: {
            getActiveSessionId: () => "web:test",
            transcribeCeoVoice: async () => ({ ok: true, text: "" }),
            setCeoComposerDraft: async () => ({}),
            saveCeoComposerDraft: async () => ({}),
        },
        fetch: async () => ({ ok: true, json: async () => ({}) }),
        lucide: { createIcons() {} },
        marked: { parse: (value) => String(value) },
        DOMPurify: { sanitize: (value) => String(value) },
        structuredClone: global.structuredClone,
        performance: { now: () => 0 },
        requestAnimationFrame: (callback) => {
            callback();
            return 1;
        },
        cancelAnimationFrame: () => {},
        WebSocket: function WebSocket() {},
        addEventListener() {},
        removeEventListener() {},
        ...contextExtra,
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${APP_CODE}
        this.__testExports = {
            S,
            U,
            encodePcmWav,
            voiceFailureText,
            voiceCaptureSupported,
            appendVoiceTextToComposer,
            syncCeoVoiceButton,
            handleCeoVoiceClick,
        };`,
        context
    );
    context.__testExports.__context = context;
    return context.__testExports;
}

function readUint16(bytes, offset) {
    return bytes[offset] | (bytes[offset + 1] << 8);
}

function readUint32(bytes, offset) {
    return (bytes[offset] | (bytes[offset + 1] << 8) | (bytes[offset + 2] << 16) | (bytes[offset + 3] << 24)) >>> 0;
}

test("composer markup carries a mic button next to the attach button", () => {
    const attachAt = APP_HTML.indexOf('id="ceo-attach-btn"');
    const voiceAt = APP_HTML.indexOf('id="ceo-voice-btn"');
    assert.ok(attachAt >= 0, "attach button missing");
    assert.ok(voiceAt >= 0, "voice button missing");
    assert.ok(voiceAt > attachAt, "voice button should sit after the attach button");
    const block = APP_HTML.slice(voiceAt, voiceAt + 220);
    assert.match(block, /data-lucide="mic"/);
    assert.match(block, /aria-pressed="false"/);
});

test("encodePcmWav writes a 16-bit mono header the binary can read", () => {
    const { encodePcmWav } = loadApp();
    const samples = new Float32Array([0, 0.5, -0.5, 1, -1]);
    const blob = encodePcmWav(samples, 16000);
    const bytes = blob.bytes;

    assert.equal(String.fromCharCode(...bytes.slice(0, 4)), "RIFF");
    assert.equal(String.fromCharCode(...bytes.slice(8, 12)), "WAVE");
    assert.equal(String.fromCharCode(...bytes.slice(12, 16)), "fmt ");
    assert.equal(readUint32(bytes, 16), 16, "fmt chunk size");
    assert.equal(readUint16(bytes, 20), 1, "PCM format tag");
    assert.equal(readUint16(bytes, 22), 1, "mono");
    assert.equal(readUint32(bytes, 24), 16000, "sample rate");
    assert.equal(readUint32(bytes, 28), 32000, "byte rate");
    assert.equal(readUint16(bytes, 32), 2, "block align");
    assert.equal(readUint16(bytes, 34), 16, "bits per sample");
    assert.equal(String.fromCharCode(...bytes.slice(36, 40)), "data");
    assert.equal(readUint32(bytes, 40), samples.length * 2);
    assert.equal(readUint32(bytes, 4), 36 + samples.length * 2, "RIFF size matches payload");
    assert.equal(blob.type, "audio/wav");

    // 满幅不溢出、静音为 0。
    assert.equal(readUint16(bytes, 44) & 0xffff, 0);
    assert.ok(readUint16(bytes, 44 + 6) === 32767 || readUint16(bytes, 44 + 6) === 0x7fff);
});

test("voice capture reports unsupported until every browser piece exists", () => {
    const bare = loadApp();
    assert.equal(bare.voiceCaptureSupported(), false);

    const supported = loadApp({
        navigator: {
            clipboard: { writeText: async () => {} },
            mediaDevices: { getUserMedia: async () => ({ getTracks: () => [] }) },
        },
        MediaRecorder: function MediaRecorder() {},
        OfflineAudioContext: function OfflineAudioContext() {},
    });
    assert.equal(supported.voiceCaptureSupported(), true);
});

test("not-ready codes are turned into the command the operator has to run", () => {
    const { voiceFailureText } = loadApp();
    assert.match(voiceFailureText({ error_code: "stt_model_missing" }), /g3ku stt prepare/);
    assert.match(voiceFailureText({ error_code: "stt_binary_missing" }), /g3ku stt prepare/);
    assert.match(voiceFailureText({ error_code: "stt_disabled" }), /stt\.enabled/);
    assert.match(voiceFailureText({ error_code: "stt_silent" }), /静音/);
    assert.match(voiceFailureText({ error_code: "audio_decoder_missing" }), /ffmpeg/);
    assert.equal(voiceFailureText({ error_code: "", error: "自定义原因" }), "自定义原因");
    assert.match(voiceFailureText(null), /未知原因/);
});

test("transcribed text appends to the draft instead of replacing it", () => {
    const { U, appendVoiceTextToComposer } = loadApp();
    U.ceoInput = new StubHTMLTextAreaElement();
    U.ceoInput.value = "先看这个，";
    appendVoiceTextToComposer("然后再说别的");
    assert.equal(U.ceoInput.value, "先看这个，然后再说别的");

    U.ceoInput.value = "结尾有空格 ";
    appendVoiceTextToComposer("接上");
    assert.equal(U.ceoInput.value, "结尾有空格 接上");

    U.ceoInput.value = "";
    appendVoiceTextToComposer("第一段");
    assert.equal(U.ceoInput.value, "第一段");

    // 拉丁两侧才补空格，中文标点后面不补。
    U.ceoInput.value = "hello";
    appendVoiceTextToComposer("world");
    assert.equal(U.ceoInput.value, "hello world");
    U.ceoInput.value = "hello,";
    appendVoiceTextToComposer("world");
    assert.equal(U.ceoInput.value, "hello, world");
});

test("recording state drives aria-pressed, icon and the red class", () => {
    const { S, U, syncCeoVoiceButton } = loadApp();
    U.ceoVoiceBtn = new StubHTMLButtonElement();

    S.ceoVoice = null;
    syncCeoVoiceButton();
    assert.equal(U.ceoVoiceBtn.attributes["aria-pressed"], "false");
    assert.match(U.ceoVoiceBtn.innerHTML, /data-lucide="mic"/);
    assert.equal(U.ceoVoiceBtn.classList.contains("is-recording"), false);

    S.ceoVoice = { chunks: [], stream: { getTracks: () => [] }, recorder: { stop() {} } };
    syncCeoVoiceButton();
    assert.equal(U.ceoVoiceBtn.attributes["aria-pressed"], "true");
    assert.match(U.ceoVoiceBtn.innerHTML, /data-lucide="mic-off"/);
    assert.equal(U.ceoVoiceBtn.classList.contains("is-recording"), true);
});
