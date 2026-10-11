/**
 * 只重组、不重录。
 *
 * ## 为什么需要这个
 *
 * 录制要 4–6 分钟墙钟，而"帧 → 视频"的合并逻辑出过一次缺陷（长 duration 被
 * ffmpeg 丢掉，段尾静止时间整段消失）。修合并逻辑时不该逼着人重录一遍 ——
 * 帧还在 `build/record-frames/<段>/` 里，而且旧的 `concat.txt` 里**保留了
 * 每一帧的原始 duration**，也就是保留了真实时间戳。
 *
 * 所以这个脚本从旧 concat.txt 读出时间轴，用修好的逻辑重新编码并重新拼片。
 * 它是**合并逻辑的回归测试工具**：改完 `record.mjs` 的合并部分，先跑它验证，
 * 确认对了再决定要不要重录。
 *
 * 用法：
 *   node reassemble.mjs
 */

import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
const VIDEO_DIR = join(HERE, "..");
const RAW = join(VIDEO_DIR, "raw");
const FRAMES = join(VIDEO_DIR, "build", "record-frames");

const FFMPEG = require("@ffmpeg-installer/ffmpeg").path;
const FFPROBE = require("@ffprobe-installer/ffprobe").path;
const FPS = 30;

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

/** 从旧 concat.txt 读回 (文件, duration) 序列 —— 它就是真实时间轴。 */
function parseOldList(file) {
  const lines = readFileSync(file, "utf8").split("\n");
  const out = [];
  let pending = null;
  for (const line of lines) {
    const f = line.match(/^file '(.+)'$/);
    const d = line.match(/^duration ([\d.]+)$/);
    if (f) {
      if (pending) out.push(pending);
      pending = { path: f[1], dur: null };
    } else if (d && pending) {
      pending.dur = Number(d[1]);
    }
  }
  if (pending) out.push(pending);
  // 最后一条没有 duration（concat 的惯用写法），沿用前一条
  for (let i = 0; i < out.length; i++) {
    if (out[i].dur === null) out[i].dur = i > 0 ? out[i - 1].dur : 1 / FPS;
  }
  return out;
}

function main() {
  /* **顺序必须来自录制脚本，不能靠目录名排序。**
     按目录名排会得到字母序（crosslayer, deploy, diagnosis, inject, observe, real），
     而成片要的是叙事序（deploy → observe → inject → real → crosslayer → diagnosis）。
     实测踩过：重组完整片顺序全乱，而每一步都不报错。 */
  const scriptOrder = JSON.parse(readFileSync(join(HERE, "segments.json"), "utf8").replace(/^\uFEFF/, ""))
    .segments.map((s) => s.id);
  const onDisk = readdirSync(FRAMES, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name);
  const segs = [...scriptOrder.filter((id) => onDisk.includes(id)), ...onDisk.filter((id) => !scriptOrder.includes(id))];

  const timeline = [];
  let cursor = 0;

  for (const seg of segs) {
    const oldList = join(FRAMES, seg, "concat.txt");
    if (!existsSync(oldList)) { console.log(`  跳过 ${seg}（没有 concat.txt）`); continue; }

    const frames = parseOldList(oldList);
    let list = "";
    let emitted = 0;
    let cum = 0;
    for (const f of frames) {
      // 累计时间映射：逐帧 Math.round 会累积误差 ——
      // 实测 observe 段 34.5s 被放大成 50.6s（1054 帧，每帧多算半帧）。
      cum += f.dur;
      const target = Math.round(cum * FPS);
      const hold = Math.max(0, target - emitted); // 允许丢帧：60fps 采集降采样到 30fps 时，强制每帧至少 1 会重复计数
      emitted += hold;
      for (let k = 0; k < hold; k++) list += `file '${f.path}'\n`;
    }
    writeFileSync(join(FRAMES, seg, "concat-cfr.txt"), list);

    const outFile = join(RAW, `seg-${seg}.mp4`);
    ffmpeg([
      "-y", "-hide_banner", "-loglevel", "error",
      "-f", "concat", "-safe", "0", "-r", String(FPS), "-i", join(FRAMES, seg, "concat-cfr.txt"),
      "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-r", String(FPS),
      outFile,
    ], `重组 ${seg}`);

    const d = duration(outFile);
    console.log(`  ✓ ${seg.padEnd(12)} 帧 ${String(frames.length).padStart(4)}  旧列表合计 ${frames.reduce((s, f) => s + f.dur, 0).toFixed(1)}s  成段 ${d.toFixed(1)}s`);
    timeline.push({ id: seg, in: cursor, out: cursor + d, seconds: d });
    cursor += d;
  }

  const listFile = join(VIDEO_DIR, "build", "record-concat.txt");
  writeFileSync(listFile, timeline.map((t) => `file '${join(RAW, `seg-${t.id}.mp4`).replace(/\\/g, "/")}'`).join("\n"));
  const rawMp4 = join(RAW, "recording.mp4");
  ffmpeg([
    "-y", "-hide_banner", "-loglevel", "error",
    "-f", "concat", "-safe", "0", "-i", listFile,
    "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-r", String(FPS),
    rawMp4,
  ], "拼接原始录屏");

  writeFileSync(join(VIDEO_DIR, "build", "segments.json"), JSON.stringify({ total: duration(rawMp4), timeline }, null, 2));

  console.log(`\n=== 重组完成 ===\n  ${rawMp4}  总时长 ${duration(rawMp4).toFixed(1)}s`);
  for (const t of timeline) console.log(`    ${t.id.padEnd(12)} ${t.in.toFixed(1)} → ${t.out.toFixed(1)}  (${t.seconds.toFixed(1)}s)`);
}

try { main(); } catch (e) { console.error("\n✗ " + e.message); process.exit(1); }
