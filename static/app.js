// 试衣间前端：选两张图 → 一次请求 → 估算进度 + 真实秒数 → 结果与图库。

// 进度条的估算基数：来源 × 档位。api-2k 那个是按像素比推的、没实测，其余都有实测点（见 README 耗时表）。
// 本地这四格是 2026-11-03 同机量出来的（只编正向的编码节点 + `COMFYUI_ENABLE_MIOPEN=1`）：
// 480p 11.2 秒、720p 29.1 秒、1K 73.2 秒、2K 302.5 秒（四档都齐了，2K 是当天补测的第三档）。
const ESTIMATE_MS = {
  "api-1k": 90000,
  "api-2k": 170000,
  "comfyui-480p": 11000,
  "comfyui-720p": 29000,
  "comfyui-1k": 73000,
  "comfyui-2k": 302000,
  // 第 3 档（llama.cpp 出编码）：三格都是 2026-11-03 同实例并排量的稳态数（§25 的八腿表）。
  // 对照 §17 那三格干净的稳态数（11.2 / 29.1 / 73.2）：480p 基本打平（桥那 7 秒刚好抵掉省下的编码），
  // 720p 快约 4 秒，1K 快约 9 秒 —— 编码越贵的那一档越划算。
  "comfyui_llama-480p": 11800,
  "comfyui_llama-720p": 25400,
  "comfyui_llama-1k": 64500,
};
// 来源判定与后端 tryon/prefs.py 的 is_local() 同一件事：这两条都跑在本机、都不花额度。
const LOCAL_SOURCES = ["comfyui", "comfyui_llama"];
const isLocal = (s) => LOCAL_SOURCES.includes(s);
const BANDS = ["480p", "720p", "1k", "2k"]; // 和后端 prefs.VALID_SIZES 同一套顺序
const BAND_LABEL = { "480p": "480p", "720p": "720p", "1k": "1K", "2k": "2K" }; // 低档大写会变成 480P，那是型号写法不是这里要的
// 单件那档的实测秒数，按来源分开。第 3 档那三格是 §25 同实例八腿**第二遍**的稳态数（11.8 / 25.4 / 64.5）；
// 第一遍不能取，那是新进程价（480p 的 posonly 腿 18.4 vs 稳态 13.2，差的就是载权重那 7 秒）。
// 2K 两档都选不到（后端会收成 1K），所以 llama 那行没这一格，waitText() 拿不到就退回第 2 档的数。
const LOCAL_SECONDS = {
  comfyui: { "480p": 11, "720p": 29, "1k": 73, "2k": 302 },
  comfyui_llama: { "480p": 11.8, "720p": 25.4, "1k": 64.5 },
};
// 这四格是**第一发**的价：同一张图同一句指令只换 seed 再点一次，编码被 ComfyUI 按内容
// 缓存住，1K 只要 54 秒、2K 只要 208 秒。所以这些数是上界，确认框只会说多不会说少 —— 照上界来是对的，反过来会让人干等。
// 但**新形状的第一发比这里写的更贵**（MIOpen 找解）：2K 那一发实测 570.7 秒、720p 104 秒、480p 49 秒 ——
// 服务重启后头一次点某一档会超过这句提示，测量文档 §15/§17 有数。
// 2K 在本机这条根本选不到（`prefs.API_ONLY_SIZES` 会把它收成 1K），所以那一格实际是占位数。
// 多一张参考图要多花多少时间。2026-11-01 同机 720p 实测：单件 70.5/70.6 秒，套装 95.2/96.2 秒，换发型 67.1 秒。
// 只有 720p 这一档是量出来的，别的档位是照这个比例推的（进度条本来就是估算，确认框里会说清）。
// 那两个点出自旧配置，当时编码比现在贵得多；多一张图主要贵在多编一次，所以 1.36 用在新配置上偏大 —— 没重测，先照旧。
const MODE_FACTOR = { single: 1, hair: 1, outfit: 1.36 };
let genSource = "api"; // 由 api/quota 回填，进度基数、确认框、页眉都看它
let genSize = "1k"; // 同上，档位决定浏览器压到多大、以及一次花多少 credit

const PAGE_SIZE = 5; // 图库每页几条

// 分类：槽位字段和名字与 tryon/prompt.py 的 REF_SLOTS / SLOT_LABELS 是同一套（这个项目里前端一直这样镜像后端表）。
// 名字必须一字不差 —— 后端报"没有收到裤子/裙子照片图片"，页面上写的却是别的字，人就对不上号。
const MODES = { single: "一件衣服", outfit: "上衣加裤子/裙子", hair: "换个发型" };
const MODE_SLOTS = { single: ["garment"], outfit: ["garment", "garment2"], hair: ["garment"] };
const SLOT_TEXT = {
  single: [["衣服照片", "只放这一件，拍清款式和细节"]],
  outfit: [["上衣照片", "上衣单独一张，拍清领口和门襟"], ["裤子/裙子照片", "平铺或挂拍都行，从头到尾拍全"]],
  hair: [["发型参考", "要看清长度、层次和分缝的那张"]],
};
const ALL_SLOTS = ["garment", "garment2"];

const $ = (id) => document.getElementById(id);
let mode = "single"; // 由 localStorage 回填，只认 MODES 里有的那几个
const files = { person: null, garment: null, garment2: null };
let ticker = null;
let currentRecord = null;
let records = [];
let page = 1;
let quota = null; // 页眉那个数字，开始前的确认要用
let localBusy = false; // 自己这个请求还在飞
let serverBusy = null; // 后端的生成锁：{age_s, source}，没人跑时 null —— 按钮按住不按看它
let busyTimer = null; // 服务端说有任务在跑时，隔几秒再问一次

const PREVIEW_SIDE = 720; // 取景框就这么大，没必要画 3072×4096 的原图
// 和后端 prepare_upload 的长边上限一致（表在 tryon/image_prep.py 的 SIDE_BY_SIZE）。
// 2K 档要 2048：本地那条的输出跟着参考图走，传 1536 就出不了 2K。
// 解码峰值从约 7MB 涨到约 22MB（2048 长边），仍然有界 —— 全尺寸解码才是手机上卡死的那个。
// 两档低档和 1K 用同一个输入上限：实测输入 1.77M 和 0.31M 都是 26 秒，缩输入一分时间都不省，只丢条件。
const UPLOAD_SIDE = { "480p": 1536, "720p": 1536, "1k": 1536, "2k": 2048 };
const UPLOAD_QUALITY = 0.92; // 和服务端那次重编码对齐，少一次有损压缩
const previewUrls = { person: "", garment: "", garment2: "" };
const working = { person: false, garment: false, garment2: false }; // 还在处理的那一格

// 选图的两条路都汇到这里：本地文件、图库里挑的那张，进了槽位就没有区别了
function setSlotFile(kind, file) {
  files[kind] = file; // 上传前一直是原图，压缩推迟到点「开始试穿」之后
  working[kind] = true;
  setPickLabel(kind);
  readyToGo();
  showPreviewOnly(kind, file);
}

function clearSlot(kind) {
  files[kind] = null;
  working[kind] = false;
  URL.revokeObjectURL(previewUrls[kind]);
  previewUrls[kind] = "";
  const preview = $(`${kind}-preview`);
  preview.hidden = true;
  preview.removeAttribute("src");
  $(`frame-${kind}`).classList.remove("has-image");
  $(kind).value = "";
  setPickLabel(kind);
}

function bindFrame(kind) {
  $(kind).addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    e.target.value = ""; // 不清空的话，连续选同一张图不会再触发 change
    setSlotFile(kind, file);
  });
  $(`frame-${kind}`).addEventListener("click", (e) => {
    if (e.target.closest(".frame-lib")) return; // 那颗按钮走图库那条路
    if (e.target === $(kind)) return; // 点到隐藏的文件输入本身：浏览器已经在开选择框了，别再开一次
    $(kind).click(); // 取景框现在是个 div，不像 label 那样自带点开选择框
  });
}

function setPickLabel(kind) {
  $(`frame-${kind}`).querySelector(".frame-pick").textContent = working[kind] ? "处理中…" : "点这里选图";
}

// 只按单维缩放解码：让 JPEG 解码器在解码过程中就降采样。
// 不带 resize 地全解码一张 12MP 手机照片要 48MB 位图（50MP 是 191MB），
// 手机上那点内存就是"点了没反应十几秒"的来源 —— 时间倒是小问题。
async function decodeLongSide(file, longSide) {
  const probe = await createImageBitmap(file, { resizeWidth: 64 }); // 只为问出长短边
  const tall = probe.height >= probe.width;
  probe.close();
  return createImageBitmap(file, tall ? { resizeHeight: longSide } : { resizeWidth: longSide });
}

async function jpegFrom(bmp, quality) {
  const canvas = document.createElement("canvas");
  canvas.width = bmp.width;
  canvas.height = bmp.height;
  canvas.getContext("2d").drawImage(bmp, 0, 0);
  return new Promise((r) => canvas.toBlob(r, "image/jpeg", quality));
}

// 选图这一步只做预览（约 2MB 位图），压缩留到提交时 —— 预览不能排在压缩后面
async function showPreviewOnly(kind, file) {
  const preview = $(`${kind}-preview`);
  preview.decoding = "async";
  try {
    const bmp = await decodeLongSide(file, PREVIEW_SIDE);
    const blob = await jpegFrom(bmp, 0.85);
    bmp.close();
    showPreviewBlob(kind, blob || file);
  } catch {
    showPreviewBlob(kind, file); // 浏览器不配合就原图显示，不能什么都不显示
  }
  working[kind] = false;
  setPickLabel(kind);
  readyToGo();
}

// 要上传的那份：从原图缩放解到长边 ≤1536 再编码。压不小（原图本就小/细节少）就用原图，
// 免得把一张 600px 的图放大到 1536 再被服务端缩回去，白丢一层锐度。
async function uploadFileFor(kind) {
  const original = files[kind];
  try {
    const bmp = await decodeLongSide(original, UPLOAD_SIDE[genSize] || 1536);
    const blob = await jpegFrom(bmp, UPLOAD_QUALITY);
    bmp.close();
    if (blob && blob.size < original.size) {
      return new File([blob], original.name || `${kind}.jpg`, { type: "image/jpeg" });
    }
  } catch {
    /* 原样上传 */
  }
  return original;
}

function showPreviewBlob(kind, blob) {
  const preview = $(`${kind}-preview`);
  URL.revokeObjectURL(previewUrls[kind]);
  preview.src = URL.createObjectURL(blob);
  previewUrls[kind] = preview.src;
  preview.hidden = false;
  $(`frame-${kind}`).classList.add("has-image");
}

const slotLabel = (kind) => SLOT_TEXT[mode][slotsFor(mode).indexOf(kind)][0];

function goDisabled() {
  // 三个来源任一都按住：图片还在处理、自己这个请求在飞、服务端有任务在跑（后端锁）
  const slots = slotsFor(mode);
  if (serverBusy || localBusy) return true;
  if (working.person || slots.some((kind) => working[kind])) return true;
  return !(files.person && slots.every((kind) => files[kind]));
}

function hintText() {
  const slots = slotsFor(mode);
  if (working.person || slots.some((kind) => working[kind])) return "图片还在处理…";
  if (serverBusy)
    return `上一次还在跑（${isLocal(serverBusy.source) ? "本地 ComfyUI" : "API"} · 已 ${serverBusy.age_s} 秒），结束前不能再点。`;
  const need = [];
  if (!files.person) need.push("你自己的照片");
  for (const kind of slots) if (!files[kind]) need.push(slotLabel(kind));
  if (!need.length) return "";
  // 一张都没选时说"几张都选好"，选了一半时得说还差哪几张 —— 后者要认得人已经放进来的那张
  if (need.length === slots.length + 1) return `${need.length} 张都选好才能开始。`;
  return `还差${need.join("和")}。`;
}

function applyGoState() {
  $("go").disabled = goDisabled();
  $("again").disabled = Boolean(serverBusy) || localBusy;
  $("need-both").textContent = hintText();
}

function readyToGo() {
  applyGoState();
}

function setPct(pct) {
  const tape = $("tape");
  tape.style.setProperty("--pct", `${pct}%`);
  tape.setAttribute("aria-valuenow", String(Math.round(pct)));
}

function showTape() {
  clearInterval(ticker);
  $("tape").hidden = false;
  setPct(0);
}

function setUploadPct(loaded, total) {
  if (!total) {
    $("readout").textContent = "准备上传…";
    return;
  }
  const pct = Math.min(100, Math.round((loaded / total) * 100));
  const mb = (n) => (n / 1048576).toFixed(1);
  setPct(pct);
  $("readout").textContent = `上传中 ${pct}% · ${mb(loaded)}/${mb(total)} MB`;
}

function postTryon(form, onUploaded) {
  // 上传进度只有 XHR 给得出来，fetch 不提供 upload progress
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "api/tryon");
    let handedOver = false;
    const handOver = () => {
      if (handedOver) return;
      handedOver = true;
      onUploaded();
    };
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) setUploadPct(e.loaded, e.total);
    };
    xhr.upload.onload = handOver; // 传得太快、一次 progress 都没有时靠它兜底
    xhr.onload = () => {
      handOver();
      try {
        resolve(JSON.parse(xhr.responseText));
      } catch {
        resolve({ ok: false, stage: "api", message: `本机服务返回读不懂（HTTP ${xhr.status}）` });
      }
    };
    xhr.onerror = () =>
      resolve({ ok: false, stage: "api", message: "连不上本机服务，确认 python dev_run.py 还开着。" });
    xhr.send(form);
  });
}

function startTape(prefix = "生成中") {
  const startedAt = Date.now();
  showTape();
  const step = () => {
    const elapsed = Date.now() - startedAt;
    // 第二张参考图那 1.36 倍只在本地这条量过，API 那边没依据，所以不加进去
    const base = (ESTIMATE_MS[`${genSource}-${genSize}`] || 90000) * (isLocal(genSource) ? MODE_FACTOR[mode] || 1 : 1);
    setPct(Math.min(90, 100 * (1 - Math.exp((-1.6 * elapsed) / base))));
    $("readout").textContent = `${prefix} · ${Math.round(elapsed / 1000)} 秒`;
  };
  step();
  ticker = setInterval(step, 100);
}

function stopTape() {
  clearInterval(ticker);
  ticker = null;
  setPct(100);
  setTimeout(() => { $("tape").hidden = true; }, 900);
}

function usageLine(meta, elapsed) {
  const u = meta.usage || {};
  const kind = meta.mode && meta.mode !== "single" ? MODES[meta.mode] : null; // 单件是默认，不必每行都写
  if (isLocal(meta.source)) {
    // 本地这条没有接口回传的那套档位字段，说清是谁出的、哪一档、多大、多久就够
    const local = [meta.source === "comfyui_llama" ? "本地 ComfyUI · llama 编码" : "本地 ComfyUI"];
    if (meta.band) local.push(`${BAND_LABEL[meta.band] || meta.band} 档`); // 低档不能大写，那是 480P
    if (u.output_width && u.output_height) local.push(`输出 ${u.output_width}×${u.output_height}`);
    if (kind) local.splice(1, 0, kind);
    local.push(`用时 ${elapsed} 秒`);
    return local;
  }
  const parts = [];
  if (kind) parts.push(kind);
  if (u.output_width && u.output_height) parts.push(`输出 ${u.output_width}×${u.output_height}`);
  if (u.output_image_type) parts.push(`${u.output_image_type.replace("qima_output_", "").toUpperCase()} 档`);
  if (u.output_image_count) parts.push(`${u.output_image_count} 张`);
  parts.push(`用时 ${elapsed} 秒`);
  if (meta.rewrite_status === "success") parts.push("指令已自动改写");
  return parts;
}

function showResult(data) {
  const box = $("result");
  box.hidden = false;
  box.classList.remove("error");
  $("result-img").src = data.record.files.result;
  currentRecord = data.record;
  const bits = usageLine(data.record.meta, data.elapsed_s);
  const detail = document.createElement("span");
  detail.textContent = bits.join(" · ");
  $("result-meta").replaceChildren(detail);
  $("again").hidden = false;
}

function showError(body) {
  const box = $("result");
  box.hidden = false;
  box.classList.add("error");
  const bits = [body.message || "生成失败"];
  if (body.code) bits.push(`代码 ${body.code}`);
  if (body.request_id) bits.push(`请求号 ${body.request_id}`);
  if (body.result_url) bits.push(`结果链接 ${body.result_url}`);
  const detail = document.createElement("span");
  detail.textContent = bits.join("\n");
  detail.style.whiteSpace = "pre-wrap";
  $("result-meta").replaceChildren(detail);
}

function setBusy(busy) {
  localBusy = busy;
  applyGoState();
}

// 页眉和确认框共用这一句：单件那档 480p/720p/1K 是 11-03 的实测点（2K 是旧配置的数，且本机这条选不到它），
// 两个新分类只有 720p 是实测，别的档位是照 720p 那两个点乘 1.36 推的 —— 推的就说是推的。
function waitText() {
  // 缺格退回第 2 档那一行的同档数：llama 那行没写 2K（本机两条都选不到它），
  // 但 `||` 只在整个来源行缺失时才兜底，格子缺失会算出 0 秒 —— 浏览器里真撞出来过一句"实测约 0 秒"
  const perBand = LOCAL_SECONDS[genSource] || {};
  const raw = (perBand[genSize] ?? LOCAL_SECONDS.comfyui[genSize] ?? 0) * (MODE_FACTOR[mode] || 1);
  const s = raw >= 60 ? Math.round(raw / 5) * 5 : Math.round(raw);
  const shown = s >= 120 ? `${Math.round(s / 60)} 分钟` : `${s} 秒`;
  return mode === "single" || genSize === "720p" ? `实测约 ${shown}` : `约 ${shown}（按 720p 的实测点推）`;
}

function confirmStart() {
  const band = BAND_LABEL[genSize];
  if (isLocal(genSource)) {
    return confirm(
      `开始试穿？\n\n${genSource === "comfyui_llama" ? "本机 ComfyUI · llama.cpp 编码" : "本机 ComfyUI"} 生成 · ${MODES[mode]} · ${band} 档 · 不花额度 · ${waitText()}。\n\n取消不动图片。`
    );
  }
  const cost = quota
    ? `会花掉 ${quota.per} credit（${band} 档），本月还能试穿 ${quota.times} 次。`
    : `会花掉一次 ${band} 生成的额度（额度查不到）。`;
  const wait = genSize === "2k" ? "要等约 2–3 分钟。" : "要等约 80–100 秒。";
  return confirm(`开始试穿？\n\n${MODES[mode]} · ${cost}${wait}\n\n取消不动图片，也不扣额度。`);
}

async function submit() {
  const slots = slotsFor(mode);
  if (!files.person || !slots.every((kind) => files[kind])) return;
  if (!confirmStart()) return; // 一次点击就是一条额度，得能反悔
  setBusy(true);
  showTape();
  $("readout").textContent = "压缩中…"; // 这一步只在点下去之后发生，选图时不挡手

  const form = new FormData();
  form.append("mode", mode); // 分类决定服务端发哪条指令、收几张参考图
  form.append("person", await uploadFileFor("person"));
  for (const kind of slots) form.append(kind, await uploadFileFor(kind));
  setUploadPct(0, 0);

  // 上传段是浏览器给的真实进度；传完才切换到估算的生成进度
  const body = await postTryon(form, () => startTape("已上传，生成中"));

  stopTape();
  setBusy(false);
  if (body.ok) showResult(body);
  else showError(body);
  loadGallery(true);
  loadQuota(); // 失败的那次也可能已经扣掉额度，重查一遍
}

async function loadQuota() {
  const el = $("quota");
  try {
    const body = await (await fetch("api/quota")).json();
    genSource = ["api", ...LOCAL_SOURCES].includes(body.source) ? body.source : "api"; // 认后端报的那三个，别把第 3 档折成 API
    genSize = BANDS.includes(body.size) ? body.size : "1k"; // 只认后端报的四档之一，其余一律当默认档
    // 后端的生成锁。这一条是给"连接被 nginx 掐断"那种情况兜底的：前端只知道请求失败了，
    // 服务端其实还在跑 —— 按钮得继续按住，过几秒再问一次，跑完顺手把图库刷出来（图本来就在库里）。
    const wasBusy = serverBusy;
    serverBusy = body.busy || null;
    applyGoState();
    clearTimeout(busyTimer);
    if (serverBusy) busyTimer = setTimeout(loadQuota, 5000);
    else if (wasBusy) loadGallery(true);
    if (isLocal(genSource)) {
      // 走本地时后端根本不去查额度；这一行也不假装知道 ComfyUI 连不连得上
      quota = null;
      // 本地这条不查额度，也不假装知道 ComfyUI 连不连得上；秒数是同机实测的那三个点
      renderLocalLine();
      return;
    }
    if (body.ok) {
      const q = body.quota;
      const per = body.credits_per_generation || body.credits_per_1k;
      // 次数按所选档位折算：2K 一次 108，拿 58 算出来的次数是骗人的
      quota = { times: q.times || q.times_1k, per };
      el.textContent =
        `还能试穿 ${quota.times} 次 · ${genSize.toUpperCase()} 档一次 ${per} credit · ` +
        `${q.remaining_days} 天后额度重置`;
    } else {
      quota = null;
      el.textContent = `额度查不到：${body.message}`;
    }
  } catch {
    quota = null;
    // 连后端都问不到，就别把按钮永久按住：能不能跑由下一次请求自己告诉你
    serverBusy = null;
    applyGoState();
    el.textContent = "额度查不到：连不上本机服务。";
  }
}

// 进页面那一次的来源体检：存的若是本机 ComfyUI 而它没在线，后端当场把设置切成 token plan 1K，
// 这里负责把这件事说出来。为什么是进页面时切、而不是点"开始试穿"时切：换来源就是换模型换价钱，
// 藏在一次点击底下是挖质量线的坑；弹窗里那句"一次 58 credit"和待会儿确认框、页眉用的是同一个数。
async function checkComfyOnLoad() {
  let body;
  try {
    const r = await fetch("api/comfy/fallback", { method: "POST" });
    if (!r.ok) return; // 后端答不了：页面自己也是靠这条才刷得出状态，不在这儿补一句错
    body = await r.json();
  } catch {
    return; // 网络断了同理，别拿一句弹窗挡住人
  }
  if (!body.switched) return;
  await loadQuota(); // 页眉那行、剩余次数、确认框的 credit 数全部跟着变成真的 API 状态
  alert(
    `${body.reason}，已自动切到 token plan。\n\n` +
      `档位 ${BAND_LABEL[body.size]} · 一次 ${body.credits} credit。\n\n` +
      `ComfyUI 起来之后去「设置」把来源切回「本机 ComfyUI」，地址还留着，档位也得重新点一次。`
  );
}

// —— 分类：槽位、名字、提示都跟着它变 ——


function slotsFor(m) {
  return MODE_SLOTS[m] || MODE_SLOTS.single;
}

function applyMode(next) {
  mode = MODES[next] ? next : "single"; // 存过的值不认识就回默认，不报错
  try {
    localStorage.setItem("tryon.mode", mode);
  } catch {
    /* 隐私模式下存不上，不影响这一次 */
  }
  const slots = slotsFor(mode);
  $("frames").style.setProperty("--cols", String(1 + slots.length)); // 人像 + 参考图
  for (const btn of $("modes").children) btn.setAttribute("aria-pressed", String(btn.dataset.mode === mode));
  for (const kind of ALL_SLOTS) {
    const frame = $(`frame-${kind}`);
    if (!slots.includes(kind)) {
      frame.hidden = true;
      if (files[kind]) clearSlot(kind); // 藏起来的槽不许还在提交里
      continue;
    }
    frame.hidden = false;
    const [name, hint] = SLOT_TEXT[mode][slots.indexOf(kind)];
    frame.querySelector(".frame-name").textContent = name;
    frame.querySelector(".frame-hint").textContent = hint;
  }
  renderLocalLine(); // 换分类就是换耗时那句要说给页眉的话
  readyToGo();
}

// —— sample 图库弹层 ——

let tree = null; // 目录树，开弹层时拉一次；null = 没拉到
let pkSlot = "garment";
let pkGroup = 0;
let pkSub = 0;

async function openPicker(kind) {
  pkSlot = kind;
  const name = kind === "person" ? "你自己的照片" : slotLabel(kind);
  $("pk-title").textContent = `从图库里挑一张 · ${name}`;
  $("picker").hidden = false;
  $("pk-note").textContent = "挑图加载中…";
  try {
    const r = await fetch("api/samples");
    tree = r.ok ? (await r.json()).groups || [] : null;
  } catch {
    tree = null; // 服务没起来：弹层里说一句就行，别拿一句弹窗挡人
  }
  renderPicker();
}

function closePicker() {
  $("picker").hidden = true;
}

function renderPicker() {
  const groups = tree || [];
  pkGroup = Math.min(pkGroup, Math.max(0, groups.length - 1));
  const subs = groups[pkGroup] ? groups[pkGroup].subs : [];
  if (!groups.length) {
    $("pk-subs").replaceChildren();
    $("pk-grid").replaceChildren();
    $("pk-note").textContent = note();
    return;
  }
  pkSub = Math.min(pkSub, Math.max(0, subs.length - 1));

  $("pk-groups").replaceChildren(
    ...groups.map((g, i) => tab(g.label || g.group, i === pkGroup, () => {
      pkGroup = i;
      pkSub = 0;
      renderPicker();
    }))
  );
  // 一个大类只有一节时（发型那些没细分的），再来一排 tab 是废话
  const single = subs.length === 1 && subs[0].name === groups[pkGroup].group;
  const subTabs = single ? [] : subs.map((s, i) => tab(s.label || s.name, i === pkSub, () => {
    pkSub = i;
    renderPicker();
  }));
  $("pk-subs").replaceChildren(...subTabs);

  const items = subs[pkSub] ? subs[pkSub].items : [];
  $("pk-grid").replaceChildren(...items.map((item) => cell(item)));
  $("pk-note").textContent = note();
}

function tab(label, on, click) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = label;
  if (on) btn.setAttribute("aria-current", "true");
  btn.addEventListener("click", click);
  return btn;
}

function cell(item) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "pk-item";
  const img = document.createElement("img");
  img.src = item.thumb;
  img.alt = item.name;
  img.loading = "lazy";
  const cap = document.createElement("span");
  cap.textContent = item.name;
  btn.append(img, cap);
  btn.addEventListener("click", () => pickFromLibrary(item, btn));
  return btn;
}

function note() {
  if (!tree) return "图库读不到：连不上本机服务，或者还没登录。";
  if (!tree.length)
    return "图库还是空的。点下面的「管理图库」加图，或者把图片放进 data/samples/menswear/top/ 这样的目录（大类/细分类/图.jpg，名字用英文），回到这里重开一次就有。";
  return `共 ${tree.reduce((n, g) => n + g.subs.reduce((m, s) => m + s.items.length, 0), 0)} 张 · 挑中的会填进「${
    pkSlot === "person" ? "你的照片" : slotLabel(pkSlot)
  }」那一格。`;
}

async function pickFromLibrary(item, btn) {
  btn.disabled = true; // 取一张原图也要一两秒，按住免得连点两次
  try {
    const r = await fetch(item.url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const blob = await r.blob();
    const ext = { "image/png": ".png", "image/webp": ".webp" }[blob.type] || ".jpg";
    setSlotFile(pkSlot, new File([blob], `${item.name}${ext}`, { type: blob.type || "image/jpeg" }));
    closePicker();
  } catch (e) {
    $("pk-note").textContent = `那张取不过来（${e.message}）。换一张，或者从本地选。`;
  } finally {
    btn.disabled = false;
  }
}

// 页眉那一行：只有走本地时它带着秒数，而秒数现在要看分类
function renderLocalLine() {
  if (!isLocal(genSource)) return;
  $("quota").textContent = `${genSource === "comfyui_llama" ? "本机 ComfyUI · llama.cpp 编码" : "本机 ComfyUI"} · ${MODES[mode]} · ${BAND_LABEL[genSize]} 档 · 不花额度 · ${waitText()}`;
}

function recordCell(rec) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.dataset.id = rec.id;
  btn.dataset.result = rec.files.result;
  btn.dataset.person = rec.files.person;
  btn.dataset.garment = rec.files.garment;
  btn.dataset.garment2 = rec.files.garment2 || "";
  btn.dataset.mode = rec.meta.mode || "single";
  btn.dataset.caption = `${rec.meta.created_at} · ${usageLine(rec.meta, rec.meta.elapsed_s).join(" · ")}`;
  const badge = document.createElement("span");
  badge.className = "src-badge";
  badge.textContent = isLocal(rec.meta.source) ? "本地" : "API"; // 两条本地来源共用一个角标：哪颗编码出的图写在标题那行里
  badge.setAttribute("aria-hidden", "true"); // 来源在灯箱标题里也有一份，这里只是给眼睛看的
  btn.append(badge);
  const shown = [["person", "本人照片"], ["garment", slotCap(rec.meta.mode, 0)], ["result", "试穿成品"]];
  if (rec.files.garment2) shown.splice(2, 0, ["garment2", slotCap(rec.meta.mode, 1)]);
  for (const [key, alt] of shown) {
    const img = document.createElement("img");
    img.src = key === "result" ? rec.files.thumb : rec.files[key];
    img.alt = alt;
    img.loading = "lazy";
    btn.append(img);
  }
  btn.addEventListener("click", () => openLightbox(btn.dataset));
  return btn;
}

function renderGallery() {
  const pages = Math.max(1, Math.ceil(records.length / PAGE_SIZE));
  page = Math.min(Math.max(1, page), pages); // 删完最后一页要自动退回去
  $("grid").replaceChildren(
    ...records.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE).map(recordCell)
  );

  const pager = $("pager");
  pager.replaceChildren(
    ...Array.from({ length: pages }, (_, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = String(i + 1);
      if (i + 1 === page) btn.setAttribute("aria-current", "page");
      btn.addEventListener("click", () => {
        page = i + 1;
        renderGallery();
      });
      return btn;
    })
  );
  pager.hidden = pages <= 1;
  $("empty").hidden = records.length > 0;
}

async function loadGallery(gotoFirst) {
  try {
    records = (await (await fetch("api/records")).json()).records || [];
  } catch {
    records = []; // 服务还没起来，图库空着就好
  }
  if (gotoFirst) page = 1; // 新生成的在最前面，别留在第三页看空
  renderGallery();
}

// 记录里那两张参考图按分类叫什么：单件是衣服，套装是上衣和下装，发型那次是发型参考
function slotCap(m, index) {
  const table = SLOT_TEXT[m] || SLOT_TEXT.single; // 老记录里没有 mode，按单件读
  return (table[index] || ["参考图"])[0];
}

function openLightbox({ result, person, garment, garment2, caption, id, mode: m }) {
  $("lb-person").src = person;
  $("lb-garment").src = garment;
  $("lb-garment-cap").textContent = slotCap(m, 0);
  const second = $("lb-ref2");
  second.hidden = !garment2;
  if (garment2) {
    $("lb-garment2").src = garment2;
    $("lb-garment2-cap").textContent = slotCap(m, 1);
  }
  $("lb-img").src = result;
  $("lb-caption").textContent = caption;
  $("lb-download").href = result;
  $("lb-delete").dataset.id = id;
  $("lightbox").hidden = false;
}

function closeLightbox() {
  $("lightbox").hidden = true;
  for (const id of ["lb-img", "lb-person", "lb-garment", "lb-garment2"]) $(id).removeAttribute("src");
}

async function deleteCurrent() {
  const id = $("lb-delete").dataset.id;
  if (!confirm("删掉这条记录？你的照片、衣服照片和成品会一起从磁盘移除。")) return;
  try { await fetch(`api/records/${id}`, { method: "DELETE" }); } catch { /* 下面重新拉列表会反映真实状态 */ }
  if (currentRecord && currentRecord.id === id) {
    $("result").hidden = true;
    currentRecord = null;
  }
  closeLightbox();
  loadGallery();
}

bindFrame("person");
bindFrame("garment");
bindFrame("garment2");
for (const btn of $("modes").children) btn.addEventListener("click", () => applyMode(btn.dataset.mode));
for (const btn of document.querySelectorAll(".frame-lib"))
  btn.addEventListener("click", () => openPicker(btn.dataset.slot));
$("pk-close").addEventListener("click", closePicker);
$("picker").addEventListener("click", (e) => { if (e.target === $("picker")) closePicker(); });
applyMode(localStorage.getItem("tryon.mode") || "single"); // 上次点过的分类，页面一开就在
$("go").addEventListener("click", submit);
$("again").addEventListener("click", submit);
$("lb-delete").addEventListener("click", deleteCurrent);
$("lb-close").addEventListener("click", closeLightbox);
$("lightbox").addEventListener("click", (e) => { if (e.target === $("lightbox")) closeLightbox(); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  closeLightbox();
  closePicker(); // 弹层开着时按 Esc 也该关，两个都关无害
});
loadGallery();
loadQuota();
checkComfyOnLoad(); // 本机不在线就在这儿说，别等点下去才报错
// 手机从设置页按物理返回键回来时，页面是从 bfcache 直接恢复的，不重跑初始化。
// 不补这一句的话，来源换了而确认框还写着"不花额度"——那点下去就是 58 credit。
window.addEventListener("pageshow", (e) => {
  loadQuota();
  loadGallery();
  // 只在真是从 bfcache 恢复时（persisted）才再问一次健康：首屏上面已经问过了，Chrome 首次加载
  // 也会派一次 pageshow —— 发两遍就是两个请求同时写设置文件，2026-11-01 实测那样会 500、弹窗丢。
  if (e.persisted) checkComfyOnLoad(); // 在设置页把来源点回本机、ComfyUI 却没起：回来的这一下同样要拦
});
