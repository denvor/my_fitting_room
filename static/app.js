// 试衣间前端：选两张图 → 一次请求 → 估算进度 + 真实秒数 → 结果与图库。

const ESTIMATE_MS = { api: 90000, comfyui: 30000 }; // 两条来源各自的实测中位数，用它估进度条
let genSource = "api"; // 由 api/quota 回填，进度基数、确认框、页眉都看它

const PAGE_SIZE = 5; // 图库每页几条

const $ = (id) => document.getElementById(id);
const files = { person: null, garment: null };
let ticker = null;
let currentRecord = null;
let records = [];
let page = 1;
let quota = null; // 页眉那个数字，开始前的确认要用

const PREVIEW_SIDE = 720; // 取景框就这么大，没必要画 3072×4096 的原图
const UPLOAD_MAX_SIDE = 1536; // 服务端本来就把长边缩到这里，先在手机里缩好，省掉几 MB 上行
const UPLOAD_QUALITY = 0.92; // 和服务端那次重编码对齐，少一次有损压缩
const previewUrls = { person: "", garment: "" };
const working = { person: false, garment: false }; // 还在压缩的那一格

function bindFrame(kind) {
  $(kind).addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    files[kind] = file; // 先记下原图：压缩不配合或失败就按原样上传
    working[kind] = true;
    setPickLabel(kind);
    readyToGo();
    prepareImage(kind, file);
  });
}

function setPickLabel(kind) {
  $(`frame-${kind}`).querySelector(".frame-pick").textContent = working[kind] ? "处理中…" : "点这里选图";
}

async function scaledBlob(bmp, scale, quality) {
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bmp.width * scale));
  canvas.height = Math.max(1, Math.round(bmp.height * scale));
  canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
  return new Promise((r) => canvas.toBlob(r, "image/jpeg", quality));
}

// 一次解码出两份：要上传的（长边 ≤1536）和取景框里看的（长边 ≤720）
async function prepareImage(kind, file) {
  const preview = $(`${kind}-preview`);
  preview.decoding = "async"; // 解码别占主线程，手机上"点了没反应"就是它
  try {
    const bmp = await createImageBitmap(file, { imageOrientation: "from-image" }); // EXIF 方向在这一步定死
    const long = Math.max(bmp.width, bmp.height);
    const upload = long > UPLOAD_MAX_SIDE ? await scaledBlob(bmp, UPLOAD_MAX_SIDE / long, UPLOAD_QUALITY) : null;
    const view = long > PREVIEW_SIDE ? await scaledBlob(bmp, PREVIEW_SIDE / long, 0.85) : null;
    bmp.close();
    if (upload) files[kind] = new File([upload], file.name || `${kind}.jpg`, { type: "image/jpeg" });
    showPreviewBlob(kind, view || upload || file);
  } catch {
    showPreviewBlob(kind, file); // 浏览器不配合就原图上传、原图显示，不能什么都不显示
  }
  working[kind] = false;
  setPickLabel(kind);
  readyToGo();
}

function showPreviewBlob(kind, blob) {
  const preview = $(`${kind}-preview`);
  URL.revokeObjectURL(previewUrls[kind]);
  preview.src = URL.createObjectURL(blob);
  previewUrls[kind] = preview.src;
  preview.hidden = false;
  $(`frame-${kind}`).classList.add("has-image");
}

function readyToGo() {
  const both = Boolean(files.person && files.garment);
  const busy = working.person || working.garment;
  $("go").disabled = !both || busy;
  $("need-both").textContent = busy
    ? "图片还在处理…"
    : both
      ? ""
      : files.person
        ? "还差衣服照片。"
        : files.garment
          ? "还差你的照片。"
          : "两张都选好才能开始。";
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
    setPct(Math.min(90, 100 * (1 - Math.exp((-1.6 * elapsed) / ESTIMATE_MS[genSource]))));
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
    // 本地这条没有接口回传的那套档位字段，说清是谁出的、多大、多久就够
    const local = ["本地 ComfyUI"];
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
  $("go").disabled = busy || !(files.person && files.garment);
  $("again").disabled = busy;
}

function confirmStart() {
  if (genSource === "comfyui") {
    return confirm("开始试穿？\n\n本机 ComfyUI 生成 · 不花额度 · 约 20–95 秒。\n\n取消不动图片。");
  }
  const cost = quota
    ? `会花掉 ${quota.per} credit，本月还能试穿 ${quota.times} 次。`
    : "会花掉一次生成的额度（额度查不到）。";
  return confirm(`开始试穿？\n\n${cost}要等约 80–100 秒。\n\n取消不动图片，也不扣额度。`);
}

async function submit() {
  if (!files.person || !files.garment) return;
  if (!confirmStart()) return; // 一次点击就是一条额度，得能反悔
  setBusy(true);
  showTape();
  setUploadPct(0, 0);

  const form = new FormData();
  form.append("person", files.person);
  form.append("garment", files.garment);

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
    if (genSource === "comfyui") {
      // 走本地时后端根本不去查额度；这一行也不假装知道 ComfyUI 连不连得上
      quota = null;
      el.textContent = "本机 ComfyUI · 不花额度 · 约 20–95 秒";
      return;
    }
    if (body.ok) {
      const q = body.quota;
      quota = { times: q.times_1k, per: body.credits_per_1k };
      el.textContent = `还能试穿 ${q.times_1k} 次 · 一次 ${body.credits_per_1k} credit · ${q.remaining_days} 天后额度重置`;
    } else {
      quota = null;
      el.textContent = `额度查不到：${body.message}`;
    }
  } catch {
    quota = null;
    el.textContent = "额度查不到：连不上本机服务。";
  }
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
