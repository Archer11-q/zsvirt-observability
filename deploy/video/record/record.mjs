/**
 * 录制器：驱动**真实终端**与**真实前端界面**，用 Chrome CDP screencast 逐段录下来。
 *
 * ## 录的是什么（这一点必须说清楚）
 *
 * - 终端段：画面上每一行都来自 WSL 里一个**真正的 bash 伪终端**（`pty_server.py`），
 *   命令是真的执行的，输出是真的产生的。
 * - 界面段：iframe 加载的是**真实运行的 Vite dev server**（:5173），
 *   数据来自**真实后端**（:8080）。
 *
 * 两者都由真实 Chrome 渲染后被 CDP screencast 逐帧录下 —— 不是事后重排的回放。
 *
 * ## 为什么逐段录、而不是录一整条
 *
 * 一整条要求每一段的节奏在实时录制中一次到位，任何一段卡住就得整条重录。
 * 逐段录每段可独立重跑（`--only inject`），而且每段的时长可以精确控制。
 * 录完把各段拼成 `raw/recording.mp4`，并把累计时间轴写进 `build/segments.json`，
 * 供 `../compose.mjs` 的 timeline.json 使用。
 *
 * ## 为什么用 headless
 *
 * 录制的是**页面内容**，浏览器窗口边框不进画面 —— 演示视频里那是噪声。
 * 画面本身完全一样：同一份 DOM、同一套字体渲染、同一个 GPU 合成路径。
 *
 * 用法：
 *   node record.mjs                 # 录全部段
 *   node record.mjs --only inject   # 只重录某一段
 *   node record.mjs --plan          # 只打印计划，不录
 */

import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer-core";

const require = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
const VIDEO_DIR = join(HERE, "..");
const RAW = join(VIDEO_DIR, "raw");
const BUILD = join(VIDEO_DIR, "build");
const FRAMES = join(BUILD, "record-frames");

const FFMPEG = require("@ffmpeg-installer/ffmpeg").path;
const FFPROBE = require("@ffprobe-installer/ffprobe").path;

const STUDIO = "http://127.0.0.1:5199/";
const CHROME = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
].find((p) => existsSync(p));

function ffmpeg(args, label) {
  const r = spawnSync(FFMPEG, args, { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
  if (r.status !== 0) {
    const tail = String(r.stderr || "").trim().split("\n").slice(-25).join("\n");
    throw new Error(`ffmpeg 失败（${label}）\n${tail}`);
  }
}
function duration(file) {
  const r = spawnSync(FFPROBE, ["-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", file], { encoding: "utf8" });
  return Number(String(r.stdout || "").trim());
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 把一串带时间戳的帧组装成 mp4。
 *
 * ## 为什么是"重复帧 + 固定帧率"，而不是"帧 + duration"
 *
 * 直觉做法是给 concat 列表写 `duration`，让 ffmpeg 按真实时间戳出可变帧率。
 * **实测不行**：一个 37.96 秒的长 duration 被 ffmpeg 直接丢掉了 ——
 * 段墙钟 46.65s、concat 时长合计 46.65s，成段却只有 8.69s，
 * 而 8.69 恰好等于 46.65 减去那个 37.96。合成命令不报任何错。
 *
 * 现在改成：按 duration 把每一帧**重复**成 30fps 的固定帧序列，交给 ffmpeg
 * 一个老老实实的定帧率序列。`render.mjs` 的包装层走的就是这条路，时长一直是对的。
 * 代价是列表变长（约 30 行/秒），但它是**可验证**的 ——
 * 行数除以 30 就是时长，一眼能对上。 */
function framesToMp4(frameList, dir, outFile, label, fps = 30) {
  mkdirSync(dir, { recursive: true });

  let list = "";
  let emitted = 0;
  let cum = 0; // 累计真实时间：**逐帧四舍五入会累积误差**
  frameList.forEach((f, idx) => {
    const name = `${String(f.i).padStart(5, "0")}.jpg`;
    writeFileSync(join(dir, name), Buffer.from(f.data, "base64"));

    const next = idx + 1 < frameList.length ? frameList[idx + 1].ts : f.ts + 1 / fps;
    cum += Math.max(0, next - f.ts);
    const target = Math.round(cum * fps);
    const hold = Math.max(0, target - emitted); // 允许丢帧：60fps 采集降采样到 30fps 时，强制每帧至少 1 会重复计数
    emitted += hold;
    const abs = join(dir, name).replace(/\\/g, "/");
    for (let k = 0; k < hold; k++) list += `file '${abs}'\n`;
  });

  writeFileSync(join(dir, "concat.txt"), list);
  ffmpeg([
    "-y", "-hide_banner", "-loglevel", "error",
    "-f", "concat", "-safe", "0", "-r", String(fps), "-i", join(dir, "concat.txt"),
    "-c:v", "libx264", "-preset", "medium", "-crf", "18",
    "-pix_fmt", "yuv420p", "-r", String(fps),
    outFile,
  ], label);
  return emitted / fps;
}

/** 执行一条脚本动作。 */
async function runAction(page, act, segId) {
  switch (act.kind) {
    case "pane":
      await page.evaluate((a) => window.__addPane(a.id, a.title, a.badge, !!a.solo), act);
      return;
    case "removePane":
      await page.evaluate((a) => window.__removePane(a.id), act);
      return;
    case "term":
      await page.evaluate(() => window.__showTerm());
      return;
    case "app":
      await page.evaluate((a) => window.__showApp(a.route, a.tab), act);
      return;
    case "exec": {
      console.log(`      $ ${act.cmd}`);
      await page.evaluate((a) => window.__exec(a.id, a.cmd, a.typingDelay ?? 0.03), act);
      return;
    }
    case "send":
      await page.evaluate((a) => window.__send(a.id, a.text), act);
      return;
    case "interrupt":
      await page.evaluate((a) => window.__interrupt(a.id), act);
      return;
    case "clear":
      await page.evaluate((a) => window.__clear(a.id), act);
      return;
    case "waitFor": {
      const ok = await page.evaluate(
        (a) => window.__waitFor(a.id, a.needle, a.timeout ?? 45000),
        act
      );
      if (!ok) console.log(`      ⚠ 等待超时：${act.id} 未出现「${act.needle}」——继续录，但请复核画面`);
      return;
    }
    case "wait":
      await sleep((act.seconds ?? 1) * 1000);
      return;
    default:
      throw new Error(`片段 ${segId} 里有未知动作：${act.kind}`);
  }
}

async function main() {
  const argv = process.argv.slice(2);
  const onlyIdx = argv.indexOf("--only");
  const only = onlyIdx !== -1 ? argv[onlyIdx + 1] : null;
  const planOnly = argv.includes("--plan");

  if (!CHROME) throw new Error("找不到 Chrome / Edge");
  const script = JSON.parse(readFileSync(join(HERE, "segments.json"), "utf8").replace(/^\uFEFF/, ""));

  if (planOnly) {
    for (const seg of script.segments) {
      const secs = seg.actions.filter((a) => a.kind === "wait").reduce((s, a) => s + (a.seconds ?? 0), 0);
      console.log(`  ${seg.id.padEnd(12)} 显式等待 ${secs}s  动作 ${seg.actions.length} 条`);
    }
    return;
  }

  mkdirSync(RAW, { recursive: true });
  mkdirSync(FRAMES, { recursive: true });

  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: true,
    args: ["--no-sandbox", "--hide-scrollbars", "--force-device-scale-factor=1", "--window-size=1920,1080"],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1920, height: 1080, deviceScaleFactor: 1 });
  await page.goto(STUDIO, { waitUntil: "load" });
  await page.evaluate(() => document.fonts.ready);

  const cdp = await page.createCDPSession();
  let frames = [];
  let capturing = false;
  cdp.on("Page.screencastFrame", async (f) => {
    if (capturing) frames.push({ i: frames.length, data: f.data, ts: f.metadata.timestamp });
    try { await cdp.send("Page.screencastFrameAck", { sessionId: f.sessionId }); } catch {}
  });

  const timeline = [];
  let cursor = 0;

  for (const seg of script.segments) {
    const outFile = join(RAW, `seg-${seg.id}.mp4`);
    if (only && seg.id !== only) {
      if (existsSync(outFile)) {
        const d = duration(outFile);
        timeline.push({ id: seg.id, in: cursor, out: cursor + d, seconds: d });
        cursor += d;
      }
      continue;
    }

    console.log(`\n=== 录制 ${seg.id} ===`);
    frames = [];
    rmSync(join(FRAMES, seg.id), { recursive: true, force: true });

    await cdp.send("Page.startScreencast", { format: "jpeg", quality: 92, everyNthFrame: 1 });
    capturing = true;
    const t0 = Date.now();
    for (const act of seg.actions) await runAction(page, act, seg.id);
    capturing = false;
    await cdp.send("Page.stopScreencast");
    const wall = (Date.now() - t0) / 1000;

    /* 补一帧"尾帧"，把段尾的静止时间补回来。
     *
     * screencast 只在画面**变化**时发帧，静止的等待期一帧都不发。而组装时长是
     * 由帧时间戳推出来的 —— 于是段尾那段"画面不动但时间在走"的部分会**整段消失**。
     * 实测：`inject` 段墙钟 26.6s，组装出来只有 5.6s（末尾 21 秒静看告警列表被吃掉）。
     * 这个缺陷很隐蔽：录制日志一切正常，段也生成成功，只是比实际短。
     *
     * 修法：用墙钟给最后一帧一个"到段落结束"的时间戳。 */
    if (frames.length) {
      frames.push({ i: frames.length, data: frames[frames.length - 1].data, ts: frames[0].ts + wall });
    }

    if (!frames.length) throw new Error(`片段 ${seg.id} 一帧都没录到`);
    framesToMp4(frames, join(FRAMES, seg.id), outFile, `组装 ${seg.id}`);
    const d = duration(outFile);
    console.log(`  ✓ ${seg.id.padEnd(12)} 墙钟 ${wall.toFixed(1)}s  成段 ${d.toFixed(1)}s  帧 ${frames.length}`);
    timeline.push({ id: seg.id, in: cursor, out: cursor + d, seconds: d });
    cursor += d;
  }

  await browser.close();

  // ---- 拼成一条原始录屏，并写出供 compose 使用的时间轴 ----
  const present = timeline.map((t) => join(RAW, `seg-${t.id}.mp4`));
  const listFile = join(BUILD, "record-concat.txt");
  writeFileSync(listFile, present.map((f) => `file '${f.replace(/\\/g, "/")}'`).join("\n"));
  const rawMp4 = join(RAW, "recording.mp4");
  ffmpeg([
    "-y", "-hide_banner", "-loglevel", "error",
    "-f", "concat", "-safe", "0", "-i", listFile,
    "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-r", "30",
    rawMp4,
  ], "拼接原始录屏");

  writeFileSync(join(BUILD, "segments.json"), JSON.stringify({ total: duration(rawMp4), timeline }, null, 2));

  console.log(`\n=== 原始录屏 ===\n  ${rawMp4}  总时长 ${duration(rawMp4).toFixed(1)}s`);
  console.log("  各段在原始时间轴上的位置（填进 timeline.json 的 source.in/out）：");
  for (const t of timeline) {
    console.log(`    ${t.id.padEnd(12)} ${t.in.toFixed(1)} → ${t.out.toFixed(1)}  (${t.seconds.toFixed(1)}s)`);
  }
}

main().catch((e) => {
  console.error("\n✗ " + e.message);
  process.exit(1);
});
