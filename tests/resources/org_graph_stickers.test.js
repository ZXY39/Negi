const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STORE_PATH = "g3ku/web/frontend/stickers/store.js";
const VIEW_PATH = "g3ku/web/frontend/org_graph_stickers.js";
const CLIENT_PATH = "g3ku/web/frontend/client_api/stickers.js";

const VIEW_CODE = [STORE_PATH, VIEW_PATH].map((file) => fs.readFileSync(file, "utf8")).join("\n");
const CLIENT_CODE = fs.readFileSync(CLIENT_PATH, "utf8");

class StubElement {
    constructor(id = "") {
        this.id = id;
        this.value = "";
        this.hidden = false;
        this.textContent = "";
        this.className = "";
        this.classList = {
            add: (token) => this.classList._set(token, true),
            remove: (token) => this.classList._set(token, false),
            contains: (token) => String(this.className || "").split(/\s+/).includes(token),
            toggle: (token, force) => this.classList._set(token, force === undefined ? !this.classList.contains(token) : !!force),
            _set: (token, on) => {
                const classes = new Set(String(this.className || "").split(/\s+/).filter(Boolean));
                if (on) classes.add(token);
                else classes.delete(token);
                this.className = [...classes].join(" ");
            },
        };
        this._innerHTML = "";
    }

    addEventListener() {}

    get innerHTML() {
        return this._innerHTML;
    }

    set innerHTML(value) {
        this._innerHTML = String(value);
    }

    querySelector() {
        return null;
    }
}

function stubU(document) {
    const ids = [
        "sticker-refresh-btn",
        "sticker-search-input",
        "sticker-pack-list",
        "sticker-pack-info",
        "sticker-detail",
        "sticker-busy-label",
        "sticker-upload-input",
        "sticker-max-per-reply",
        "sticker-injected-banner",
        "sticker-create-pack-btn",
    ];
    return {
        stickerRefresh: document.getElementById("sticker-refresh-btn"),
        stickerSearch: document.getElementById("sticker-search-input"),
        stickerPackList: document.getElementById("sticker-pack-list"),
        stickerPackInfo: document.getElementById("sticker-pack-info"),
        stickerDetail: document.getElementById("sticker-detail"),
        stickerBusyLabel: document.getElementById("sticker-busy-label"),
        stickerUploadInput: document.getElementById("sticker-upload-input"),
        stickerMaxPerReply: document.getElementById("sticker-max-per-reply"),
        stickerInjectedBanner: document.getElementById("sticker-injected-banner"),
        stickerCreatePackBtn: document.getElementById("sticker-create-pack-btn"),
        _ids: ids,
    };
}

function loadView({ calls = {} } = {}) {
    const elements = new Map();
    const document = {
        getElementById(id) {
            if (!elements.has(id)) elements.set(id, new StubElement(id));
            return elements.get(id);
        },
    };
    const context = {
        console,
        document,
        U: stubU(document),
        esc: (value) => String(value === undefined || value === null ? "" : value),
        addNotice: (payload) => calls.notices?.push(payload),
        showToast: (payload) => calls.toasts?.push(payload),
        openConfirm: (payload) => calls.confirms?.push(payload),
        openMediaViewer: (payload) => calls.viewers?.push(payload),
        isAbortLike: (error) => String(error?.name || "") === "AbortError",
        ApiClient: {
            async getStickerPacks() {
                return [];
            },
            async getStickerPack() {
                return null;
            },
            async getStickerCategoryImages() {
                return { items: [], total: 0 };
            },
        },
        window: {},
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(
        `${VIEW_CODE}
        this.__test = {
            StickersState,
            stickerFilteredPacks,
            renderStickerPacks,
            renderStickerDetail,
            applyStickerPack,
            dropStickerPack,
            onStickerDetailClick,
            onStickerListClick,
            renderStickerSettings,
            stickerLimits,
            stickerErrorText,
            startStickerPackCreate,
            createStickerPack,
        };`,
        context,
    );
    return { ...context.__test, U: context.U, calls, ApiClient: context.ApiClient };
}

function pack(overrides = {}) {
    return {
        dir_name: "demo",
        key: "demo",
        theme: "动漫少女",
        theme_source: "human",
        source: {},
        enabled: true,
        total: 2,
        categories: [
            {
                dir_name: "happy",
                key: "happy",
                description: "开心大笑",
                enabled: true,
                count: 2,
                images: [
                    { name: "a.png", size: 10, mtime: 1, url: "/api/resources/stickers/image?pack=demo&category=happy&name=a.png" },
                    { name: "b.png", size: 10, mtime: 1, url: "/api/resources/stickers/image?pack=demo&category=happy&name=b.png" },
                ],
            },
        ],
        ...overrides,
    };
}

test("包列表只报计数与待注册数，启停不加徽标", () => {
    const view = loadView();

    view.renderStickerPacks();
    assert.match(view.U.stickerPackList.innerHTML, /还没有表情包/);
    assert.match(view.U.stickerPackList.innerHTML, /导入这个表情包/);

    view.StickersState.packs = [
        pack(),
        pack({ dir_name: "_parked", key: "parked", theme: "", enabled: false, total: 3, pending: 2, categories: [] }),
    ];
    view.StickersState.selected = "demo";
    view.renderStickerPacks();

    const html = view.U.stickerPackList.innerHTML;
    assert.match(html, /sticker-pack-item selected" data-sticker-pack="demo"/);
    assert.match(html, /动漫少女/);
    assert.match(html, /待注册 2/);
    assert.doesNotMatch(html, /已停用/);
    assert.match(html, /（未设置主题）/);
    assert.equal(view.U.stickerPackInfo.textContent, "共 2 个包");
});

test("搜索按主题、分类名和分类描述过滤", () => {
    const view = loadView();
    view.StickersState.packs = [pack(), pack({ dir_name: "cat", key: "cat", theme: "猫猫", categories: [] })];

    view.StickersState.search = "开心";
    assert.deepEqual(view.stickerFilteredPacks().map((item) => item.dir_name), ["demo"]);

    view.StickersState.search = "猫猫";
    assert.deepEqual(view.stickerFilteredPacks().map((item) => item.dir_name), ["cat"]);

    view.StickersState.search = "";
    assert.equal(view.stickerFilteredPacks().length, 2);
});

test("详情渲染缩略图、描述与停用提示", () => {
    const view = loadView();
    view.StickersState.detail = pack();
    view.StickersState.selected = "demo";
    view.renderStickerDetail();

    const html = view.U.stickerDetail.innerHTML;
    assert.match(html, /value="动漫少女"/);
    assert.match(html, /开心大笑/);
    assert.match(html, /happy-a\.png|a\.png/);
    assert.match(html, /data-sticker-action="delete-image"/);
    assert.match(html, /data-sticker-category="happy"/);
    // 只有 2 张，没被截断，所以不该出现"查看全部"。
    assert.doesNotMatch(html, /查看全部/);
    assert.doesNotMatch(html, /停用的包不进模型的清单/);
});

test("详情：截断给查看全部，待注册给注册按钮，停用只降饱和不加提示", () => {
    const view = loadView();
    const disabled = pack({
        enabled: false,
        dir_name: "_demo",
        total: 9,
        categories: [
            {
                dir_name: "_happy",
                key: "happy",
                description: "",
                enabled: false,
                count: 9,
                registered: 2,
                pending: 1,
                images: [
                    { name: "001.png", size: 1, mtime: 1, url: "/u1", registered: true },
                    { name: "a.png", size: 1, mtime: 1, url: "/u2", registered: false },
                ],
            },
        ],
    });
    view.StickersState.detail = disabled;
    view.renderStickerDetail();

    const html = view.U.stickerDetail.innerHTML;
    assert.match(html, /查看全部/);
    assert.match(html, /9 张/);
    assert.doesNotMatch(html, /停用的包不进模型的清单/);
    assert.match(html, /sticker-category-card is-disabled/);
    assert.match(html, /2 \/ 9 张已注册（1 张待注册）/);
    assert.match(html, /data-sticker-action="register-category"/);
    assert.match(html, /sticker-thumb is-unregistered/);
});

test("新建包：卡片只在点创建后出现，保存后收起并选中新包", async () => {
    const view = loadView();
    view.U.stickerDetail.clientWidth = 500;
    view.StickersState.detail = pack();
    view.StickersState.selected = "demo";
    view.renderStickerDetail();
    assert.doesNotMatch(view.U.stickerDetail.innerHTML, /sticker-new-pack-key/);

    view.startStickerPackCreate();
    const html = view.U.stickerDetail.innerHTML;
    assert.match(html, /id="sticker-new-pack-key"/);
    assert.match(html, /data-sticker-action="save-new-pack"/);
    assert.match(html, /data-sticker-action="cancel-new-pack"/);
    // 卡片铺在右栏顶部，已选中的包详情还在下面。
    assert.ok(html.indexOf("sticker-new-pack-card") < html.indexOf("sticker-pack-head"));

    view.U.stickerDetail.querySelector = (selector) =>
        selector === "#sticker-new-pack-key" ? { value: "atri" } : null;
    const posted = [];
    view.ApiClient.createStickerPack = async (key) => {
        posted.push(key);
        return { item: pack({ dir_name: key, key, total: 0, categories: [] }) };
    };
    await view.createStickerPack();

    assert.deepEqual(posted, ["atri"]);
    assert.equal(view.StickersState.creatingPack, false);
    assert.equal(view.StickersState.selected, "atri");
    assert.doesNotMatch(view.U.stickerDetail.innerHTML, /sticker-new-pack-key/);
});

test("取消新建包只收起卡片，不动已选中的包", () => {
    const view = loadView();
    view.StickersState.detail = pack();
    view.StickersState.selected = "demo";
    view.startStickerPackCreate();
    const target = {
        dataset: { stickerAction: "cancel-new-pack" },
        closest(selector) {
            return selector === "[data-sticker-action]" ? target : null;
        },
    };

    view.onStickerDetailClick({ target });

    assert.equal(view.StickersState.creatingPack, false);
    assert.equal(view.StickersState.selected, "demo");
    assert.doesNotMatch(view.U.stickerDetail.innerHTML, /sticker-new-pack-key/);
});

function categoryWithImages(count) {
    return {
        dir_name: "happy",
        key: "happy",
        description: "开心大笑",
        enabled: true,
        count,
        registered: count,
        pending: 0,
        images: Array.from({ length: count }, (_, index) => ({
            name: `${String(index + 1).padStart(3, "0")}.png`,
            size: 1,
            mtime: 1,
            url: `/u${index}`,
            registered: true,
        })),
    };
}

test("首屏按当前列数只铺两行减一格，末位是查看全部瓦片", () => {
    const view = loadView();
    // 面板 500px 扣掉内边距与卡片边距后正好 4 列：4 × 2 - 1 = 7 张。
    view.U.stickerDetail.clientWidth = 500;
    view.StickersState.detail = pack({ categories: [categoryWithImages(9)] });
    view.renderStickerDetail();

    const html = view.U.stickerDetail.innerHTML;
    assert.equal((html.match(/class="sticker-thumb-open"/g) || []).length, 7);
    assert.match(html, /data-sticker-action="expand-category"/);
    assert.match(html, /查看全部/);
    assert.doesNotMatch(html, /data-sticker-action="collapse-category"/);
});

test("展开后全部图片保持原色，末尾出现同尺寸收起瓦片", () => {
    const view = loadView();
    view.U.stickerDetail.clientWidth = 500;
    const category = categoryWithImages(9);
    category.images = [];
    view.StickersState.detail = pack({ categories: [category] });
    view.StickersState.expanded["demo/happy"] = categoryWithImages(9).images;
    view.renderStickerDetail();

    const html = view.U.stickerDetail.innerHTML;
    assert.equal((html.match(/class="sticker-thumb-open"/g) || []).length, 9);
    assert.match(html, /class="sticker-thumb-more" data-sticker-action="collapse-category"/);
    assert.match(html, /收起/);
    assert.doesNotMatch(html, /data-sticker-action="expand-category"/);
    // 已注册的图片不带 is-unregistered：展开接口曾经漏传注册表，整页会变灰。
    assert.doesNotMatch(html, /sticker-thumb is-unregistered/);
});

test("收起清掉展开缓存并回到预览", () => {
    const view = loadView();
    view.U.stickerDetail.clientWidth = 500;
    view.StickersState.detail = pack({ categories: [categoryWithImages(9)] });
    view.StickersState.selected = "demo";
    view.StickersState.expanded["demo/happy"] = categoryWithImages(9).images;
    const card = { dataset: { stickerCategory: "happy" } };
    const target = {
        dataset: { stickerAction: "collapse-category" },
        closest(selector) {
            if (selector === "[data-sticker-action]") return target;
            if (selector === ".sticker-category-card") return card;
            return null;
        },
    };

    view.onStickerDetailClick({ target });

    assert.equal(view.StickersState.expanded["demo/happy"], undefined);
    assert.match(view.U.stickerDetail.innerHTML, /data-sticker-action="expand-category"/);
});

test("写操作返回的整包快照替换骨架，改名后选中跟着走", () => {
    const view = loadView();
    view.StickersState.packs = [{ ...pack(), categories: [{ dir_name: "happy", key: "happy", description: "开心大笑", enabled: true, count: 2 }] }];
    view.StickersState.selected = "demo";

    view.applyStickerPack(pack({ dir_name: "_demo", key: "demo", enabled: false }), "demo");

    assert.equal(view.StickersState.selected, "_demo");
    assert.deepEqual(Array.from(view.StickersState.packs, (item) => item.dir_name), ["_demo"]);
    assert.equal(view.StickersState.packs[0].enabled, false);
    // 列表只拿骨架：图不下发到左栏状态里。
    assert.equal(view.StickersState.packs[0].categories[0].images.length, 0);
});

test("点缩略图交给程序内查看器，显式 image 模式", () => {
    const view = loadView({ calls: {} });
    view.calls.viewers = [];
    view.StickersState.detail = pack();
    const target = {
        dataset: {
            stickerAction: "preview",
            url: "/api/resources/stickers/image?pack=demo&category=happy&name=a.png",
            name: "a.png",
            title: "demo / happy",
        },
        closest(selector) {
            return selector === "[data-sticker-action]" ? target : null;
        },
    };

    view.onStickerDetailClick({ target });

    assert.equal(view.calls.viewers.length, 1);
    assert.equal(view.calls.viewers[0].mode, "image");
    assert.equal(view.calls.viewers[0].downloadName, "a.png");
    assert.match(view.calls.viewers[0].title, /demo \/ happy/);
});

test("删除按钮先弹确认，确认文案指向可逆的停用", () => {
    const view = loadView({ calls: { confirms: [] } });
    view.StickersState.detail = pack();
    const target = {
        dataset: { stickerAction: "delete-pack" },
        closest(selector) {
            return selector === "[data-sticker-action]" ? target : null;
        },
    };

    view.onStickerDetailClick({ target });

    assert.equal(view.calls.confirms.length, 1);
    assert.equal(view.calls.confirms[0].confirmKind, "danger");
    assert.match(view.calls.confirms[0].text, /不可恢复/);
    assert.match(view.calls.confirms[0].text, /停用/);
});

test("发送策略按服务端下发的上限渲染", () => {
    const view = loadView();
    view.StickersState.settings = {
        max_per_reply: 2,
        theme_max_chars: 40,
        description_max_chars: 12,
        max_upload_files: 5,
    };
    view.renderStickerSettings();

    assert.equal(view.U.stickerMaxPerReply.value, "2");
    assert.equal(view.U.stickerInjectedBanner.hidden, true);
    assert.equal(view.stickerLimits().theme, 40);
    assert.equal(view.stickerLimits().description, 12);
});

test("注入总量超限时露出停用提醒", () => {
    const view = loadView();
    view.StickersState.settings = { max_per_reply: 1, over_injected_limit: true };
    view.renderStickerSettings();
    assert.equal(view.U.stickerInjectedBanner.hidden, false);
});

test("http 错误优先显示服务端中文 message", () => {
    const view = loadView();
    const error = new Error("HTTP 409");
    error.data = { code: "sticker_name_conflict", message: "happy 已存在" };
    assert.equal(view.stickerErrorText(error), "happy 已存在");
    assert.equal(view.stickerErrorText(new Error("boom")), "boom");
});

// ------------------------------------------------------------ api client

function loadClient() {
    const calls = [];
    class FormDataStub {
        constructor() {
            this.entries = [];
        }
        append(key, value) {
            this.entries.push([key, value]);
        }
    }
    const context = {
        console,
        FormData: FormDataStub,
        window: {},
        ApiCore: class {
            static async _request(method, path, options = {}) {
                calls.push({ method, path, options });
                return { ok: true, items: [], item: {}, total: 0 };
            }
            static get(path, params = {}) {
                return this._request("GET", path, { params });
            }
            static post(path, body = {}, params = {}) {
                return this._request("POST", path, { params, body });
            }
            static put(path, body = {}, params = {}) {
                return this._request("PUT", path, { params, body });
            }
            static delete(path, options = {}) {
                return this._request("DELETE", path, options);
            }
        },
    };
    context.window = context;
    vm.createContext(context);
    vm.runInContext(CLIENT_CODE, context);
    // 与 api_client.js 同一个装配方式：域模块挂到 ApiCore 派生类上，方法里的
    // this 才是类本身（_request 是静态方法）。
    class ApiClient extends context.ApiCore {}
    Object.assign(ApiClient, context.window.ApiDomains.stickers);
    return { ApiClient, calls };
}

test("前端域模块把回调参数送到 query，而不是拼进路径", async () => {
    const { ApiClient, calls } = loadClient();

    await ApiClient.getStickerPacks();
    assert.equal(calls[0].method, "GET");
    assert.equal(calls[0].path, "/api/resources/stickers/packs");
    assert.equal(calls[0].options.requestKey, "stickers:packs");

    await ApiClient.deleteStickerImage("demo", "happy", "a b.png");
    const remove = calls.at(-1);
    assert.equal(remove.method, "DELETE");
    assert.equal(remove.path, "/api/resources/stickers/packs/demo/categories/happy/images");
    assert.equal(JSON.stringify(remove.options.params), JSON.stringify({ name: "a b.png" }));
    assert.equal(remove.options.timeoutMs, 0);
});

test("上传走 FormData，同名文件不丢", async () => {
    const { ApiClient, calls } = loadClient();

    await ApiClient.uploadStickerImages("demo", "happy", ["f1", "f2"]);

    const upload = calls.at(-1);
    assert.equal(upload.method, "POST");
    assert.equal(upload.path, "/api/resources/stickers/upload");
    assert.equal(JSON.stringify(upload.options.params), JSON.stringify({ pack: "demo", category: "happy" }));
    assert.equal(JSON.stringify(upload.options.body.entries), JSON.stringify([["files", "f1"], ["files", "f2"]]));
    assert.equal(upload.options.timeoutMs, 0);
});

test("包名里的特殊字符会被转义，不拼出额外路径段", async () => {
    const { ApiClient, calls } = loadClient();

    await ApiClient.getStickerPack("a/b c", 3);

    const detail = calls.at(-1);
    assert.equal(detail.path, "/api/resources/stickers/packs/a%2Fb%20c");
    assert.equal(JSON.stringify(detail.options.params), JSON.stringify({ per_category: 3 }));
});
