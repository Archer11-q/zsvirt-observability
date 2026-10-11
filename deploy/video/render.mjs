/**
 * 包装层渲染器：把 cards.html 逐帧渲成 PNG 序列（含透明通道）。
 *
 * ## 为什么是逐帧 PNG 序列，而不是录一段带 alpha 的视频
 *
 * 透明视频要么用 `qtrle`（.mov，体积大得离谱）要么用 VP9 alpha（编码慢、
 * 且 2018 版的 ffmpeg 未必支持）。PNG 序列两个问题都没有，而且**中间产物可直查** ——
 * 出问题时能直接打开第 137 帧看到底错在哪，这是排查速度的关键。
 *
 * ## 为什么用系统 Chrome 而不是 puppeteer 自带的 Chromium
 *
 * `puppeteer-core` 不自带浏览器（省掉约 150 MB 下载），直接驱动本机已装的
 * Chrome/Edge。本机两个都有，实测可用。
 *
 * 用法：
 *   node render.mjs                # 按 timeline.json 渲染全部包装层
 *   node render.mjs --qa           # 额外输出静帧自检图到 build/qa/
 *   node render.mjs --still title 1.6   # 只渲一张指定时刻的静帧
 */

import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import puppeteer from "puppeteer-core";

const HERE = dirname(fileURLToPath(import.meta.url));
const OVERLAY_DIR = join(HERE, "overlay");
const BUILD_DIR = join(HERE, "build");
const CARDS_URL = pathToFileURL(join(OVERLAY_DIR, "cards.html")).href;

const CHROME_CANDIDATES = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
];

function findBrowser() {
  const found = CHROME_CANDIDATES.find((p) => existsSync(p));
  if (!found) {
    throw new Error(
      "找不到 Chrome 或 Edge。装一个，或在 CHROME_CANDIDATES 里加上你的路径。\n" +
        "（刻意不自动下载 Chromium：那要多下约 150 MB，而本机已经有两个浏览器。）"
    );
  }
  return found;
}

function readTimeline() {
  const path = join(HERE, "timeline.json");
  if (!existsSync(path)) {
    throw new Error(`缺少 ${path}——从 timeline.example.json 复制一份再填。`);
  }
  /* 去掉 BOM 再解析。
   *
   * 这条不是洁癖：`timeline.json` 是要**在 Windows 上手工编辑**的文件，
   * 而 PowerShell 的 `Set-Content -Encoding UTF8`（5.1）与记事本"UTF-8"选项
   * 都会写入 BOM。`JSON.parse` 遇到 BOM 直接抛 "Unexpected token"，
   * 而报错信息看起来像是 JSON 语法写错了 —— 会让人去翻花括号，
   * 实际问题在文件头三个不可见字节上。实测踩到过。 */
  const raw = readFileSync(path, "utf8").replace(/^\uFEFF/, "");
  return JSON.parse(raw);
}

/** 一个待渲染的包装层条目。 */
function collectItems(timeline) {
  const items = [];

  // 常驻来源角标：整片都要，渲一次序列即可（内容是静态的）
  if (timeline.badge) {
    items.push({
      id: "badge",
      kind: "badge",
      params: timeline.badge,
      seconds: timeline.badgeSeconds ?? 1,
      alpha: true,
    });
  }

  for (const clip of timeline.clips ?? []) {
    if (clip.card) {
      items.push({
        id: `card-${clip.id}`,
        kind: clip.card.kind,
        params: clip.card.params ?? {},
        seconds: clip.card.seconds ?? 2.6,
        alpha: false, // 卡片是不透明的，铺满画面
      });
    }
    for (const ov of clip.overlays ?? []) {
      /* 两个时间概念必须分开 —— 这里踩过坑：
       *
       * - `from` / `to`：图层在**录屏时间轴**上的可见区间，由 timeline.json 给。
       * - `animSeconds`：入场动画的长度，之后静止保持。
       *
       * 一开始只有一个 `seconds` 字段，被同时当成"图层时长"和"动画时长"用：
       * 统计栈写了 `seconds: 0.9`（本意是动画 0.9 秒），于是它在成片里只出现 0.9 秒
       * 就消失了 —— 而渲染日志、合成日志、时长自检**全都是绿的**。
       * 一个字段两种含义，出错是迟早的事。
       */
      const windowSeconds =
        ov.from !== undefined && ov.to !== undefined
          ? Math.max(0.1, Number(ov.to) - Number(ov.from))
          : Number(ov.seconds ?? 1);

      items.push({
        id: `ov-${ov.id}`,
        kind: ov.kind,
        params: ov.params ?? {},
        seconds: windowSeconds,
        animSeconds: ov.animSeconds ?? Math.min(0.6, windowSeconds),
        alpha: true,
      });
    }
  }
  return items;
}

async function main() {
  const argv = process.argv.slice(2);
  const qa = argv.includes("--qa");
  const stillIdx = argv.indexOf("--still");

  const timeline = readTimeline();
  const fps = timeline.fps ?? 30;
  const width = timeline.width ?? 1920;
  const height = timeline.height ?? 1080;

  const browser = await puppeteer.launch({
    executablePath: findBrowser(),
    headless: true,
    args: ["--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1"],
  });

  const page = await browser.newPage();
  await page.setViewport({ width, height, deviceScaleFactor: 1 });
  await page.goto(CARDS_URL, { waitUntil: "load" });

  /* 字体就绪再截图。中文系统字体偶尔会晚到一帧，那一帧的字宽不对 ——
     表现为成片里"某帧字突然变宽"，抽帧回看才看得出来。 */
  await page.evaluate(() => document.fonts.ready);

  const items = collectItems(timeline);

  if (stillIdx !== -1) {
    const wantKind = argv[stillIdx + 1];
    const at = Number(argv[stillIdx + 2] ?? 1.5);
    const item = items.find((i) => i.id === wantKind || i.kind === wantKind);
    if (!item) throw new Error(`没有 id 或 kind 为 ${wantKind} 的条目`);
    const dir = join(BUILD_DIR, "qa");
    mkdirSync(dir, { recursive: true });
    await renderOne(page, item, at, join(dir, `${item.id}.png`));
    console.log(`静帧已输出：build/qa/${item.id}.png  (t=${at}s)`);
    await browser.close();
    return;
  }

  rmSync(join(BUILD_DIR, "overlays"), { recursive: true, force: true });

  let total = 0;
  let shots = 0;
  for (const item of items) {
    const outDir = join(BUILD_DIR, "overlays", item.id);
    mkdirSync(outDir, { recursive: true });
    const frames = Math.max(1, Math.round(item.seconds * fps));
    const animFrames = item.alpha
      ? Math.min(frames, Math.max(1, Math.round((item.animSeconds ?? item.seconds) * fps)))
      : frames;

    await page.evaluate((cfg) => window.__setup(cfg), { kind: item.kind, params: item.params });

    let lastPath = null;
    for (let f = 0; f < frames; f++) {
      const file = join(outDir, String(f).padStart(5, "0") + ".png");
      if (f < animFrames) {
        // 动画前缀：真的逐帧渲染（截图是整条流水线最慢的一步）
        await page.evaluate((tt) => window.__frame(tt), f / fps);
        await page.screenshot({ path: file, omitBackground: item.alpha });
        shots++;
      } else {
        // 静止保持：复制最后一张。逐帧完全一致，重算没有意义。
        copyFileSync(lastPath, file);
      }
      lastPath = file;
    }

    total += frames;
    const note = animFrames < frames ? `（渲 ${animFrames} 帧，其余复制）` : "";
    console.log(
      `  ✓ ${item.id.padEnd(22)} ${item.kind.padEnd(8)} ${String(frames).padStart(4)} 帧  (${item.seconds}s) ${note}`
    );

    if (qa && item.kind !== "badge") {
      const qaDir = join(BUILD_DIR, "qa");
      mkdirSync(qaDir, { recursive: true });
      const tQa = Math.min(item.seconds * 0.75, item.animSeconds ?? item.seconds);
      await renderOne(page, item, tQa, join(qaDir, `${item.id}.png`));
    }
  }

  await browser.close();
  console.log(`\n共 ${items.length} 个包装层条目，${total} 帧（实际截图 ${shots} 张）→ build/overlays/`);
  if (qa) console.log("静帧自检图 → build/qa/");
}

async function renderOne(page, item, t, outPath) {
  await page.evaluate((cfg) => window.__setup(cfg), { kind: item.kind, params: item.params });
  await page.evaluate((tt) => window.__frame(tt), t);
  await page.screenshot({ path: outPath, omitBackground: item.alpha });
}

main().catch((e) => {
  console.error("\n✗ 渲染失败：" + e.message);
  process.exit(1);
});
