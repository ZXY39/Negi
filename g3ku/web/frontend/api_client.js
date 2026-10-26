"use strict";

// 门面：方法名与签名保持原样，实现分散在 api/*.js 的域模块里。
// 页面与测试都必须按 HTML 顺序加载 core + 各域，再加载本文件。
class ApiClient extends ApiCore {}

Object.assign(
    ApiClient,
    window.ApiDomains.bootstrap,
    window.ApiDomains.ceo,
    window.ApiDomains.tasks,
    window.ApiDomains.models,
    window.ApiDomains.memory,
    window.ApiDomains.persona,
    window.ApiDomains.audit,
    window.ApiDomains.resources,
    window.ApiDomains.external,
    window.ApiDomains.stickers,
);

window.ApiClient = ApiClient;
