"use strict";

// 表情包管理页的状态。只放数据、不放 DOM：本页每次写操作都由服务端返回整包快照，
// 渲染永远对着最近一次响应，所以这里没有"乐观副本"需要回滚。
const StickersState = {
    packs: [],
    loadedOnce: false,
    selected: "",
    detail: null,
    // "pack/category" -> 已取回的完整图片列表（展开"查看全部"时才填）。
    expanded: {},
    // 详情快照里每个分类取回的图片上限；视口变宽后可据此判断要不要补请求。
    previewLimit: 0,
    settings: null,
    settingsLoadedOnce: false,
    busy: false,
    search: "",
    // 上传按钮属于某个分类，但 <input type="file"> 只有一颗，所以选择时记在这里。
    uploadTarget: null,
};

window.StickersState = StickersState;
