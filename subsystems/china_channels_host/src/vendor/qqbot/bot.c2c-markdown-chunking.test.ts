import { describe, expect, it, vi } from "vitest";
import { chunkC2CMarkdownText, looksLikeStructuredMarkdown } from "./bot.js";

describe("looksLikeStructuredMarkdown", () => {
  it("detects headings, tables, quotes, code fences, lists, inline markdown, and multi-paragraph text", () => {
    expect(looksLikeStructuredMarkdown("# 标题")).toBe(true);
    expect(looksLikeStructuredMarkdown("| a | b |\n| --- | --- |\n| 1 | 2 |")).toBe(true);
    expect(looksLikeStructuredMarkdown("> 引用")).toBe(true);
    expect(looksLikeStructuredMarkdown("```ts\nconst answer = 42;\n```")).toBe(true);
    expect(looksLikeStructuredMarkdown("- item")).toBe(true);
    expect(looksLikeStructuredMarkdown("这里有 **加粗** 和 `代码`")).toBe(true);
    expect(looksLikeStructuredMarkdown("第一段\n\n第二段")).toBe(true);
    expect(looksLikeStructuredMarkdown("普通单段文本")).toBe(false);
  });
});

describe("chunkC2CMarkdownText", () => {
  it("keeps headings with the first paragraph when possible", () => {
    const chunks = chunkC2CMarkdownText({
      text: "# 标题\n\n第一段说明。\n\n第二段说明继续。",
      limit: 40,
      strategy: "markdown-block",
    });

    expect(chunks).toEqual(["# 标题\n\n第一段说明。", "第二段说明继续。"]);
  });

  it("does not leave thematic breaks as standalone chunks", () => {
    const chunks = chunkC2CMarkdownText({
      text: "前文说明。\n\n---\n\n后文补充内容。",
      limit: 40,
      strategy: "markdown-block",
    });

    expect(chunks.some((chunk) => chunk.trim() === "---")).toBe(false);
    expect(chunks.some((chunk) => chunk.includes("---"))).toBe(true);
  });

  it("moves thematic breaks with the next section when the current chunk is near the limit", () => {
    const intro = "intro ".repeat(22).trim();
    const text = `${intro}\n\n---\n\n# 标题\n\n后文补充说明。`;

    expect(text.length).toBeLessThan(160);

    const chunks = chunkC2CMarkdownText({
      text,
      limit: 160,
      strategy: "markdown-block",
    });

    expect(chunks).toEqual([intro, "---\n\n# 标题\n\n后文补充说明。"]);
  });

  it("repeats table headers when splitting long tables", () => {
    const table = [
      "| col1 | col2 |",
      "| --- | --- |",
      "| a1 | b1 |",
      "| a2 | b2 |",
      "| a3 | b3 |",
      "| a4 | b4 |",
    ].join("\n");

    const chunks = chunkC2CMarkdownText({
      text: table,
      limit: 48,
      strategy: "markdown-block",
    });

    expect(chunks.length).toBeGreaterThan(1);
    for (const chunk of chunks) {
      expect(chunk).toContain("| col1 | col2 |\n| --- | --- |");
    }
  });

  it("proactively splits near-limit tables on complete rows before hitting the hard limit", () => {
    const table = [
      "| col1 | col2 |",
      "| --- | --- |",
      `| row-1 | ${"A".repeat(36)} |`,
      `| row-2 | ${"B".repeat(36)} |`,
      `| row-3 | ${"C".repeat(36)} |`,
      `| row-4 | ${"D".repeat(36)} |`,
    ].join("\n");

    expect(table.length).toBeLessThan(240);

    const chunks = chunkC2CMarkdownText({
      text: table,
      limit: 240,
      strategy: "markdown-block",
    });

    expect(chunks.length).toBeGreaterThan(1);
    for (const chunk of chunks) {
      expect(chunk).toContain("| col1 | col2 |\n| --- | --- |");
      const bodyLines = chunk.split("\n").slice(2);
      expect(bodyLines.every((line) => line.startsWith("| row-"))).toBe(true);
    }
  });

  it("keeps real-world chinese-heavy tables byte-safe with repeated headers", () => {
    const table = [
      "| 序号 | 成语 | 拼音 | 释义 | 出处 | 用法举例 |",
      "|------|------|------|------|------|----------|",
      "| 1 | 画蛇添足 | huà shé tiān zú | 做多余的事，反而不恰当 | 《战国策·齐策》 | 这篇文章已经很好了，再改就是画蛇添足 |",
      "| 2 | 杯弓蛇影 | bēi gōng shé yǐng | 疑神疑鬼，自相惊扰 | 《晋书·乐广传》 | 别杯弓蛇影的，没那么多阴谋 |",
      "| 3 | 打草惊蛇 | dǎ cǎo jīng shé | 行动不慎惊动对方 | 《太平广记》 | 先别动，免得打草惊蛇 |",
      "| 4 | 虎头蛇尾 | hǔ tóu shé wěi | 开头声势大，结尾劲头小 | 元·康进之 | 很多人的新年计划都是虎头蛇尾 |",
      "| 5 | 龙蛇混杂 | lóng shé hùn zá | 好人坏人混在一起 | 《敦煌变文集》 | 这市场龙蛇混杂，买东西要当心 |",
      "| 6 | 拨草寻蛇 | bō cǎo xún shé | 故意挑起事端 | 明·汤显祖 | 你这不是解决问题，是拨草寻蛇 |",
      "| 7 | 惊蛇入草 | jīng shé rù cǎo | 形容草书笔势矫健 | 唐·韦续《书诀》 | 怀素的草书，有惊蛇入草之势 |",
      "| 8 | 春蚓秋蛇 | chūn yǐn qiū shé | 比喻书法拙劣 | 《晋书·王羲之传》 | 这字写得春蚓秋蛇，认都认不出 |",
      "| 9 | 豕分蛇断 | shǐ fēn shé duàn | 支离破碎 | 南朝·陈·徐陵 | 古籍残缺，豕分蛇断，难以辨认 |",
      "| 10 | 佛口蛇心 | fó kǒu shé xīn | 嘴上慈悲，心肠狠毒 | 《五灯会元》 | 那人佛口蛇心，别被他的好话骗了 |",
      "| 11 | 强龙不压地头蛇 | qiáng lóng bù yā dì tóu shé | 外来势力斗不过本地势力 | 《西游记》 | 强龙不压地头蛇，到了人家地盘要客气 |",
      "| 12 | 牛鬼蛇神 | niú guǐ shén | 各种坏人或丑恶现象 | 唐·杜牧 | 那年代牛鬼蛇神都冒出来了 |",
      "| 13 | 虚与委蛇 | xū yǔ wēi yí | 假意敷衍应酬 | 《庄子·应帝王》 | 他只是虚与委蛇，不是真答应你 |",
      "| 14 | 岁在龙蛇 | suì zài lóng shé | 指生命终结之年 | 《后汉书·郑玄传》 | 先生年过七旬，恐岁在龙蛇 |",
      "| 15 | 蛇蝎心肠 | shé xiē xīn cháng | 心肠极其狠毒 | 明·冯梦龙 | 竟对亲人都下得去手，真是蛇蝎心肠 |",
      "| 16 | 笔走龙蛇 | bǐ zǒu lóng shé | 书法笔势雄健洒脱 | 唐·李白 | 他挥毫泼墨，笔走龙蛇 |",
      "| 17 | 灵蛇之珠 | líng shé zhī zhū | 珍贵的宝物 | 《三国志》 | 此乃灵蛇之珠，不可轻弃 |",
      "| 18 | 长蛇封豕 | cháng shé fēng shǐ | 贪暴的敌人 | 《左传》 | 长蛇封豕横行，百姓苦不堪言 |",
      "| 19 | 飞鸟惊蛇 | fēi niǎo jīng shé | 书法笔势自然灵动 | 唐·释亚栖 | 这幅字飞鸟惊蛇，妙不可言 |",
      "| 20 | 骇龙走蛇 | hài lóng zǒu shé | 形容气势奔放 | 唐·张怀瓘 | 文章气势骇龙走蛇，一气呵成 |",
      "| 21 | 蛇口蜂针 | shé kǒu fēng zhēn | 比喻恶毒的言行 | 无考 | 此人蛇口蜂针，得罪了不少人 |",
      "| 22 | 行行蛇蚓 | xíng xíng shé yǐn | 形容字体歪扭难看 | 无考 | 他的字行行蛇蚓，不像样子 |",
      "| 23 | 蝮蛇螫手 | fù shé shì shǒu | 坏事虽小但危害大 | 《三国志》 | 蝮蛇螫手，壮士解腕，不可犹豫 |",
      "| 24 | 一龙一蛇 | yī lóng yī shé | 比喻变化无常 | 《庄子》 | 处世之道，一龙一蛇，与时俱化 |",
      "| 25 | 贪蛇忘尾 | tān shé wàng wěi | 贪图眼前利益不顾后患 | 无考 | 贪蛇忘尾，迟早要吃亏的 |",
      "| 26 | 壁间蛇影 | bì jiān shé yǐng | 疑心太重 | 无考 | 类似杯弓蛇影，壁间蛇影也是多疑 |",
      "| 27 | 为蛇画足 | wéi shé huà zú | 同画蛇添足 | 《三国志》 | 为蛇画足，反失其真 |",
      "| 28 | 握蛇骑虎 | wò shé qí hǔ | 处境极其危险 | 《魏书》 | 他现在是握蛇骑虎，进退两难 |",
      "| 29 | 蛇化为龙 | shé huà wéi lóng | 由微贱变为显贵 | 《后汉书》 | 蛇化为龙，不变其文，志在千里 |",
      "| 30 | 蛇行鳞伏 | shé xíng lín fú | 形容潜行躲避 | 无考 | 他们蛇行鳞伏，悄悄靠近目标 |",
    ].join("\n");

    const limit = 1500;
    const chunks = chunkC2CMarkdownText({
      text: table,
      limit,
      strategy: "markdown-block",
    });

    expect(chunks.length).toBeGreaterThan(1);
    for (const chunk of chunks) {
      expect(Buffer.byteLength(chunk, "utf8")).toBeLessThanOrEqual(limit);
      expect(chunk).toContain("| 序号 | 成语 | 拼音 | 释义 | 出处 | 用法举例 |");
      expect(chunk).toContain("|------|------|------|------|------|----------|");

      const bodyLines = chunk.split("\n").slice(2).filter(Boolean);
      expect(
        bodyLines.every((line) => line.startsWith("|") && line.endsWith("|"))
      ).toBe(true);
    }
  });

  it("repeats the heading for continuation chunks of a long chinese-heavy table", () => {
    const table = [
      "| 序号 | 国家 | 首都 | 官方语言 | 货币 | 人口（约） | 面积（万km²） | 所属大洲 | 特色 |",
      "|------|------|------|----------|------|------------|----------------|----------|------|",
      "| 1 | 中国 | 北京 | 普通话 | 人民币 | 14.1亿 | 960 | 亚洲 | 长城、熊猫、火锅 |",
      "| 2 | 日本 | 东京 | 日语 | 日元 | 1.25亿 | 37.8 | 亚洲 | 樱花、动漫、寿司 |",
      "| 3 | 韩国 | 首尔 | 韩语 | 韩元 | 5174万 | 10 | 亚洲 | K-pop、泡菜、韩剧 |",
      "| 4 | 美国 | 华盛顿 | 英语 | 美元 | 3.3亿 | 983 | 北美 | 好莱坞、硅谷、NASA |",
      "| 5 | 英国 | 伦敦 | 英语 | 英镑 | 6700万 | 24.4 | 欧洲 | 大本钟、皇室、英超 |",
      "| 6 | 法国 | 巴黎 | 法语 | 欧元 | 6700万 | 64.3 | 欧洲 | 埃菲尔铁塔、卢浮宫 |",
      "| 7 | 德国 | 柏林 | 德语 | 欧元 | 8300万 | 35.7 | 欧洲 | 啤酒、汽车、哲学 |",
      "| 8 | 意大利 | 罗马 | 意大利语 | 欧元 | 5900万 | 30.1 | 欧洲 | 披萨、文艺复兴、足球 |",
      "| 9 | 西班牙 | 马德里 | 西班牙语 | 欧元 | 4700万 | 50.6 | 欧洲 | 弗拉门戈、斗牛 |",
      "| 10 | 俄罗斯 | 莫斯科 | 俄语 | 卢布 | 1.44亿 | 1710 | 欧洲/亚洲 | 红场、芭蕾、伏特加 |",
      "| 11 | 巴西 | 巴西利亚 | 葡萄牙语 | 雷亚尔 | 2.1亿 | 851 | 南美 | 足球、狂欢节、雨林 |",
      "| 12 | 阿根廷 | 布宜诺斯艾利斯 | 西班牙语 | 比索 | 4500万 | 278 | 南美 | 探戈、牛排、梅西 |",
      "| 13 | 加拿大 | 渥太华 | 英语/法语 | 加元 | 3800万 | 998 | 北美 | 枫叶、冰球、尼亚加拉瀑布 |",
      "| 14 | 澳大利亚 | 堪培拉 | 英语 | 澳元 | 2580万 | 769 | 大洋洲 | 袋鼠、大堡礁、考拉 |",
      "| 15 | 新西兰 | 惠灵顿 | 英语 | 新西兰元 | 510万 | 26.8 | 大洋洲 | 魔戒取景地、霍比特人 |",
      "| 16 | 印度 | 新德里 | 印地语/英语 | 卢比 | 14.2亿 | 328 | 亚洲 | 塔姬陵、咖喱、IT |",
      "| 17 | 泰国 | 曼谷 | 泰语 | 泰铢 | 7000万 | 51.3 | 亚洲 | 寺庙、大象、冬阴功 |",
      "| 18 | 越南 | 河内 | 越南语 | 越南盾 | 9700万 | 33.1 | 亚洲 | 河粉、下龙湾、奥黛 |",
      "| 19 | 埃及 | 开罗 | 阿拉伯语 | 埃及镑 | 1.04亿 | 100 | 非洲 | 金字塔、尼罗河、法老 |",
      "| 20 | 南非 | 比勒陀利亚 | 11种官方语言 | 兰特 | 5900万 | 122 | 非洲 | 曼德拉、钻石、彩虹之国 |",
      "| 21 | 墨西哥 | 墨西哥城 | 西班牙语 | 比索 | 1.3亿 | 196 | 北美 | 玛雅遗址、塔可、亡灵节 |",
      "| 22 | 印度尼西亚 | 雅加达 | 印尼语 | 印尼盾 | 2.7亿 | 191 | 亚洲 | 巴厘岛、科莫多龙 |",
      "| 23 | 土耳其 | 安卡拉 | 土耳其语 | 里拉 | 8500万 | 78.4 | 亚洲/欧洲 | 热气球、清真寺、烤肉 |",
      "| 24 | 冰岛 | 雷克雅未克 | 冰岛语 | 冰岛克朗 | 37万 | 10.3 | 欧洲 | 极光、火山、蓝湖温泉 |",
      "| 25 | 挪威 | 奥斯陆 | 挪威语 | 挪威克朗 | 540万 | 38.5 | 欧洲 | 峡湾、极昼、三文鱼 |",
      "| 26 | 瑞典 | 斯德哥尔摩 | 瑞典语 | 瑞典克朗 | 1040万 | 45 | 欧洲 | 宜家、诺贝尔奖、极光 |",
      "| 27 | 芬兰 | 赫尔辛基 | 芬兰语 | 欧元 | 550万 | 33.8 | 欧洲 | 桑拿、圣诞老人村、千湖之国 |",
      "| 28 | 荷兰 | 阿姆斯特丹 | 荷兰语 | 欧元 | 1750万 | 4.15 | 欧洲 | 风车、郁金香、自行车 |",
      "| 29 | 瑞士 | 伯尔尼 | 德语/法语/意大利语 | 瑞士法郎 | 870万 | 4.13 | 欧洲 | 阿尔卑斯山、手表、巧克力 |",
      "| 30 | 比利时 | 布鲁塞尔 | 荷兰语/法语/德语 | 欧元 | 1160万 | 3.06 | 欧洲 | 华夫饼、欧盟总部、巧克力 |",
      "| 31 | 奥地利 | 维也纳 | 德语 | 欧元 | 900万 | 8.39 | 欧洲 | 音乐之都、咖啡馆、阿尔卑斯山 |",
      "| 32 | 葡萄牙 | 里斯本 | 葡萄牙语 | 欧元 | 1030万 | 9.22 | 欧洲 | 大航海、蛋挞、法多 |",
      "| 33 | 希腊 | 雅典 | 希腊语 | 欧元 | 1040万 | 13.2 | 欧洲 | 神话、爱琴海、奥运发源地 |",
      "| 34 | 沙特阿拉伯 | 利雅得 | 阿拉伯语 | 里亚尔 | 3600万 | 215 | 亚洲 | 石油、麦加、沙漠 |",
      "| 35 | 阿联酋 | 阿布扎比 | 阿拉伯语 | 迪拉姆 | 1000万 | 8.36 | 亚洲 | 迪拜、哈利法塔、人工岛 |",
      "| 36 | 伊朗 | 德黑兰 | 波斯语 | 里亚尔 | 8800万 | 164 | 亚洲 | 波斯文明、地毯、清真寺 |",
      "| 37 | 秘鲁 | 利马 | 西班牙语 | 索尔 | 3300万 | 128 | 南美 | 马丘比丘、羊驼、印加文明 |",
      "| 38 | 哥伦比亚 | 波哥大 | 西班牙语 | 比索 | 5100万 | 114 | 南美 | 咖啡、翡翠、马尔克斯 |",
      "| 39 | 肯尼亚 | 内罗毕 | 斯瓦希里语/英语 | 肯尼亚先令 | 5400万 | 58 | 非洲 | 动物大迁徙、马拉松 |",
      "| 40 | 尼日利亚 | 阿布贾 | 英语 | 奈拉 | 2.2亿 | 92.4 | 非洲 | 诺莱坞、石油、非洲巨人 |",
    ].join("\n");

    const text = "# 世界国家信息表\n\n" + table;
    const chunks = chunkC2CMarkdownText({
      text,
      limit: 1500,
      strategy: "markdown-block",
    });

    expect(chunks.length).toBeGreaterThan(2);
    for (const chunk of chunks) {
      expect(Buffer.byteLength(chunk, "utf8")).toBeLessThanOrEqual(1200);
      expect(chunk).toContain("| 序号 | 国家 | 首都 | 官方语言 | 货币 | 人口（约） | 面积（万km²） | 所属大洲 | 特色 |");
      expect(chunk).toContain("|------|------|------|----------|------|------------|----------------|----------|------|");
    }

    for (let index = 1; index < chunks.length; index += 1) {
      expect(chunks[index]?.startsWith("# 世界国家信息表\n\n")).toBe(true);
    }
  });

  it("respects an explicit safe chunk byte limit override", () => {
    const table = [
      "| 序号 | 国家 | 首都 | 特色 |",
      "|------|------|------|------|",
      "| 1 | 中国 | 北京 | 长城、熊猫、火锅、故宫、兵马俑 |",
      "| 2 | 日本 | 东京 | 樱花、动漫、寿司、富士山、新干线 |",
      "| 3 | 韩国 | 首尔 | K-pop、韩剧、泡菜、烤肉、济州岛 |",
      "| 4 | 法国 | 巴黎 | 埃菲尔铁塔、卢浮宫、红酒、香水、面包 |",
      "| 5 | 德国 | 柏林 | 汽车、啤酒、黑森林、哲学、圣诞市集 |",
      "| 6 | 巴西 | 巴西利亚 | 足球、狂欢节、雨林、桑巴、伊瓜苏瀑布 |",
      "| 7 | 埃及 | 开罗 | 金字塔、尼罗河、法老、神庙、撒哈拉 |",
      "| 8 | 澳大利亚 | 堪培拉 | 袋鼠、大堡礁、考拉、海滩、内陆荒原 |",
    ].join("\n");

    const chunks = chunkC2CMarkdownText({
      text: table,
      limit: 1500,
      safeChunkByteLimit: 360,
      strategy: "markdown-block",
    });

    expect(chunks.length).toBeGreaterThan(2);
    for (const chunk of chunks) {
      expect(Buffer.byteLength(chunk, "utf8")).toBeLessThanOrEqual(360);
      expect(chunk).toContain("| 序号 | 国家 | 首都 | 特色 |");
      expect(chunk).toContain("|------|------|------|------|");
    }
  });

  it("keeps oversized wide-table rows valid instead of splitting them into orphan cells", () => {
    const table = [
      "| 序号 | 游戏名称 | 年份 | 类型 | 平台 | 开发商 | Metacritic | 玩法特色 | 一句话评价 | 经典元素 |",
      "|------|----------|------|------|------|--------|------------|----------|------------|----------|",
      "| 1 | 塞尔达传说：旷野之息 | 2017 | 开放世界 | Switch | 任天堂 | 97 | 自由探索/物理引擎 | 重新定义了开放世界 | 希卡之石/呀哈哈 |",
      "| 2 | 艾尔登法环 | 2022 | 动作RPG | 多平台 | FromSoftware | 96 | 魂系/开放世界 | 老贼的巅峰之作 | 葛瑞克/玛莲妮亚 |",
      "| 3 | 荒野大镖客：救赎2 | 2018 | 开放世界 | 多平台 | R星 | 97 | 沉浸式叙事/西部 | 游戏界的电影 | 亚瑟·摩根/约翰 |",
      "| 4 | 巫师3：狂猎 | 2015 | RPG | 多平台 | CDPR | 92 | 剧情驱动/开放世界 | 杰洛特的一生 | 希里/叶奈法 |",
      "| 5 | 超级马力欧：奥德赛 | 2017 | 平台跳跃 | Switch | 任天堂 | 97 | 附身机制/收集 | 玩具箱式的快乐 | 凯皮/帽子跳 |",
      "| 6 | 只狼：影逝二度 | 2019 | 动作 | 多平台 | FromSoftware | 91 | 弹反/拼刀 | 死了就再来一次 | 苇名弦一郎/苇名一心 |",
      "| 7 | 塞尔达传说：王国之泪 | 2023 | 开放世界 | Switch | 任天堂 | 96 | 究极手/造物 | 想象力即战斗力 | 究极手/余料建造 |",
      "| 8 | 上古卷轴5：天际 | 2011 | RPG | 多平台 | B社 | 94 | 沙盒/模组 | 我曾是龙裔 | Fus-Ro-Dah |",
      "| 9 | 荒野大镖客 | 2010 | 开放世界 | 多平台 | R星 | 95 | 西部/复仇 | 前传也是神作 | 约翰·马斯顿 |",
      "| 10 | 塞尔达传说：时之笛 | 1998 | 动作冒险 | N64 | 任天堂 | 99 | 时间旅行/3D标杆 | 3D游戏的教科书 | 时之笛/三角力量 |",
      "| 11 | 侠盗猎车手5 | 2013 | 开放世界 | 多平台 | R星 | 97 | 三主角/在线模式 | 卖了一个时代 | 崔佛/小富/麦克 |",
      "| 12 | 超级马力欧银河 | 2007 | 平台跳跃 | Wii | 任天堂 | 97 | 重力机制/太空 | 真正的3D马力欧 | 星之碎片/马力欧 |",
      "| 13 | 最终幻想7 | 1997 | RPG | PS | 史克威尔 | 92 | 回合制/剧情 | Cloud的故事 | 萨菲罗斯/爱丽丝 |",
      "| 14 | 超级马力欧64 | 1996 | 平台跳跃 | N64 | 任天堂 | 94 | 3D平台开创者 | 3D游戏的起点 | 120颗星星 |",
      "| 15 | 黑暗之魂 | 2011 | 动作RPG | 多平台 | FromSoftware | 89 | 魂系/高难度 | 菜就多练 | 葛温/传火祭祀场 |",
      "| 16 | 博德之门3 | 2023 | RPG | 多平台 | 拉瑞安 | 96 | 回合制/DnD规则 | 掷骰子的魅力 | 阿斯代伦/影心 |",
      "| 17 | 战神：诸神黄昏 | 2022 | 动作冒险 | PS5 | 圣莫尼卡 | 94 | 北欧神话/父子 | 阿特柔斯长大了 | 奎托斯/阿特柔斯 |",
      "| 18 | 我的世界 | 2011 | 沙盒 | 多平台 | Mojang | 93 | 建造/生存/红石 | 只要想就能创造 | 苦力怕/钻石 |",
      "| 19 | 我的世界地下城 | 2020 | 动作RPG | 多平台 | Mojang | 70 | 地牢/刷装备 | 轻量但上头 | 附魔/红石魔像 |",
    ].join("\n");

    const text = "# 必玩游戏推荐表\n\n" + table;
    const chunks = chunkC2CMarkdownText({
      text,
      limit: 1500,
      safeChunkByteLimit: 360,
      strategy: "markdown-block",
    });

    expect(chunks.length).toBeGreaterThan(2);
    const tableChunks = chunks.filter((chunk) =>
      chunk.includes("| 序号 | 游戏名称 | 年份 | 类型 | 平台 | 开发商 | Metacritic | 玩法特色 | 一句话评价 | 经典元素 |")
    );

    expect(tableChunks.length).toBeGreaterThan(1);
    for (const chunk of tableChunks) {
      expect(Buffer.byteLength(chunk, "utf8")).toBeLessThanOrEqual(360);
      expect(chunk).toContain("| 序号 | 游戏名称 | 年份 | 类型 | 平台 | 开发商 | Metacritic | 玩法特色 | 一句话评价 | 经典元素 |");
      expect(chunk).toContain("|------|----------|------|------|------|--------|------------|----------|------------|----------|");

      const bodyLines = chunk
        .replace(/^# 必玩游戏推荐表\n\n/, "")
        .split("\n")
        .slice(2)
        .filter(Boolean);
      expect(bodyLines.every((line) => (line.match(/\|/g) ?? []).length === 11)).toBe(true);
      expect(bodyLines).not.toContain("| Fus-Ro-Dah |");
    }
  });

  it("uses a more conservative auto chunk size for very wide tables", () => {
    const table = [
      "| 序号 | 食物 | 菜系/地区 | 主要食材 | 口味 | 烹饪方式 | 难度 | 热量（约） | 推荐指数 | 备注 |",
      "|------|------|-----------|----------|------|----------|------|------------|----------|------|",
      "| 1 | 北京烤鸭 | 京菜 | 鸭子/甜面酱 | 咸甜 | 烤 | ★★★★ | 800kcal | ★★★★★ | 全聚德/大董 |",
      "| 2 | 麻婆豆腐 | 川菜 | 豆腐/牛肉末/花椒 | 麻辣 | 炒 | ★★ | 200kcal | ★★★★★ | 陈麻婆原创 |",
      "| 3 | 宫保鸡丁 | 川菜 | 鸡丁/花生/干辣椒 | 酸辣微甜 | 爆炒 | ★★★ | 350kcal | ★★★★☆ | 国民菜 |",
      "| 4 | 红烧肉 | 本帮菜 | 五花肉/冰糖 | 咸甜浓油 | 炖 | ★★★ | 600kcal | ★★★★★ | 毛主席最爱 |",
      "| 5 | 小笼包 | 上海 | 猪肉/皮冻 | 鲜香 | 蒸 | ★★★★ | 300kcal | ★★★★★ | 南翔馒头店 |",
      "| 6 | 酸菜鱼 | 川渝 | 草鱼/酸菜 | 酸辣 | 煮 | ★★★ | 400kcal | ★★★★★ | 下饭神器 |",
      "| 7 | 回锅肉 | 川菜 | 二刀肉/蒜苗/豆瓣酱 | 香辣 | 炒 | ★★ | 450kcal | ★★★★★ | 川菜之王 |",
      "| 8 | 东坡肉 | 杭帮菜 | 五花肉/黄酒 | 甜咸软糯 | 慢炖 | ★★★★ | 550kcal | ★★★★★ | 苏东坡发明 |",
      "| 9 | 番茄炒蛋 | 家常 | 番茄/鸡蛋 | 酸甜 | 炒 | ★ | 180kcal | ★★★★★ | 华人厨艺第一课 |",
      "| 10 | 糖醋里脊 | 鲁菜 | 猪里脊/醋/糖 | 酸甜 | 炸+炒 | ★★★ | 400kcal | ★★★★☆ | 老少皆宜 |",
      "| 11 | 水煮鱼 | 川菜 | 鱼片/豆芽/辣椒 | 麻辣 | 煮 | ★★★ | 350kcal | ★★★★★ | 辣到飞起 |",
      "| 12 | 蛋炒饭 | 家常 | 米饭/鸡蛋/葱花 | 咸香 | 炒 | ★ | 350kcal | ★★★★★ | 最简也最难 |",
      "| 13 | 兰州牛肉面 | 西北 | 牛肉/拉面/萝卜 | 清香 | 煮 | ★★★★ | 500kcal | ★★★★★ | 一清二白三红四绿 |",
      "| 14 | 粤式早茶点心 | 粤菜 | 虾/猪肉/粉 | 鲜 | 蒸/炸 | ★★★★ | 300kcal | ★★★★★ | 一盅两件 |",
      "| 15 | 叉烧 | 粤菜 | 猪颈肉/蜂蜜 | 甜香 | 烤 | ★★★ | 350kcal | ★★★★☆ | 半肥瘦最香 |",
      "| 16 | 佛跳墙 | 闽菜 | 鲍鱼/海参/鱼翅等 | 醇厚 | 慢炖 | ★★★★★ | 800kcal | ★★★★★ | 闽菜之王 |",
      "| 17 | 叫花鸡 | 苏菜 | 整鸡/荷叶 | 咸香 | 烤 | ★★★★ | 600kcal | ★★★★☆ | 叫花子发明 |",
      "| 18 | 烤羊肉串 | 西北 | 羊肉/孜然/辣椒面 | 咸辣香 | 烤 | ★★ | 250kcal | ★★★★★ | 新疆灵魂 |",
      "| 19 | 抄手/馄饨 | 川渝/江南 | 猪肉/面皮 | 鲜香 | 煮 | ★★ | 200kcal | ★★★★☆ | 红油抄手YYDS |",
      "| 20 | 煎饼果子 | 天津 | 面糊/鸡蛋/薄脆 | 咸香 | 煎 | ★★★ | 400kcal | ★★★★★ | 天津早餐标配 |",
      "| 21 | 臭豆腐 | 湖南/南京 | 豆腐/卤水 | 香辣 | 炸 | ★★★ | 200kcal | ★★★★☆ | 闻着臭吃着香 |",
    ].join("\n");

    const chunks = chunkC2CMarkdownText({
      text: table,
      limit: 1500,
      strategy: "markdown-block",
    });

    const tableChunks = chunks.filter((chunk) =>
      chunk.includes("| 序号 | 食物 | 菜系/地区 | 主要食材 | 口味 | 烹饪方式 | 难度 | 热量（约） | 推荐指数 | 备注 |")
    );

    expect(tableChunks.length).toBeGreaterThan(2);
    expect(tableChunks.length).toBeLessThanOrEqual(8);
    for (const chunk of tableChunks) {
      expect(Buffer.byteLength(chunk, "utf8")).toBeLessThanOrEqual(1000);
      expect(chunk).toContain("|------|------|-----------|----------|------|----------|------|------------|----------|------|");

      const bodyLines = chunk.split("\n").slice(2).filter(Boolean);
      expect(bodyLines.every((line) => (line.match(/\|/g) ?? []).length === 11)).toBe(true);
      expect(bodyLines).not.toContain("| 酸甜 | 炸+炒 | ★★★ | 400kcal | ★★★★☆ | 老少皆宜 |");
    }
  });

  it("packs anime-table rows greedily and never emits orphan continuation rows", () => {
    const table = [
      "| 序号 | 动漫名 | 年份 | 类型 | 集数 | 制作公司 | 豆瓣评分 | 题材标签 | 代表角色 | 一句话 |",
      "|------|--------|------|------|------|----------|----------|----------|----------|--------|",
      "| 1 | 钢之炼金术师FA | 2009 | 热血/奇幻 | 64 | 骨头社 | 9.6 | 等价交换/兄弟 | 爱德华/阿尔冯斯 | 神作不需要解释 |",
      "| 2 | 进击的巨人 | 2013 | 热血/末日 | 87 | WIT/MAPPA | 9.8 | 巨人/人类存亡 | 艾伦/三笠/利威尔 | 进击的剧情 |",
      "| 3 | 命运石之门 | 2011 | SF/悬疑 | 24 | WHITE FOX | 9.4 | 时间旅行 | 冈部伦太郎 | El Psy Kongroo |",
      "| 4 | CLANNAD | 2007 | 恋爱/治愈 | 44 | 京都动画 | 9.4 | 家庭/成长 | 冈崎朋也/渚 | 写给生活的情书 |",
      "| 5 | 灌篮高手 | 1993 | 热血/运动 | 101 | 东映 | 9.7 | 篮球/青春 | 樱木花道/流川枫 | 教练我想打篮球 |",
      "| 6 | 死亡笔记 | 2006 | 悬疑/心理 | 37 | MADHOUSE | 9.2 | 智斗/正义 | 夜神月/L | 新世界的神 |",
      "| 7 | 钢之炼金术师03 | 2003 | 奇幻/悲剧 | 51 | 骨头社 | 9.1 | 原创结局 | 爱德华 | 和FA不同的悲伤 |",
      "| 8 | 千与千寻 | 2001 | 奇幻/冒险 | 1 | 吉卜力 | 9.4 | 成长/异世界 | 千寻/白龙 | 宫崎骏的巅峰 |",
      "| 9 | 你的名字 | 2016 | 恋爱/奇幻 | 1 | 新海诚团队 | 8.4 | 穿越时空 | 泷/三叶 | 黄昏之时 |",
      "| 10 | 新世纪福音战士 | 1995 | 机甲/心理 | 26 | GAINAX | 9.4 | EVA/宗教/存在 | 碇真嗣/明日香 | 动画史的转折点 |",
      "| 11 | 反叛的鲁路修 | 2006 | 机甲/智斗 | 50 | SUNRISE | 9.2 | 叛逆/复仇 | 鲁路修/C.C. | 错的不是我，是世界 |",
      "| 12 | 一拳超人 | 2015 | 热血/搞笑 | 12 | MADHOUSE | 9.4 | 英雄/无敌 | 埼玉/杰诺斯 | 我是个兴趣使然的英雄 |",
      "| 13 | 夏目友人帐 | 2008 | 治愈/日常 | 74 | 脑洞/朱夏 | 9.3 | 妖怪/温柔 | 夏目贵志/猫咪老师 | 温柔的力量 |",
      "| 14 | 四月是你的谎言 | 2014 | 恋爱/音乐 | 22 | A-1 Pictures | 8.9 | 音乐/成长 | 有马公生/宫园薰 | 春/今/明/後 |",
      "| 15 | FATE/ZERO | 2011 | 奇幻/战斗 | 13 | ufotable | 8.9 | 圣杯战争 | 卫宫切嗣/吉尔伽美什 | 大人看的Fate |",
      "| 16 | 全职猎人 | 2011 | 热血/冒险 | 148 | MADHOUSE | 9.3 | 念能力/黑暗 | 小杰/奇犽 | 富坚义博别再休刊了 |",
      "| 17 | 紫罗兰永恒花园 | 2018 | 治愈/催泪 | 13 | 京都动画 | 9.1 | 战后/信件 | 薇尔莉特 | 你会爱上你自己的 |",
      "| 18 | 齐木楠雄的灾难 | 2016 | 搞笑/日常 | 24 | J.C.STAFF | 9.3 | 超能力/吐槽 | 齐木楠雄 | 最强超能力者的日常 |",
      "| 19 | 命运之夜UBW | 2014 | 奇幻/战斗 | 25 | ufotable | 9.1 | 圣杯战争/理想 | 卫宫士郎/远坂凛 | 这条线最王道 |",
    ].join("\n");

    const expectedRows = table.split("\n").slice(2);
    const chunks = chunkC2CMarkdownText({
      text: table,
      limit: 1500,
      safeChunkByteLimit: 600,
      strategy: "markdown-block",
    });

    expect(chunks.length).toBeGreaterThan(2);
    for (const chunk of chunks) {
      expect(Buffer.byteLength(chunk, "utf8")).toBeLessThanOrEqual(600);
      expect(chunk).toContain("| 序号 | 动漫名 | 年份 | 类型 | 集数 | 制作公司 | 豆瓣评分 | 题材标签 | 代表角色 | 一句话 |");
      expect(chunk).toContain("|------|--------|------|------|------|----------|----------|----------|----------|--------|");

      const bodyLines = chunk.split("\n").slice(2).filter(Boolean);
      expect(bodyLines.every((line) => (line.match(/\|/g) ?? []).length === 11)).toBe(true);
      expect(bodyLines).not.toContain("| 8.4 | 穿越时空 | 泷/三叶 | 黄昏之时 |");
    }

    const renderedRows = chunks.flatMap((chunk) => chunk.split("\n").slice(2).filter(Boolean));
    expect(renderedRows).toEqual(expectedRows);
  });

  it("keeps fenced code blocks closed after splitting", () => {
    const chunks = chunkC2CMarkdownText({
      text: "```ts\nconst a = 1;\nconst b = 2;\nconst c = 3;\n```",
      limit: 28,
      strategy: "markdown-block",
    });

    expect(chunks.length).toBeGreaterThan(1);
    for (const chunk of chunks) {
      expect(chunk.startsWith("```ts\n")).toBe(true);
      expect(chunk.endsWith("\n```")).toBe(true);
    }
  });

  it("keeps blockquote prefixes when splitting long quotes", () => {
    const chunks = chunkC2CMarkdownText({
      text: "> 第一行引用内容\n> 第二行引用内容\n> 第三行引用内容",
      limit: 48,
      strategy: "markdown-block",
    });

    expect(chunks.length).toBeGreaterThan(1);
    for (const chunk of chunks) {
      for (const line of chunk.split("\n")) {
        if (!line.trim()) continue;
        expect(line.startsWith(">")).toBe(true);
      }
    }
  });

  it("avoids splitting common inline markdown markers across chunks", () => {
    const chunks = chunkC2CMarkdownText({
      text: "这是 **加粗内容** 和 `inline-code` 的说明，还有一些补充文字用于触发切分。",
      limit: 64,
      strategy: "markdown-block",
    });

    for (const chunk of chunks) {
      expect((chunk.match(/\*\*/g) ?? []).length % 2).toBe(0);
      expect((chunk.match(/`/g) ?? []).length % 2).toBe(0);
    }
  });

  it("uses the fallback chunker unchanged in length mode", () => {
    const fallbackChunkText = vi.fn((text: string) => [text.slice(0, 4), text.slice(4)]);

    const chunks = chunkC2CMarkdownText({
      text: "# 标题\n\n第一段",
      limit: 8,
      strategy: "length",
      fallbackChunkText,
    });

    expect(fallbackChunkText).toHaveBeenCalledWith("# 标题\n\n第一段");
    expect(chunks).toEqual(["# 标题", "\n\n第一段"]);
  });
});
