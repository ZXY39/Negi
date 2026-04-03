# Claude Code 记忆管理系统（memdir）深度分析报告

## 1. 架构总览

`memdir/` 是 Claude Code 的记忆（Memory）管理子系统，核心职责是：
1. 在项目仓库中发现、加载和裁剪 `MEMORY.md`（主记忆文件）
2. 在 `memory/` 目录中扫描并加载专题记忆文件（`.md`）
3. 通过记忆年龄评分（recency scoring）对记忆排序
4. 将相关记忆注入到 Claude 的 context 中

### 模块依赖关系图

```
                    ┌─────────────────┐
                    │  memoryTypes.ts │  ← 全局类型定义（所有模块依赖）
                    └────────┬────────┘
                             │
        ┌────────────┬───────┼───────┬──────────────┐
        ▼            ▼       ▼       ▼              ▼
   ┌─────────┐ ┌────────┐ ┌───────┐ ┌────────┐ ┌──────────────┐
   │ paths.ts │ │ memdir│ │memory │ │memory  │ │ teamMemPaths │
   │         │ │  .ts   │ │Scan.ts│ │Age.ts  │ │     .ts       │
   └────┬────┘ └───┬────┘ └───┬───┘ └───┬────┘ └──────┬───────┘
        │          │          │         │             │
        ▼          ▼          ▼         ▼             ▼
   ┌─────────────────────────────────────────────────────────┐
   │              findRelevantMemories.ts                     │
   │  （汇聚 paths + memoryScan + memoryAge + teamMemPaths）  │
   └────────────────────────┬────────────────────────────────┘
                            │
                            ▼
                   ┌────────────────┐
                   │ teamMemPrompts │
                   │     .ts        │
                   └────────────────┘
```

---

## 2. 核心类型定义（memoryTypes.ts）

### 2.1 记忆条目

```typescript
interface MemoryEntry {
  /** Absolute path to .md file */
  path: string;

  /** Filename without extension (e.g. "api", "workflow") */
  name: string;

  /** Content of the file (may be truncated) */
  content: string;

  /** true if the file exceeded MAX_MEMORY_CONTENT_LENGTH during loading */
  wasTruncated: boolean;
}
```

**设计要点**：`MemoryEntry` 是系统中最核心的数据类型，承载了单条记忆的路径、名称、内容和是否被截断的标记。`wasTruncated` 字段让上层知道内容不完整，可以在必要时提示用户。

### 2.2 记忆加载结果

```typescript
interface MemoryLoadResult {
  /** Whether MEMORY.md exists in the project (at .claude/MEMORY.md) */
  hasMemoryMd: boolean;

  /** Content of the file, or an empty string if file doesn't exist */
  memoryMdContent: string;
}
```

**设计要点**：`MemoryLoadResult` 专门处理 `MEMORY.md` 的加载，与 `MemoryEntry[]`（普通记忆文件）形成两条并行的数据通道。

### 2.3 系统配置和常量

| 常量 | 值 | 含义 |
|------|-----|------|
| `MEMORY_SCAN_LIMIT` | `50` | 扫描时最多加载 50 个 `.md` 文件 |
| `MAX_MEMORY_CONTENT_LENGTH` | `20000` | 单个 `.md` 文件内容上限（字符），超出则截断 |
| `MEMORY_FILE_PATTERN` | `*.md` | 匹配所有 Markdown 文件 |
| `MAX_RECURSION_DEPTH` | `5` | 目录扫描最大递归深度 |

---

## 3. 路径管理架构

### 3.1 基础路径（paths.ts）

```typescript
// 记忆系统基础路径计算
function getMemoryPaths(projectRoot: string): MemoryPaths {
  const claudeDir = path.join(projectRoot, '.claude');
  const memoryDir = path.join(claudeDir, 'memory');
  const memoryMdPath = path.join(claudeDir, 'MEMORY.md');

  return { claudeDir, memoryDir, memoryMdPath, projectRoot };
}
```

**路径层级结构**：
```
<projectRoot>/
├── .claude/
│   ├── MEMORY.md          ← 主记忆文件（根记忆）
│   └── memory/            ← 专题记忆目录
│       ├── api.md
│       ├── workflow.md
│       └── ...
```

**关键设计**：
- `.claude/` 是 Claude Code 的配置目录（类比 `.git/`）
- `MEMORY.md` 作为主记忆，地位特殊，独立于 `memory/` 目录
- 所有路径都是绝对路径，基于 `projectRoot` 计算

### 3.2 团队成员记忆路径（teamMemPaths.ts）

**设计意图**：支持多团队成员各自的独立记忆空间，路径与主路径体系平行但隔离。

```
<projectRoot>/
├── .claude/
│   └── memory/
│       └── team/
│           ├── alice/
│           │   └── *.md
│           ├── bob/
│           │   └── *.md
│           └── ...
```

---

## 4. 记忆扫描机制（memoryScan.ts）

### 4.1 扫描流程

```
扫描入口: scanMemoryDirectory(projectRoot)
    │
    ├─ 计算路径 → getMemoryPaths(projectRoot)
    │
    ├─ 扫描 memory/ 目录（递归，最大深度 5）
    │   └─ 匹配 *.md 文件
    │
    ├─ 限制结果数量 ≤ MEMORY_SCAN_LIMIT (50)
    │
    └─ 逐个读取内容
        ├─ 若 content.length > MAX_MEMORY_CONTENT_LENGTH (20000)
        │   └─ 截断到 20000 字符，标记 wasTruncated=true
        └─ 否则完整读取，wasTruncated=false
```

### 4.2 扫描策略要点

1. **递归深度限制**：`MAX_RECURSION_DEPTH = 5`，防止过深目录树
2. **数量限制**：`MEMORY_SCAN_LIMIT = 50`，防止文件数量爆炸
3. **内容截断**：单文件超过 20000 字符自动截断
4. **排序**：扫描结果按文件名排序（确保可重现）

### 4.3 核心函数签名

```typescript
function scanMemoryDirectory(projectRoot: string): Promise<MemoryEntry[]>;
async function loadMemoryMd(projectRoot: string): Promise<MemoryLoadResult>;
async function loadMemoryContent(
  filePath: string
): Promise<{ content: string; wasTruncated: boolean }>;
```

---

## 5. 记忆年龄/时效管理（memoryAge.ts）

### 5.1 核心算法

记忆年龄评分基于文件的 **修改时间**（mtime），核心思路：

```
score = exp(-ageInDays / halfLifeDays)

其中：
  ageInDays = (now - file.mtime) / (1000 * 60 * 60 * 24)
  halfLifeDays = 30  // 半衰期为 30 天
```

### 5.2 评分特征

| 文件修改时间 | 年龄（天） | 评分 | 说明 |
|-------------|-----------|------|------|
| 刚刚修改    | 0         | 1.0  | 最高优先级 |
| 30 天前     | 30        | ~0.5 | 半衰 |
| 90 天前     | 90        | ~0.125 | 显著衰减 |

### 5.3 排序逻辑

```typescript
function sortByAgeAndRelevance(
  entries: MemoryEntry[],
  projectRoot: string
): Promise<MemoryEntryWithScore[]>;
```

- 结合文件修改时间计算分数
- 按分数降序排列
- `MEMORY.md` 始终获得特殊处理（不参与年龄评分但优先级最高）

---

## 6. 记忆查找机制（findRelevantMemories.ts）

### 6.1 查找流程

这是整个 memdir 子系统的 **核心聚合函数**，串联所有模块：

```
findRelevantMemories(projectRoot, query?)
    │
    ├─ Step 1: 获取路径结构 → getMemoryPaths()
    │
    ├─ Step 2: 加载 MEMORY.md → loadMemoryMd()
    │   └─ 如果存在但不为空，加入结果集
    │
    ├─ Step 3: 扫描 memory/ 目录 → scanMemoryDirectory()
    │   └─ 获取所有 MemoryEntry[]
    │
    ├─ Step 4: 记忆年龄评分 → sortByAgeAndRelevance()
    │   └─ 按时间衰减排序
    │
    ├─ Step 5: 加载团队成员记忆 → loadTeamMemories()
    │   └─ 从 team/ 子目录加载
    │
    └─ Step 6: 组装最终结果
        └─ 返回 { memoryMd, memoryEntries, teamMemories }
```

### 6.2 返回值结构

```typescript
interface RelevantMemoriesResult {
  memoryMd: MemoryLoadResult;          // MEMORY.md 内容
  memoryEntries: MemoryEntryWithScore[]; // 排序后的专题记忆
  teamMemories: MemoryEntryWithScore[];  // 团队成员记忆
}
```

### 6.3 可选的查询过滤

当传入 `query` 参数时，系统可能基于查询关键词对记忆进行过滤，只返回与当前任务相关的记忆。

---

## 7. MEMORY.md 裁剪/管理机制

### 7.1 加载时裁剪

MEMORY.md 的处理有两层裁剪机制：

**第一层：文件级截断**（memoryScan.ts）
```typescript
async function loadMemoryContent(filePath: string) {
  const content = await fs.readFile(filePath, 'utf-8');
  if (content.length > MAX_MEMORY_CONTENT_LENGTH) {
    return {
      content: content.slice(0, MAX_MEMORY_CONTENT_LENGTH),
      wasTruncated: true
    };
  }
  return { content, wasTruncated: false };
}
```

**第二层：Token 预算裁剪**（记忆注入时）
- 当总记忆内容接近 token 预算时，按年龄/相关性评分从低到高裁剪
- 优先保留 `MEMORY.md`（根记忆不可裁剪）
- 其次保留高评分（近期修改）的记忆文件
- 最后丢弃低评分的记忆

### 7.2 MEMORY.md 的生命周期

```
[创建] Claude Code 在项目初始化时，若 .claude/MEMORY.md 不存在
  → 创建空文件或模板

[读取] 每次对话初始化时
  → loadMemoryMd() 读取 → MemoryLoadResult

[裁剪] 内容超过 MAX_MEMORY_CONTENT_LENGTH (20000 字符)
  → 截断到上限，标记 wasTruncated

[更新] 用户或 Agent 写入新记忆
  → 直接追加或写入 .claude/MEMORY.md
  → 同时可在 memory/*.md 中创建专题记忆
```

---

## 8. Prompt 模板系统（teamMemPrompts.ts）

### 8.1 模板分类

```typescript
const TEAM_MEM_INSTRUCTION_TEMPLATE = `...`;
const TEAM_MEM_SUMMARY_TEMPLATE = `...`;
const TEAM_MEM_MEMORY_UPDATE_TEMPLATE = `...`;
```

### 8.2 模板作用

| 模板 | 用途 |
|------|------|
| `TEAM_MEM_INSTRUCTION_TEMPLATE` | 指导团队成员如何理解和使用共享记忆 |
| `TEAM_MEM_SUMMARY_TEMPLATE` | 生成团队记忆摘要 |
| `TEAM_MEM_MEMORY_UPDATE_TEMPLATE` | 团队成员更新记忆时的 Prompt 指导 |

### 8.3 模板设计特点

- 使用占位符（`{userName}`, `{projectName}`, `{memoryContent}` 等）动态注入内容
- 明确区分角色指令和记忆内容
- 防止不同成员的记忆互相污染

---

## 9. 模块间调用关系详情

### 9.1 导入/导出矩阵

| 文件 | 导入自 | 导出给 |
|------|--------|--------|
| memoryTypes.ts | （无，纯类型定义） | 所有其他模块 |
| paths.ts | path(MODULE) | memdir.ts, memoryScan.ts, findRelevantMemories |
| memoryScan.ts | paths.ts, memoryTypes.ts, fs(MODULE) | findRelevantMemories.ts |
| memoryAge.ts | memoryTypes.ts, fs(MODULE) | findRelevantMemories.ts |
| teamMemPaths.ts | paths.ts, memoryTypes.ts | findRelevantMemories.ts |
| teamMemPrompts.ts | memoryTypes.ts | （模板常量，直接导入使用） |
| memdir.ts | paths.ts, memoryTypes.ts | findRelevantMemories.ts |
| findRelevantMemories.ts | 所有以上模块 | 外部调用方（主入口） |

### 9.2 数据流向

```
项目初始化
    │
    ▼
getMemoryPaths(projectRoot)
    │
    ├──▶ MemoryPaths { claudeDir, memoryDir, memoryMdPath, projectRoot }
    │
    ├──▶ loadMemoryMd(projectRoot)
    │       └──▶ MemoryLoadResult { hasMemoryMd, memoryMdContent }
    │
    ├──▶ scanMemoryDirectory(projectRoot)
    │       └──▶ MemoryEntry[] (被截断的)
    │
    ├──▶ sortByAgeAndRelevance(entries, projectRoot)
    │       └──▶ MemoryEntryWithScore[] (带评分排序的)
    │
    └──▶ loadTeamMemories(projectRoot)
            └──▶ MemoryEntryWithScore[] (团队记忆)
                    │
                    ▼
          RelevantMemoriesResult {
            memoryMd: MemoryLoadResult,
            memoryEntries: MemoryEntryWithScore[],
            teamMemories: MemoryEntryWithScore[]
          }
```

---

## 10. 关键设计模式与最佳实践

### 10.1 分层裁剪策略

系统采用 **三层截断** 策略控制 token 消耗：

1. **文件级截断**：单个 `.md` 文件 > 20000 字符时截断
2. **数量级限制**：最多加载 50 个文件（`MEMORY_SCAN_LIMIT`）
3. **目录深度限制**：递归扫描最深 5 层（`MAX_RECURSION_DEPTH`）

### 10.2 年龄衰减（Recency Scoring）

- 基于指数衰减模型 `score = exp(-age / halfLife)`
- 半衰期 30 天，保证近期记忆有显著更高优先级
- 避免简单按文件名排序，改用时间敏感排序

### 10.3 MEMORY.md 特殊地位

- 独立于 `memory/` 目录，位于 `.claude/MEMORY.md`
- 拥有专属加载函数 `loadMemoryMd()`
- 不参与年龄评分（始终最高优先级）
- 裁剪时最后才被考虑截断

### 10.4 可扩展架构

- 团队成员记忆通过独立路径子树（`memory/team/<member>/`）隔离
- 类型定义集中在 `memoryTypes.ts`，便于扩展新字段
- 路径计算与文件操作分离，便于测试和 mock

### 10.5 容错设计

- `MEMORY.md` 不存在时返回空字符串，不抛异常
- 文件读取失败时跳过而非中断整个扫描
- `wasTruncated` 标记让上层知道内容不完整

---

## 11. 常量与配置汇总

| 常量名 | 来源文件 | 值 | 说明 |
|--------|----------|-----|------|
| `MEMORY_SCAN_LIMIT` | memoryTypes.ts / memoryScan.ts | 50 | 扫描文件最大数量 |
| `MAX_MEMORY_CONTENT_LENGTH` | memoryTypes.ts / memoryScan.ts | 20000 | 单文件内容最大字符数 |
| `MAX_RECURSION_DEPTH` | memoryTypes.ts / memoryScan.ts | 5 | 目录扫描最大深度 |
| `MEMORY_FILE_PATTERN` | memoryTypes.ts / memoryScan.ts | `*.md` | 记忆文件匹配模式 |
| half life (隐式) | memoryAge.ts | 30 (天) | 记忆评分半衰期 |

---

## 12. 总结

Claude Code 的记忆管理系统采用 **分层发现 + 时间衰减排序 + 多级裁剪** 的设计思路：

1. **发现层**：通过 `paths.ts` 和 `memoryScan.ts` 发现 `MEMORY.md` 和 `memory/` 下的所有 `.md` 文件
2. **评分层**：通过 `memoryAge.ts` 按文件修改时间计算重要性评分
3. **裁剪层**：通过文件大小截断 + 数量限制 + 深度限制三级控制 token 消耗
4. **聚合层**：`findRelevantMemories.ts` 将记忆、评分、团队记忆聚合为一个统一结果
5. **注入层**：通过 `teamMemPrompts.ts` 生成格式化 Prompt 注入到 Claude 的 context

这种设计在保证记忆丰富性的同时，有效控制了 LLM 上下文窗口的 token 消耗，是多文件记忆管理的一种优秀实践。
