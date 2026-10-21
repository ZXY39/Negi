"use strict";

// 资源页（Skill / Tool）自有状态：列表、选中项、编辑草稿、文件缓存与分页。
// 渲染与请求在 org_graph_resources.js；app.js 的翻页/详情动作读写同一份状态。
const RESOURCE_PAGE_SIZES = [20, 50, 100];

const ResourcesState = {
    contextRiskCatalogRequest: null,
    skills: [],
    selectedSkill: null,
    skillFiles: [],
    skillContents: {},
    skillFileLoads: {},
    selectedSkillFile: "",
    skillBusy: false,
    skillDirty: false,
    skillAutosaveTimerId: null,
    skillAutosavePending: false,
    skillPage: 1,
    skillPageSize: RESOURCE_PAGE_SIZES[0],
    tools: [],
    selectedTool: null,
    toolBusy: false,
    toolDirty: false,
    toolAutosaveTimerId: null,
    toolAutosavePending: false,
    toolPage: 1,
    toolPageSize: RESOURCE_PAGE_SIZES[0],
};
