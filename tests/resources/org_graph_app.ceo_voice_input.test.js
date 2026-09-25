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

class StubHTMLTextAreaElement extends StubHTMLElement {
    constructor() {
        super();
        // 光标插入要看得见选区，桩必须真的记着它。
        this.selectionStart = 0;
        this.selectionEnd = 0;
    }
    setSelectionRange(start, end) {
        this.selectionStart = start;
        this.selectionEnd = end;
    }
}

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

function makeStorage(seed = {}) {
    const data = { ...seed };
    return {
        getItem: (key) => (key in data ? data[key] : null),
        setItem: (key, value) => { data[key] = String(value); },
        removeItem: (key) => { delete data[key]; },
        __data: data,
    };
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
        localStorage: makeStorage(contextExtra.__localStorageSeed),
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
            syncCeoVoiceButton,
            syncCeoVoiceOptions,
            deliverVoiceText,
            insertVoiceTextAtCursor,
            restoreCeoVoiceAutoSend,
            ceoVoiceAutoSendEnabled,
            setCeoVoiceAutoSend,
            handleCeoVoiceClick,
            VOICE_AUTO_SEND_PREFIX,
            pickVoiceClip,
            stripVoiceTranscriptMarkers,
            buildCeoVoiceBubbleMarkup,
            formatVoiceDuration,
            handleCeoVoiceBubbleClick,
            renderStructuredChatAttachments,
            summarizeUploads,
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

    // 自动发送开关：默认勾选、文案固定；它是 composer 的子行，排在输入行
    // 之前（语音按钮本身在输入行里，所以不能用它当顺序锚点）。
    const optionsAt = APP_HTML.indexOf('id="ceo-voice-options"');
    const rowAt = APP_HTML.indexOf('class="ceo-input-row"');
    const composerAt = APP_HTML.indexOf('class="ceo-composer"');
    assert.ok(optionsAt > composerAt && optionsAt < rowAt, "option row must sit above the input row inside the composer");
    const optionBlock = APP_HTML.slice(optionsAt, optionsAt + 300);
    assert.match(optionBlock, /<input id="ceo-voice-autosend" type="checkbox" checked>/);
    assert.match(optionBlock, /识别后自动发送/);
    assert.match(optionBlock, /hidden/);
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

test("manual mode inserts at the cursor and keeps both sides of the draft", () => {
    const { U, insertVoiceTextAtCursor } = loadApp();
    U.ceoInput = new StubHTMLTextAreaElement();
    U.ceoInput.value = "前面后面";
    U.ceoInput.selectionStart = 2;
    U.ceoInput.selectionEnd = 2;

    insertVoiceTextAtCursor("语音");

    assert.equal(U.ceoInput.value, "前面语音后面");
    assert.equal(U.ceoInput.selectionStart, 4);
    assert.equal(U.ceoInput.selectionEnd, 4);
});

test("manual mode replaces the live selection instead of appending", () => {
    const { U, insertVoiceTextAtCursor } = loadApp();
    U.ceoInput = new StubHTMLTextAreaElement();
    U.ceoInput.value = "把这段换掉谢谢";
    U.ceoInput.selectionStart = 0;
    U.ceoInput.selectionEnd = 5;

    insertVoiceTextAtCursor("新内容");

    assert.equal(U.ceoInput.value, "新内容谢谢");
});

test("auto send is on by default and prefixes the marker before sending", () => {
    const app = loadApp();
    const { U, deliverVoiceText, VOICE_AUTO_SEND_PREFIX } = app;
    const sent = [];
    U.ceoInput = new StubHTMLTextAreaElement();
    U.ceoInput.value = "";
    app.__context.sendCeoMessage = () => {
        sent.push(U.ceoInput.value);
        U.ceoInput.value = "";
    };

    deliverVoiceText("帮我查一下昨天的任务");

    assert.deepEqual(sent, [`${VOICE_AUTO_SEND_PREFIX}帮我查一下昨天的任务`]);
    assert.equal(VOICE_AUTO_SEND_PREFIX, "用户语音，机器识别结果：");
    assert.equal(U.ceoInput.value, "");
});

test("auto send does not fire when a draft is already in the box", () => {
    const app = loadApp();
    const { U, deliverVoiceText } = app;
    const sent = [];
    U.ceoInput = new StubHTMLTextAreaElement();
    U.ceoInput.value = "还没想发出去";
    U.ceoInput.selectionStart = 7;
    U.ceoInput.selectionEnd = 7;
    app.__context.sendCeoMessage = () => sent.push(U.ceoInput.value);

    deliverVoiceText("语音内容");

    assert.deepEqual(sent, []);
    assert.equal(U.ceoInput.value, "还没想发出去语音内容");
});

test("auto send keeps the text in the box when the send lane refuses", () => {
    const app = loadApp();
    const { U, deliverVoiceText, VOICE_AUTO_SEND_PREFIX } = app;
    U.ceoInput = new StubHTMLTextAreaElement();
    U.ceoInput.value = "";
    // sendCeoMessage 在会话忙/只读时直接 return，不清空输入框。
    app.__context.sendCeoMessage = () => {};

    deliverVoiceText("一句话");

    assert.equal(U.ceoInput.value, `${VOICE_AUTO_SEND_PREFIX}一句话`);
});

test("turning auto send off inserts without the marker", () => {
    const app = loadApp();
    const { U, deliverVoiceText, setCeoVoiceAutoSend } = app;
    const sent = [];
    U.ceoInput = new StubHTMLTextAreaElement();
    U.ceoInput.value = "";
    app.__context.sendCeoMessage = () => sent.push(U.ceoInput.value);

    setCeoVoiceAutoSend(false);
    deliverVoiceText("口头补充");

    assert.deepEqual(sent, []);
    assert.equal(U.ceoInput.value, "口头补充");
});

test("auto send preference persists and is restored on boot", () => {
    const app = loadApp();
    const { ceoVoiceAutoSendEnabled, setCeoVoiceAutoSend } = app;
    assert.equal(ceoVoiceAutoSendEnabled(), true);
    setCeoVoiceAutoSend(false);
    assert.equal(app.__context.localStorage.__data["g3ku.ceoVoiceAutoSend"], "0");

    const booted = loadApp({ __localStorageSeed: { "g3ku.ceoVoiceAutoSend": "0" } });
    booted.restoreCeoVoiceAutoSend();
    assert.equal(booted.ceoVoiceAutoSendEnabled(), false);

    const fresh = loadApp();
    fresh.restoreCeoVoiceAutoSend();
    assert.equal(fresh.ceoVoiceAutoSendEnabled(), true);
});

test("the option row is visible only while recording or transcribing", () => {
    const { S, U, syncCeoVoiceOptions } = loadApp();
    U.ceoVoiceOptions = new StubHTMLElement();
    U.ceoVoiceAutoSend = new StubHTMLInputElement();
    U.ceoVoiceOptions.hidden = true;

    S.ceoVoice = null;
    S.ceoVoiceBusy = false;
    syncCeoVoiceOptions();
    assert.equal(U.ceoVoiceOptions.hidden, true);

    S.ceoVoice = { chunks: [] };
    syncCeoVoiceOptions();
    assert.equal(U.ceoVoiceOptions.hidden, false);

    S.ceoVoice = null;
    S.ceoVoiceBusy = true;
    syncCeoVoiceOptions();
    assert.equal(U.ceoVoiceOptions.hidden, false);

    S.ceoVoiceBusy = false;
    syncCeoVoiceOptions();
    assert.equal(U.ceoVoiceOptions.hidden, true);
});

test("the web and channel marker constants are the same string", () => {
    // 提示词里引用的是这一串；渠道侧与网页侧各有一份常量，任一边改了而另一边没改，
    // 前门那条"看到前缀先确认"就会只对一半的语音生效——所以这条相等必须被测出来。
    const bridgeCode = fs
        .readFileSync("g3ku/qq_official/bridge.py", "utf8")
        .replace(/\r\n/g, "\n");
    const channelMatch = bridgeCode.match(/_VOICE_TRANSCRIPT_PREFIX = "([^"]+)"/);
    const promptMatch = fs
        .readFileSync("g3ku/runtime/prompts/ceo_frontdoor.md", "utf8")
        .replace(/\r\n/g, "\n");

    assert.ok(channelMatch, "channel marker constant not found in bridge.py");
    const { VOICE_AUTO_SEND_PREFIX } = loadApp();
    assert.equal(VOICE_AUTO_SEND_PREFIX, channelMatch[1]);
    assert.ok(
        promptMatch.includes(VOICE_AUTO_SEND_PREFIX),
        "ceo_frontdoor.md must quote the exact marker the two lanes emit"
    );
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

test("marker text is stripped for display and detected", () => {
    const { stripVoiceTranscriptMarkers, formatVoiceDuration } = loadApp();
    const stripped = stripVoiceTranscriptMarkers(
        "今天怎么样\n用户语音，机器识别结果：刚刚给你发了啥"
    );
    assert.equal(stripped.text, "今天怎么样\n刚刚给你发了啥");
    assert.equal(stripped.hasVoice, true);
    assert.equal(stripVoiceTranscriptMarkers("手打文字").hasVoice, false);
    assert.equal(stripVoiceTranscriptMarkers("用户语音，机器识别结果：只剩转写").text, "只剩转写");
    assert.equal(formatVoiceDuration(4.34), "0:04");
    assert.equal(formatVoiceDuration(65), "1:05");
    assert.equal(formatVoiceDuration(NaN), "--");
});

test("voice clip is picked out of attachments and rendered as a player", () => {
    const { pickVoiceClip, buildCeoVoiceBubbleMarkup } = loadApp();
    const clip = pickVoiceClip([
        { path: "/x/a.png", kind: "image" },
        { path: "/x/v.wav", kind: "audio", mime_type: "audio/wav", url: "/api/ceo/uploads/file?a=1" },
    ]);
    assert.equal(clip.kind, "audio");
    assert.equal(pickVoiceClip([{ path: "/x/a.pdf", kind: "file" }]), null);

    const markup = buildCeoVoiceBubbleMarkup(clip, "刚刚给你发了啥");
    assert.match(markup, /class="msg-voice-bubble"/);
    assert.match(markup, /data-ceo-voice-play/);
    assert.match(markup, /<audio[^>]+src="\/api\/ceo\/uploads\/file\?a=1"/);
    assert.match(markup, /data-ceo-voice-toggle[^>]*>转文字</);
    assert.match(markup, /data-ceo-voice-transcript hidden>刚刚给你发了啥</);
    assert.doesNotMatch(
        buildCeoVoiceBubbleMarkup({ path: "/x/v.wav", kind: "audio" }, ""),
        /data-ceo-voice-toggle/,
        "空转写不该画一个点开没内容的按钮"
    );
});

test("audio attachments never render as file pills", () => {
    const { renderStructuredChatAttachments } = loadApp();
    const html = renderStructuredChatAttachments([
        { path: "/x/a.pdf", name: "a.pdf", kind: "file", mime_type: "application/pdf" },
        { path: "/x/v.wav", name: "v.wav", kind: "audio", mime_type: "audio/wav" },
    ]);
    assert.match(html, /a\.pdf/);
    assert.doesNotMatch(html, /v\.wav/);
});

test("pending upload summary names voice clips as voice", () => {
    const { summarizeUploads } = loadApp();
    assert.equal(summarizeUploads([{ path: "/x/v.wav", kind: "audio" }]), "已附加 1 段语音");
    assert.equal(
        summarizeUploads([{ path: "/x/v.wav", kind: "audio" }, { path: "/x/a.pdf", kind: "file" }]),
        "已附加 1 段语音，1 个文件"
    );
});

test("转文字 disclosure toggles its own transcript only", () => {
    const { handleCeoVoiceBubbleClick } = loadApp();
    const transcript = { hidden: true, matches: () => true, setAttribute() {} };
    const toggle = {
        nextElementSibling: transcript,
        parentElement: { querySelector: () => transcript },
        setAttribute() {},
    };
    const event = { target: { closest: (sel) => (sel === "[data-ceo-voice-toggle]" ? toggle : null) } };
    handleCeoVoiceBubbleClick(event);
    assert.equal(transcript.hidden, false);
    handleCeoVoiceBubbleClick(event);
    assert.equal(transcript.hidden, true);
});
