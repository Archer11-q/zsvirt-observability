/**
 * 合成器：把原始录屏 + 包装层剪接成成片。
 *
 * ## 分工（这条边界是刻意的）
 *
 * - `render.mjs` 负责**一切与时间有关的美术**：逐帧 PNG。什么时候淡入、白闪多久，
 *   全部在浏览器里按"时间的纯函数"算好。
 * - `compose.mjs` 只做 ffmpeg 擅长的四件事：**裁剪、叠加、拼接、限长**。
 *
 * 为什么不把动画交给 ffmpeg 的 filter 表达式：filter 里的 `enable=between(t,...)`
 * 写错了，成片照样渲得出来 —— 只是那一层永远不出现或从头到尾都在。这种**静默错**
 * 在交付前极难发现。把时间交给能逐帧直视的 PNG，把 ffmpeg 限制在它不会出错的地方。
 *
 * ## 每一段的结构（顺序有语义，不要调换）
 *
 * ```
 *   [分段卡]  +  [录屏段 ⊕ 该段字幕/统计/白闪]   →  concat  →  ⊕ 常驻来源角标
 *     不透明            叠在录屏上                  重编码       叠在整段上
 * ```
 *
 * - 段内字幕/统计是**相对录屏时间轴**的，所以必须叠在 concat 之前。
 * - 来源角标要盖住**整段（含分段卡）**，所以必须叠在 concat 之后 ——
 *   官方答复第 13 条要求"视频中标明来源"，角标在分段卡上缺席就是漏标。
 *
 * ## 用法
 *
 *   node compose.mjs                 # 全程
 *   node compose.mjs --clip inject   # 只重做某一段（改字幕时省时间）
 *   node compose.mjs --review        # 额外抽帧做回看表
 */

import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
const BUILD = join(HERE, "build");
const CLIPS = join(BUILD, "clips");
const OVERLAYS = join(BUILD, "overlays");
const OUT = join(HERE, "out");
const RAW_DIR = join(HERE, "raw");

const FFMPEG = require("@ffmpeg-installer/ffmpeg").path;
const FFPROBE = require("@ffprobe-installer/ffprobe").path;

const ENCODE = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"];

function ffmpeg(args, label) {
  /* 失败时打印 ffmpeg 的**最后 30 行**：它的报错总是在末尾，
     截头部只会看到一堆无关的输入描述。 */
  const r = spawnSync(FFMPEG, args, { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
  if (r.status !== 0) {
    const tail = String(r.stderr || "").trim().split("\n").slice(-30).join("\n");
    throw new Error(`ffmpeg 失败（${label}）\n${tail}`);
  }
}

function probeDuration(file) {
  const r = spawnSync(FFPROBE, [
    "-v", "error", "-show_entries", "format=duration",
    "-of", "default=nw=1:nk=1", file,
  ], { encoding: "utf8" });
  return Number(String(r.stdout || "").trim());
}

function timeline() {
  const p = join(HERE, "timeline.json");
  if (!existsSync(p)) throw new Error("缺少 timeline.json —— 从 timeline.example.json 复制一份。");
  // 去 BOM：这个文件要在 Windows 上手工编辑，而 PowerShell / 记事本的
  // "UTF-8" 会写 BOM，JSON.parse 遇到它抛的错看起来像花括号写错了。
  const tl = JSON.parse(readFileSync(p, "utf8").replace(/^\uFEFF/, ""));

  /* 录屏段的 in/out **自动取自录制产物**，不靠人手抄。
   *
   * `record/record.mjs` 录完会把每段在原始时间轴上的位置写进
   * `build/segments.json`。这里若发现它就用它覆盖 timeline.json 的 `source` ——
   * 手工抄秒数是这个项目反复出问题的那类步骤：抄错一位，成片里就会
   * "命令还没敲完就切走"，而所有日志都是正常的。
   *
   * 想手工指定（例如多次录制混用素材），把 `build/segments.json` 删掉即可。 */
  const auto = join(BUILD, "segments.json");
  if (existsSync(auto)) {
    const recorded = JSON.parse(readFileSync(auto, "utf8").replace(/^\uFEFF/, ""));
    const byId = new Map(recorded.timeline.map((t) => [t.id, t]));
    let applied = 0;
    for (const clip of tl.clips ?? []) {
      const seg = byId.get(clip.id);
      if (!seg) continue;
      clip.source = { in: seg.in, out: seg.out };
      applied++;
    }
    if (applied) {
      tl.recording = join(RAW_DIR, "recording.mp4");
      console.log(`时间轴：自动取用 build/segments.json 的 ${applied} 段切点（录屏总长 ${recorded.total.toFixed(1)}s）`);
    }
  }

  return tl;
}

function seqDir(id) {
  return join(OVERLAYS, id);
}

function mustExist(p, why) {
  if (!existsSync(p)) throw new Error(`缺少 ${p}\n  ${why}`);
  return p;
}

/** 把若干 PNG 序列叠到 base 上。items 按数组顺序自下而上。 */
function overlayArgs(baseFile, items, fps, outFile, label) {
  const args = ["-y", "-hide_banner", "-loglevel", "error", "-i", baseFile];

  for (const it of items) {
    if (it.from > 0) args.push("-itsoffset", String(it.from));
    args.push("-framerate", String(fps), "-i", join(it.dir, "%05d.png"));
  }

  let filter = "";
  let current = "0:v";
  items.forEach((_, i) => {
    const tag = `b${i + 1}`;
    // eof_action=pass：这一层放完就交还给下面那层，不要继续顶在上面。
    // repeatlast=0：同理，不要在结束后把最后一帧一直重复到片尾。
    filter += `[${current}][${i + 1}:v]overlay=0:0:eof_action=pass:repeatlast=0[${tag}];`;
    current = tag;
  });

  args.push(
    "-filter_complex", filter.replace(/;$/, ""),
    "-map", `[${current}]`,
    ...ENCODE,
    "-an",
    outFile
  );
  ffmpeg(args, label);
}

function concatParts(files, outFile, label) {
  if (files.length === 1) {
    // 只有一段时不必走 concat 复用器 —— 少一次重编码，少一处可能出错的地方。
    ffmpeg(["-y", "-hide_banner", "-loglevel", "error", "-i", files[0], ...ENCODE, "-an", outFile], label);
    return;
  }
  const list = join(CLIPS, `${label.replace(/[^\w-]/g, "_")}-concat.txt`);
  writeFileSync(list, files.map((f) => `file '${f.replace(/\\/g, "/")}'`).join("\n"));
  // 刻意**不**用 `-c copy`：拼接处两段的编码参数只要有一丝不同，copy 就会产出
  // 花屏或时长错乱，而且不报错。重编码慢一点，但它不会悄悄坏。
  ffmpeg([
    "-y", "-hide_banner", "-loglevel", "error",
    "-f", "concat", "-safe", "0", "-i", list,
    ...ENCODE, "-an",
    outFile,
  ], label);
}

function buildClip(clip, tl) {
  const fps = tl.fps ?? 30;
  const W = tl.width ?? 1920;
  const H = tl.height ?? 1080;

  mkdirSync(CLIPS, { recursive: true });

  const cardSeconds = clip.card ? (clip.card.seconds ?? 2.6) : 0;
  const bodySeconds = clip.source ? Number(clip.source.out) - Number(clip.source.in) : 0;
  const clipSeconds = cardSeconds + bodySeconds;
  if (clipSeconds <= 0) throw new Error(`片段 ${clip.id} 既没有卡片也没有录屏段`);

  const parts = [];

  // ---- 1. 分段卡 ----
  if (clip.card) {
    const cardSeq = mustExist(seqDir(`card-${clip.id}`), `先跑 node render.mjs`);
    const cardMp4 = join(CLIPS, `${clip.id}-card.mp4`);
    ffmpeg([
      "-y", "-hide_banner", "-loglevel", "error",
      "-framerate", String(fps), "-i", join(cardSeq, "%05d.png"),
      "-r", String(fps), ...ENCODE,
      cardMp4,
    ], `分段卡 ${clip.id}`);
    parts.push(cardMp4);
  }

  // ---- 2. 录屏段 + 段内叠加层 ----
  if (clip.source) {
    const bodyMp4 = join(CLIPS, `${clip.id}-body.mp4`);
    ffmpeg([
      "-y", "-hide_banner", "-loglevel", "error",
      "-ss", String(clip.source.in), "-to", String(clip.source.out),
      "-i", mustExist(tl.recording, "原始录屏不存在 —— 改 timeline.json 的 recording 字段。"),
      "-vf", `scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2:color=0x0F1720,fps=${fps}`,
      "-an", ...ENCODE,
      bodyMp4,
    ], `裁剪 ${clip.id}`);

    const layers = (clip.overlays ?? []).map((ov) => {
      const dir = mustExist(seqDir(`ov-${ov.id}`), "先跑 node render.mjs");
      return { dir, from: ov.from ?? 0 };
    });

    if (layers.length) {
      const overlaid = join(CLIPS, `${clip.id}-overlaid.mp4`);
      overlayArgs(bodyMp4, layers, fps, overlaid, `段内叠加 ${clip.id}`);
      parts.push(overlaid);
    } else {
      parts.push(bodyMp4);
    }
  }

  // ---- 3. 拼成整段（此时还没有角标）----
  const base = join(CLIPS, `${clip.id}-base.mp4`);
  concatParts(parts, base, `${clip.id}-base`);

  // ---- 4. 常驻来源角标：盖住整段，含分段卡 ----
  const badgePng = mustExist(join(seqDir("badge"), "00000.png"), "先跑 node render.mjs");
  const finalOut = join(CLIPS, `${clip.id}.mp4`);
  ffmpeg([
    "-y", "-hide_banner", "-loglevel", "error",
    "-i", base,
    // `-loop 1 -t <时长>` 把单张 PNG 变成**有限长**的视频流。
    // 这里必须给 -t：只写 `-loop 1` 会得到一个无限长的输入，输出永远渲不完 ——
    // 而且 ffmpeg 不会报错，只是一直跑（实测踩过）。
    "-loop", "1", "-framerate", String(fps), "-t", clipSeconds.toFixed(3), "-i", badgePng,
    "-filter_complex", "[0:v][1:v]overlay=0:0[out]",
    "-map", "[out]",
    "-t", clipSeconds.toFixed(3),
    ...ENCODE, "-an",
    finalOut,
  ], `角标 ${clip.id}`);

  const got = probeDuration(finalOut);
  console.log(`  ✓ ${clip.id.padEnd(12)} ${clipSeconds.toFixed(1)}s（实测 ${got.toFixed(1)}s）`);
  return finalOut;
}

function main() {
  const argv = process.argv.slice(2);
  const onlyIdx = argv.indexOf("--clip");
  const only = onlyIdx !== -1 ? argv[onlyIdx + 1] : null;
  const review = argv.includes("--review");

  const tl = timeline();
  mkdirSync(CLIPS, { recursive: true });
  mkdirSync(OUT, { recursive: true });
  mustExist(FFMPEG, "ffmpeg 二进制缺失，先 npm install");

  console.log("=== 逐段合成 ===");
  const produced = [];
  for (const clip of tl.clips) {
    if (only && clip.id !== only) {
      const p = join(CLIPS, `${clip.id}.mp4`);
      if (existsSync(p)) produced.push(p);
      continue;
    }
    produced.push(buildClip(clip, tl));
  }

  if (only) {
    console.log(`\n只重做了 ${only}。整片请不带 --clip 再跑一次。`);
    return;
  }

  const finalOut = join(OUT, "crosslayer-demo.mp4");
  concatParts(produced, finalOut, "final");
  const dur = probeDuration(finalOut);

  const mm = Math.floor(dur / 60);
  const ss = (dur % 60).toFixed(1);
  const ok = dur >= 180 && dur <= 300;
  console.log(`\n=== 成片 ===\n  ${finalOut}`);
  console.log(
    `  时长 ${mm}:${String(ss).padStart(4, "0")}　` +
      (ok ? "✓ 落在官方 3–5 分钟区间内" : "✗ 超出官方 3–5 分钟区间，需要增删内容")
  );

  if (review) {
    const dir = join(OUT, "review");
    mkdirSync(dir, { recursive: true });
    ffmpeg([
      "-y", "-hide_banner", "-loglevel", "error",
      "-i", finalOut, "-vf", "fps=1/10", join(dir, "%03d.png"),
    ], "抽帧回看");
    console.log("  抽帧回看表 → out/review/（每 10 秒一张）");
  }
}

try {
  main();
} catch (e) {
  console.error("\n✗ " + e.message);
  process.exit(1);
}
