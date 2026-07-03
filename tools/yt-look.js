#!/usr/bin/env node
/* yt-look — ask a video-capable LLM (video-qa preset, grok) detail questions about
 * a YouTube video that the transcript can't answer. Downloads on demand (cached),
 * cuts a clip or extracts frames, uploads via the xAI Files API, asks via
 * /v1/responses, archives the verdict next to the yt-transcriptor transcript.
 *
 * Escalation ladder (cheapest first):
 *   frames  --frames 9:41,10:02   full-res stills, best for reading chart numbers
 *   clip    --range 9:30-11:30    ~1 fps sampling + AUDIO, best for "what happens here"
 *   full    --full                whole video, sparse ~36-frame sampling, overview only
 *
 * Usage:
 *   node tools/yt-look.js <url|id> --range 9:30-11:30 --question "..."
 *   node tools/yt-look.js <url|id> --frames 10:02,10:15 --question "..."
 *   node tools/yt-look.js <url|id> --full --question "..."
 * Options: --pad <s> (clip padding, default 20) --json --keep-remote --no-archive
 *          --cache-dir <dir> --force (override clip-length guardrail)
 *
 * Grok's video ingestion is an undocumented consumer-surface behavior (docs say
 * image-only) — if it breaks, this tool reports the upstream error; fall back to
 * --frames, which uses the documented image path. */
const fs = require('fs');
const path = require('path');
const { execFileSync, spawnSync } = require('child_process');
// LLM access goes through the stockstuff OAuth-preset backend — the ONE canonical
// token store (a second refresher on the same refresh_token could invalidate it)
const STOCKSTUFF = process.env.STOCKSTUFF_DIR || '/home/ma-agent1/projects/stockstuff';
const backend = require(path.join(STOCKSTUFF, 'server/agents/backend'));

const PROJECT_ROOT = path.join(__dirname, '..');
const CACHE_ROOT = process.env.YT_LOOK_CACHE || path.join(PROJECT_ROOT, 'videos');
const TRANSCRIPTS_ROOT = path.join(PROJECT_ROOT, 'transcripts');
const YT_DLP = process.env.YT_DLP_BIN || 'yt-dlp';
const YTFETCH = process.env.YTFETCH_BIN || 'ytfetch';
const FFMPEG = process.env.FFMPEG_BIN || 'ffmpeg';
const FFPROBE = process.env.FFPROBE_BIN || 'ffprobe';
const ROLE = 'video-qa';
const MAX_CLIP_SECONDS = 360;       // cost guardrail (~$0.05); override with --force
const MAX_UPLOAD_BYTES = 140 * 1024 * 1024; // observed consumer limit ~150MB, keep margin
const MAX_FRAMES = 8;
const CACHE_CAP_BYTES = 20 * 1024 * 1024 * 1024;

const FORMATS = 'bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080][vcodec^=vp9]+ba/bv*[height<=1080]+ba/b[height<=1080]/b';

function usage(code) {
  console.error('usage: yt-look.js <url|id> (--range M:SS-M:SS | --frames t1,t2 | --full) --question "..." [--pad s] [--json] [--keep-remote] [--no-archive] [--force]');
  process.exit(code);
}

function videoId(s) {
  const m = String(s).match(/(?:v=|youtu\.be\/|shorts\/|embed\/|live\/)([\w-]{11})/) || String(s).match(/^([\w-]{11})$/);
  if (!m) throw new Error(`cannot extract a video id from: ${s}`);
  return m[1];
}

function parseTime(t) {
  const p = String(t).trim().split(':').map(Number);
  if (p.some(Number.isNaN)) throw new Error(`bad timestamp: ${t}`);
  if (p.length === 3) return p[0] * 3600 + p[1] * 60 + p[2];
  if (p.length === 2) return p[0] * 60 + p[1];
  return p[0];
}
const fmtTime = (s) => {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? `${h}:${String(m).padStart(2, '0')}` : `${m}`) + `:${String(sec).padStart(2, '0')}`;
};

function probeDuration(file) {
  try {
    const out = execFileSync(FFPROBE, ['-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', file], { encoding: 'utf8' });
    return Math.round(parseFloat(out) || 0);
  } catch { return 0; }
}

// ytfetch is the default fetch path; set USE_YTFETCH=0 (or false/off/no) to force
// the legacy direct-yt-dlp ladder. Any ytfetch failure degrades to it regardless.
function ytfetchEnabled() {
  const v = (process.env.USE_YTFETCH ?? '1').trim().toLowerCase();
  return !['0', 'false', 'off', 'no'].includes(v);
}

// Shared byte-fetch via the ytfetch CLI (yt-video-engine). Returns the cached
// media path, or null to signal the caller to fall back to the direct yt-dlp
// ladder. The video+audio enum carries our avc1->vp9->any codec ladder inside
// ytfetch, so we never get AV1 (itag399 500s) back here.
function ytfetchVideo(id) {
  let r;
  try {
    r = spawnSync(YTFETCH, ['fetch', id, '--media', 'video+audio', '--max-height', '1080'],
      { encoding: 'utf8', timeout: 15 * 60 * 1000 });
  } catch { return null; }
  if (r.status !== 0) return null; // auth/ratelimit/format/unknown -> fall back
  try {
    const out = JSON.parse(r.stdout);
    if (out.path && fs.existsSync(out.path) && fs.statSync(out.path).size > 0) return out.path;
  } catch { /* bad JSON -> fall back */ }
  return null;
}

function download(id, dir) {
  // frames/clips are written into `dir` by the caller, so it must exist even when
  // the media itself comes from ytfetch's cache (early return below) rather than
  // the yt-dlp branch that used to be the only thing creating it.
  fs.mkdirSync(dir, { recursive: true });
  const dest = path.join(dir, 'source.mp4');
  if (fs.existsSync(dest) && fs.statSync(dest).size > 0) return dest;
  if (ytfetchEnabled()) {
    const p = ytfetchVideo(id);
    if (p) return p; // ytfetch owns the cache + eviction; ffmpeg reads this path directly
    // fall through to the direct yt-dlp ladder on any ytfetch failure
  }
  const url = `https://www.youtube.com/watch?v=${id}`;
  // avc1/vp9 first (AV1 itag 399 has been serving HTTP 500s); then client fallbacks
  const attempts = [
    [],
    ['--extractor-args', 'youtube:player_client=default,android'],
    ['--extractor-args', 'youtube:player_client=tv,android'], // may cap at 360p — better than nothing
  ];
  let lastErr = '';
  for (const extra of attempts) {
    const r = spawnSync(YT_DLP, [
      '-f', FORMATS, '--merge-output-format', 'mp4', '--no-playlist',
      '-o', dest, ...extra, url,
    ], { encoding: 'utf8', timeout: 15 * 60 * 1000 });
    if (r.status === 0 && fs.existsSync(dest) && fs.statSync(dest).size > 0) return dest;
    lastErr = ((r.stderr || '') + (r.stdout || '')).slice(-500);
    for (const f of fs.readdirSync(dir)) if (f.startsWith('source.')) { try { fs.unlinkSync(path.join(dir, f)); } catch { /* partial */ } }
  }
  throw new Error(`yt-dlp failed for ${id} (try 'yt-dlp -U'; members-only/age-gated videos need cookies): ${lastErr}`);
}

function cutClip(src, dir, start, end) {
  const dest = path.join(dir, `clip_${Math.round(start)}-${Math.round(end)}.mp4`);
  if (fs.existsSync(dest) && fs.statSync(dest).size > 0) return dest;
  // write to a temp name, rename on success — a killed ffmpeg must not leave a
  // truncated clip behind that the size>0 cache check would reuse forever
  const tmp = path.join(dir, `.clip_tmp_${process.pid}.mp4`);
  try {
    // -c copy snaps to the previous keyframe (<=~5s early) — padding already covers that
    execFileSync(FFMPEG, ['-y', '-ss', String(start), '-to', String(end), '-i', src,
      '-c', 'copy', '-avoid_negative_ts', 'make_zero', tmp], { stdio: 'pipe', timeout: 120000 });
    if (!fs.existsSync(tmp) || fs.statSync(tmp).size === 0) throw new Error('ffmpeg produced an empty clip');
    fs.renameSync(tmp, dest);
  } finally {
    try { fs.unlinkSync(tmp); } catch { /* already renamed */ }
  }
  return dest;
}

function extractFrame(src, dir, t) {
  const dest = path.join(dir, `frame_${String(t).replace(/[^\w.]/g, '_')}.png`);
  // -update 1: ffmpeg 7.x needs it to write a single image to a fixed filename;
  // without it the muxer intermittently aborts with an I/O error (-5).
  execFileSync(FFMPEG, ['-y', '-ss', String(t), '-i', src, '-frames:v', '1', '-update', '1', dest], { stdio: 'pipe', timeout: 60000 });
  if (!fs.existsSync(dest) || fs.statSync(dest).size === 0) throw new Error(`no frame at ${t} (past end of video?)`);
  return dest;
}

function pruneCache() {
  let entries = [];
  try {
    for (const d of fs.readdirSync(CACHE_ROOT)) {
      const dir = path.join(CACHE_ROOT, d);
      if (!fs.statSync(dir).isDirectory()) continue;
      let size = 0, mtime = 0;
      for (const f of fs.readdirSync(dir)) {
        const st = fs.statSync(path.join(dir, f));
        size += st.size; mtime = Math.max(mtime, st.mtimeMs);
      }
      entries.push({ dir, size, mtime });
    }
  } catch { return; }
  let total = entries.reduce((a, e) => a + e.size, 0);
  entries.sort((a, b) => a.mtime - b.mtime);
  for (const e of entries) {
    if (total <= CACHE_CAP_BYTES) break;
    try { fs.rmSync(e.dir, { recursive: true, force: true }); total -= e.size; } catch { /* keep going */ }
  }
}

function findTranscriptDir(id) {
  const matches = [];
  const stack = [TRANSCRIPTS_ROOT];
  while (stack.length) {
    const dir = stack.pop();
    let items;
    try { items = fs.readdirSync(dir, { withFileTypes: true }); } catch { continue; }
    for (const it of items) {
      if (!it.isDirectory()) continue;
      const p = path.join(dir, it.name);
      if (it.name.endsWith(`_${id}`)) matches.push({ name: it.name, dir: p });
      else stack.push(p);
    }
  }
  // re-transcribed videos get a new dated folder (YYYY-MM-DD_ prefix sorts) — use the newest
  matches.sort((a, b) => a.name.localeCompare(b.name));
  return matches.length ? matches[matches.length - 1].dir : null;
}

function archive(id, mode, detail, question, answer, costUsd) {
  const dir = findTranscriptDir(id) || path.join(CACHE_ROOT, id);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, 'video-qa.md');
  const head = fs.existsSync(file) ? '' : `# Video Q&A — ${id}\n\nAnswers from the video-qa preset (visual/audio look at the actual video).\n`;
  const entry = `\n---\n\n**${new Date().toISOString().slice(0, 16).replace('T', ' ')}Z** · ${mode} ${detail} · ~$${costUsd.toFixed(3)}\n\n**Q:** ${question}\n\n**A:**\n${answer}\n`;
  fs.appendFileSync(file, head + entry);
  return file;
}

function parseArgs(argv) {
  const a = { url: null, range: null, frames: null, full: false, question: null, pad: 20, json: false, keepRemote: false, noArchive: false, force: false, cacheDir: null };
  for (let i = 2; i < argv.length; i++) {
    const x = argv[i];
    if (x === '--range') a.range = argv[++i];
    else if (x === '--frames') a.frames = argv[++i];
    else if (x === '--full') a.full = true;
    else if (x === '--question' || x === '-q') a.question = argv[++i];
    else if (x === '--pad') a.pad = Number(argv[++i]);
    else if (x === '--json') a.json = true;
    else if (x === '--keep-remote') a.keepRemote = true;
    else if (x === '--no-archive') a.noArchive = true;
    else if (x === '--force') a.force = true;
    else if (x === '--cache-dir') a.cacheDir = argv[++i];
    else if (x === '--help' || x === '-h') usage(0);
    else if (!a.url && !x.startsWith('--')) a.url = x;
    else { console.error(`unknown arg: ${x}`); usage(1); }
  }
  return a;
}

const SYSTEM = `You are a precise video analyst. Answer ONLY from what you can actually see and hear in the provided video or frames.
- Cite evidence with a timestamp (m:ss) for every claim.
- If something is not visible or audible, say so explicitly — never guess or fill in from general knowledge.
- If asked about audio and you received no audio, reply "NO AUDIO".
- Answer in the language of the question.`;

(async () => {
  const args = parseArgs(process.argv);
  if (!args.url || !args.question) usage(1);
  const modes = [args.range, args.frames, args.full].filter(Boolean).length;
  if (modes !== 1) { console.error('pick exactly one of --range / --frames / --full'); usage(1); }

  const id = videoId(args.url);
  const cacheDir = path.join(args.cacheDir || CACHE_ROOT, id);

  // validate media selection BEFORE any download/API work — an empty frame list
  // would silently send a text-only request and archive a blind "video" verdict
  let frameTimes = [];
  if (args.frames) {
    const requested = args.frames.split(',').map((t) => t.trim()).filter(Boolean);
    if (!requested.length) { console.error(`--frames needs at least one timestamp, got: "${args.frames}"`); usage(1); }
    requested.forEach(parseTime); // throws on malformed timestamps
    if (requested.length > MAX_FRAMES) {
      console.error(`[yt-look] WARNING: ${requested.length} frames requested, sending only the first ${MAX_FRAMES}`);
    }
    frameTimes = requested.slice(0, MAX_FRAMES);
  }

  const preset = backend.rolePreset(ROLE);

  console.error(`[yt-look] ${id} via ${preset.provider}/${preset.model} (${args.range ? 'clip ' + args.range : args.frames ? 'frames ' + args.frames : 'full video'})`);
  const src = download(id, cacheDir);
  const duration = probeDuration(src);
  const srcBytes = fs.statSync(src).size;
  console.error(`[yt-look] source: ${(srcBytes / 1e6).toFixed(1)}MB, ${fmtTime(duration)}`);

  let parts = [];
  let uploaded = null;
  let mode, detail, userPrefix = '';

  if (args.frames) {
    mode = 'frames';
    const times = frameTimes;
    detail = times.join(',');
    const labels = [];
    for (const t of times) {
      const secs = parseTime(t);
      if (duration && secs > duration) throw new Error(`frame ${t} is past the end of the video (${fmtTime(duration)})`);
      const f = extractFrame(src, cacheDir, secs);
      parts.push({ type: 'input_image', image_url: `data:image/png;base64,${fs.readFileSync(f).toString('base64')}` });
      labels.push(`frame ${labels.length + 1} = t=${fmtTime(secs)}`);
    }
    userPrefix = `Full-resolution frames from YouTube video ${id} (${labels.join(', ')}).\n\n`;
  } else {
    let fileToSend = src;
    if (args.range) {
      mode = 'clip';
      const [s, e] = args.range.split('-').map(parseTime);
      if (!(e > s)) throw new Error(`bad --range (need START-END): ${args.range}`);
      const start = Math.max(0, s - args.pad);
      const end = duration ? Math.min(duration, e + args.pad) : e + args.pad;
      if (end - start > MAX_CLIP_SECONDS && !args.force) {
        throw new Error(`clip is ${Math.round(end - start)}s (> ${MAX_CLIP_SECONDS}s guardrail). Narrow --range or pass --force.`);
      }
      detail = `${fmtTime(start)}-${fmtTime(end)}`;
      fileToSend = cutClip(src, cacheDir, start, end);
      userPrefix = `This clip is the segment ${fmtTime(start)}–${fmtTime(end)} of YouTube video ${id} (full length ${fmtTime(duration)}). Cite timestamps relative to the ORIGINAL video (add ${fmtTime(start)} to clip-relative times).\n\n`;
    } else {
      mode = 'full';
      detail = fmtTime(duration);
      userPrefix = `Full YouTube video ${id} (${fmtTime(duration)}). Note: long videos are sparsely frame-sampled — for fine visual detail, re-ask with a narrow clip.\n\n`;
    }
    const bytes = fs.statSync(fileToSend).size;
    if (bytes > MAX_UPLOAD_BYTES) throw new Error(`${(bytes / 1e6).toFixed(0)}MB exceeds the ~140MB upload limit — use --range to send a clip`);
    console.error(`[yt-look] uploading ${(bytes / 1e6).toFixed(1)}MB...`);
    uploaded = await backend.uploadFile({ provider: preset.provider, file: preset.file, filePath: fileToSend });
    parts.push({ type: 'input_file', file_id: uploaded.id });
  }

  try {
    console.error('[yt-look] asking...');
    const answer = await backend.runRole(ROLE, { system: SYSTEM, user: userPrefix + args.question, parts });
    if (!answer) throw new Error('empty model response');
    // usage isn't exposed by the streaming backend; estimate from observed ~200 tok/s + $1.25/M
    const estSeconds = mode === 'frames' ? parts.length * 8 : mode === 'clip' ? parseTime(detail.split('-')[1]) - parseTime(detail.split('-')[0]) : duration;
    const costUsd = (estSeconds * 200 / 1e6) * 1.25;
    const archived = args.noArchive ? null : archive(id, mode, detail, args.question, answer, costUsd);
    if (args.json) {
      console.log(JSON.stringify({ ok: true, video_id: id, mode, detail, answer, archived, est_cost_usd: Number(costUsd.toFixed(4)) }, null, 2));
    } else {
      console.log(answer);
      if (archived) console.error(`\n[yt-look] archived -> ${archived} (est. ~$${costUsd.toFixed(3)})`);
    }
  } finally {
    if (uploaded && !args.keepRemote) await backend.deleteFile({ provider: preset.provider, file: preset.file, fileId: uploaded.id });
    pruneCache();
  }
})().catch((e) => {
  console.error(`[yt-look] FAILED: ${e.message}`);
  process.exit(1);
});
