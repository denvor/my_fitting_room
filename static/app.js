// 试衣间前端：选两张图 → 一次请求 → 估算进度 + 真实秒数 → 结果与图库。

// 进度条的估算基数：来源 × 档位。api-2k 那个是按像素比推的、没实测，其余都有实测点（见 README 耗时表）。
// 本地这四个数是 2026-10-31 同机连测出来的：480p 26.0/26.1/26.2 秒、720p 70.5/70.6 秒、1K 95.1/95.4/96.2 秒、2K 543 秒。
const ESTIMATE_MS = {
  "api-1k": 90000,
  "api-2k": 170000,
  "comfyui-480p": 26000,
  "comfyui-720p": 70000,
  "comfyui-1k": 95000,
  "comfyui-2k": 540000,
};
const BANDS = ["480p", "720p", "1k", "2k"]; // 和后端 prefs.VALID_SIZES 同一套顺序
const BAND_LABEL = { "480p": "480p", "720p": "720p", "1k": "1K", "2k": "2K" }; // 低档大写会变成 480P，那是型号写法不是这里要的
const LOCAL_WAIT = { "480p": "约 26 秒", "720p": "约 70 秒", "1k": "约 95 秒", "2k": "约 9 分钟" };
let genSource = "api"; // 由 api/quota 回填，进度基数、确认框、页眉都看它
let genSize = "1k"; // 同上，档位决定浏览器压到多大、以及一次花多少 credit

const PAGE_SIZE = 5; // 图库每页几条

const $ = (id) => document.getElementById(id);
const files = { person: null, garment: null };
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
const previewUrls = { person: "", garment: "" };
const working = { person: false, garment: false }; // 还在处理的那一格

function bindFrame(kind) {
  $(kind).addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    files[kind] = file; // 上传前一直是原图，压缩推迟到点「开始试穿」之后
    e.target.value = ""; // 不清空的话，连续选同一张图不会再触发 change
    working[kind] = true;
    setPickLabel(kind);
    readyToGo();
    showPreviewOnly(kind, file);
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

function goDisabled() {
  // 三个来源任一都按住：图片还在处理、自己这个请求在飞、服务端有任务在跑（后端锁）
  return Boolean(serverBusy) || localBusy || working.person || working.garment || !(files.person && files.garment);
}

function hintText() {
  if (working.person || working.garment) return "图片还在处理…";
  if (serverBusy)
    return `上一次还在跑（${serverBusy.source === "comfyui" ? "本地 ComfyUI" : "API"} · 已 ${serverBusy.age_s} 秒），结束前不能再点。`;
  if (files.person && files.garment) return "";
  return files.person ? "还差衣服照片。" : files.garment ? "还差本人照片。" : "两张都选好才能开始。";
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
    setPct(Math.min(90, 100 * (1 - Math.exp((-1.6 * elapsed) / ESTIMATE_MS[`${genSource}-${genSize}`]))));
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
  if (meta.source === "comfyui") {
    // 本地这条没有接口回传的那套档位字段，说清是谁出的、哪一档、多大、多久就够
    const local = ["本地 ComfyUI"];
    if (meta.band) local.push(`${BAND_LABEL[meta.band] || meta.band} 档`); // 低档不能大写，那是 480P
    if (u.output_width && u.output_height) local.push(`输出 ${u.output_width}×${u.output_height}`);
    local.push(`用时 ${elapsed} 秒`);
    return local;
  }
  const parts = [];
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

function confirmStart() {
  const band = BAND_LABEL[genSize];
  if (genSource === "comfyui") {
    return confirm(
      `开始试穿？\n\n本机 ComfyUI 生成 · ${band} 档 · 不花额度 · 实测${LOCAL_WAIT[genSize]}。\n\n取消不动图片。`
    );
  }
  const cost = quota
    ? `会花掉 ${quota.per} credit（${band} 档），本月还能试穿 ${quota.times} 次。`
    : `会花掉一次 ${band} 生成的额度（额度查不到）。`;
  const wait = genSize === "2k" ? "要等约 2–3 分钟。" : "要等约 80–100 秒。";
  return confirm(`开始试穿？\n\n${cost}${wait}\n\n取消不动图片，也不扣额度。`);
}

async function submit() {
  if (!files.person || !files.garment) return;
  if (!confirmStart()) return; // 一次点击就是一条额度，得能反悔
  setBusy(true);
  showTape();
  $("readout").textContent = "压缩中…"; // 这一步只在点下去之后发生，选图时不挡手

  const form = new FormData();
  form.append("person", await uploadFileFor("person"));
  form.append("garment", await uploadFileFor("garment"));
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
    genSource = body.source === "comfyui" ? "comfyui" : "api";
    genSize = BANDS.includes(body.size) ? body.size : "1k"; // 只认后端报的四档之一，其余一律当默认档
    // 后端的生成锁。这一条是给"连接被 nginx 掐断"那种情况兜底的：前端只知道请求失败了，
    // 服务端其实还在跑 —— 按钮得继续按住，过几秒再问一次，跑完顺手把图库刷出来（图本来就在库里）。
    const wasBusy = serverBusy;
    serverBusy = body.busy || null;
    applyGoState();
    clearTimeout(busyTimer);
    if (serverBusy) busyTimer = setTimeout(loadQuota, 5000);
    else if (wasBusy) loadGallery(true);
    if (genSource === "comfyui") {
      // 走本地时后端根本不去查额度；这一行也不假装知道 ComfyUI 连不连得上
      quota = null;
      // 本地这条不查额度，也不假装知道 ComfyUI 连不连得上；秒数是同机实测的那三个点
      el.textContent = `本机 ComfyUI · ${BAND_LABEL[genSize]} 档 · 不花额度 · 实测${LOCAL_WAIT[genSize]}`;
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

function recordCell(rec) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.dataset.id = rec.id;
  btn.dataset.result = rec.files.result;
  btn.dataset.person = rec.files.person;
  btn.dataset.garment = rec.files.garment;
  btn.dataset.caption = `${rec.meta.created_at} · ${usageLine(rec.meta, rec.meta.elapsed_s).join(" · ")}`;
  const badge = document.createElement("span");
  badge.className = "src-badge";
  badge.textContent = rec.meta.source === "comfyui" ? "本地" : "API";
  badge.setAttribute("aria-hidden", "true"); // 来源在灯箱标题里也有一份，这里只是给眼睛看的
  btn.append(badge);
  for (const [key, alt] of [["person", "本人照片"], ["garment", "衣服照片"], ["result", "试穿成品"]]) {
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

function openLightbox({ result, person, garment, caption, id }) {
  $("lb-person").src = person;
  $("lb-garment").src = garment;
  $("lb-img").src = result;
  $("lb-caption").textContent = caption;
  $("lb-download").href = result;
  $("lb-delete").dataset.id = id;
  $("lightbox").hidden = false;
}

function closeLightbox() {
  $("lightbox").hidden = true;
  for (const id of ["lb-img", "lb-person", "lb-garment"]) $(id).removeAttribute("src");
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
$("go").addEventListener("click", submit);
$("again").addEventListener("click", submit);
$("lb-delete").addEventListener("click", deleteCurrent);
$("lb-close").addEventListener("click", closeLightbox);
$("lightbox").addEventListener("click", (e) => { if (e.target === $("lightbox")) closeLightbox(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeLightbox(); });
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
