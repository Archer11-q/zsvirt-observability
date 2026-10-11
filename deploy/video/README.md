# 演示视频制作流水线（交付要求 g）

> 赛题交付要求 g：「提供 3-5 分钟**录屏**，展示**部署、正常观测、故障注入、
> 跨层关联和诊断输出**的完整过程。」
>
> 本目录是那支录屏的**包装层与合成工具**：你负责录，它负责把片头、分段标题卡、
> 常驻来源角标、字幕和片尾叠上去，并输出成片。
>
> 制作流程、设计依据与偏离记录见 [`design-spec.md`](design-spec.md)。
> 录制时的操作顺序与旁白/字幕文本见 [`../../docs/DEMO_SCRIPT.md`](../../docs/DEMO_SCRIPT.md)。

---

## 为什么主体必须是录屏，而不是宣传片

我们评估过用 `video-shotcraft` 的 Ink Press 模板直接做一支宣传片。放弃了，两个理由：

1. 赛题要的是**录屏**，要看到**完整操作过程**；宣传片是形式上的替换，不满足要求。
   官方答复第 15 条也明确「降级条款不能直接推导为全模拟可替代最终环境演示」。
2. Ink Press 是纸质暖调，与产品的深色冷调控制台（`#0F1720` + `#0E7C86`）冲突；
   skill 自己的理念 2 要求「视频视觉必须从产品自身生长」。

所以取它的**包装层能力与节奏纪律**，主体留给真实录屏。

---

## 一、准备（一次性）

```bash
cd deploy/video
npm install                     # 只装 puppeteer-core + 两个二进制包，约 30 秒
cp timeline.example.json timeline.json
```

**依赖为什么是这三个**：

| 包 | 为什么是它 |
|---|---|
| `puppeteer-core` | **不带浏览器**。本机已有 Chrome/Edge，省掉约 150 MB 的 Chromium 下载 |
| `@ffmpeg-installer/ffmpeg` | 二进制**打进 npm 包**，只走 `registry.npmjs.org`。对比：`ffmpeg-static` 在 postinstall 从 GitHub Releases 下载，而本机 `github.com` 不可达（实测） |
| `@ffprobe-installer/ffprobe` | 用来做时长自检（官方 3–5 分钟是硬区间） |

## 二、录制

录制清单（终端布局、逐条命令、要看到的输出）见
[`../../docs/DEMO_SCRIPT.md`](../../docs/DEMO_SCRIPT.md) 的「录制操作手册」。

**录成一条连续的视频**即可 —— 不需要边录边暂停。录完把每段在原始时间轴上的
起止秒数填进 `timeline.json` 的 `source.in` / `source.out`（剪接由本工具做）。

规格：**1920×1080 @ 30fps**。放 `raw/recording.mp4`。

## 三、配置

`timeline.json` 是**唯一配置源**，渲染与合成共同读它。录制后你只需要改两处：

```jsonc
{
  "recording": "raw/recording.mp4",        // ← 1. 你的录屏路径
  "clips": [
    {
      "id": "deploy",
      "source": { "in": 0.0, "out": 26.0 } // ← 2. 这一段在原始录屏里的起止秒
    }
  ]
}
```

分段卡的文案、字幕文本、角标三行都在这个文件里 —— **改文案不用碰 HTML/JS**。

### overlay 的两个时间概念（不要混用）

| 字段 | 含义 |
|---|---|
| `from` / `to` | 图层在**录屏时间轴**上的可见区间（秒） |
| `animSeconds` | **入场动画**长度（秒），之后静止保持 |

> ⚠️ 这两者混用会出**静默故障**：第一版只有一个 `seconds` 字段承担两种含义，
> 统计栈（本意动画 0.9 秒）在成片里只出现 0.9 秒就消失了，
> 而渲染日志、合成日志、时长自检**全都是绿的**。

## 四、生成

```bash
npm run render        # 渲染包装层（约 900 张截图，1–2 分钟）
npm run compose       # 剪接合成 → out/crosslayer-demo.mp4
npm run review        # 额外抽帧到 out/review/（每 10 秒一张，用于回看）
```

只改了某一段的字幕想快速看效果：

```bash
node render.mjs && node compose.mjs --clip inject
```

`compose` 结束时会打印**时长自检**：

```
  时长 4:32.6　✓ 落在官方 3–5 分钟区间内
```

超出 3–5 分钟会明确报 ✗ —— 这是硬区间，别靠肉眼估。

## 五、包装层由什么组成

| 文件 | 作用 |
|---|---|
| `overlay/cards.html` | 全部卡片与叠加层的 DOM + **帧时钟**（`window.__frame(t)`） |
| `overlay/overlay.css` | 产品 token 与版式（颜色全部取证自 `frontend/src/theme.ts`） |
| `render.mjs` | 用系统 Chrome 逐帧渲成 PNG 序列到 `build/overlays/` |
| `compose.mjs` | ffmpeg：裁剪 → 段内叠加 → 拼段 → **整段叠角标** → 全片拼接 |
| `build/qa/` | 静帧自检图（每个条目的定格画面） |

### 每段的结构（顺序有语义）

```
  [分段卡]  +  [录屏段 ⊕ 该段字幕/统计/白闪]   →  拼接  →  ⊕ 常驻来源角标
    不透明            叠在录屏上                 重编码       叠在整段上
```

- 段内字幕/统计是**相对录屏时间轴**的 → 必须叠在拼接**之前**。
- 来源角标要盖住**整段（含分段卡）** → 必须叠在拼接**之后**。
  官方答复第 13 条要求「视频中标明来源」，角标在分段卡上缺席就是漏标。

## 六、来源角标是硬要求，不是装饰

官方第一轮答复第 13 条原文：

> 「建议在采集记录、API 响应、界面、诊断证据**和视频中**一致标明来源，
> 并区分真实采集、真实数据回放和人工模拟。」

任务书 g 全文没有出现「旁白 / 讲解 / 语音」—— **人声不是必需项**；
但「视频中标明来源」是明确点到的。所以本片**不打旁白，改用**：

1. **右上角常驻三行角标**（`timeline.json` 的 `badge`）
2. **底部常驻字幕条**（每个 clip 的 `overlays`）

**改角标内容之前先确认你有依据说不同的话** —— 那三行是对官方的承诺。

## 七、交付前的独立终检（还没做）

`video-shotcraft` 要求：交付前派一个**干净上下文的 subagent** 做独立终检，
逐条出**带帧号证据**的报告，输入包含成片、关键帧、`design-spec.md`、分镜与数据口径。
理由是「制作者对自己的产出有确认偏差，首检永远不能交给用户」。

**必须在真实录屏合成成片之后执行** —— 现在审的会是合成录屏，没有意义。
`design-spec.md` §8 记录了当前的终检状态。

## 八、目录

```
deploy/video/
├── README.md              本文
├── design-spec.md         设计 spec（终检基准）
├── timeline.example.json  配置模板（复制成 timeline.json）
├── render.mjs             包装层逐帧渲染
├── compose.mjs            剪接合成 + 时长自检
├── overlay/
│   ├── cards.html         卡片与叠加层 + 帧时钟
│   └── overlay.css        产品 token 与版式
├── raw/                   你的原始录屏（不进 git）
├── build/                 中间产物（不进 git）
└── out/                   成片与回看帧（不进 git）
```
