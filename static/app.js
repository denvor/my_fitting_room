// 试衣间前端：选两张图 → 一次请求 → 估算进度 + 真实秒数 → 结果与图库。

const ESTIMATE_MS = 55000; // 实测 1K 档 55.8 秒，用它估进度条

const $ = (id) => document.getElementById(id);
const files = { person: null, garment: null };
let ticker = null;
let currentRecord = null;

function bindFrame(kind) {
  $(kind).addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    files[kind] = file;
    const preview = $(`${kind}-preview`);
    preview.src = URL.createObjectURL(file);
    preview.hidden = false;
    $(`frame-${kind}`).classList.add("has-image");
    readyToGo();
  });
}

function readyToGo() {
  const both = Boolean(files.person && files.garment);
  $("go").disabled = !both;
  $("need-both").textContent = both ? "" : files.person ? "还差衣服照片。" : files.garment ? "还差你的照片。" : "两张都选好才能开始。";
}

function setPct(pct) {
  const tape = $("tape");
  tape.style.setProperty("--pct", `${pct}%`);
  tape.setAttribute("aria-valuenow", String(Math.round(pct)));
}

function startTape() {
  const startedAt = Date.now();
  $("tape").hidden = false;
  const step = () => {
    const elapsed = Date.now() - startedAt;
    setPct(Math.min(90, 100 * (1 - Math.exp((-1.6 * elapsed) / ESTIMATE_MS))));
    $("readout").textContent = `生成中 · ${Math.round(elapsed / 1000)} 秒`;
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

async function submit() {
  if (!files.person || !files.garment) return;
  setBusy(true);
  startTape();

  const form = new FormData();
  form.append("person", files.person);
  form.append("garment", files.garment);

  let body;
  try {
    const resp = await fetch("/api/tryon", { method: "POST", body: form });
    body = await resp.json();
  } catch {
    body = { ok: false, stage: "api", message: "连不上本机服务，确认 python app.py 还开着。" };
  }

  stopTape();
  setBusy(false);
  if (body.ok) showResult(body);
  else showError(body);
  loadGallery();
}

async function loadGallery() {
  let records = [];
  try {
    records = (await (await fetch("/api/records")).json()).records || [];
  } catch { /* 服务还没起来，图库空着就好 */ }

  const grid = $("grid");
  grid.replaceChildren(...records.map((rec) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.id = rec.id;
    btn.dataset.result = rec.files.result;
    btn.dataset.person = rec.files.person;
    btn.dataset.garment = rec.files.garment;
    btn.dataset.caption = `${rec.meta.created_at} · ${usageLine(rec.meta, rec.meta.elapsed_s).join(" · ")}`;
    for (const [key, alt] of [["person", "本人照片"], ["garment", "衣服照片"], ["result", "试穿成品"]]) {
      const img = document.createElement("img");
      img.src = key === "result" ? rec.files.thumb : rec.files[key];
      img.alt = alt;
      img.loading = "lazy";
      btn.append(img);
    }
    btn.addEventListener("click", () => openLightbox(btn.dataset));
    return btn;
  }));
  $("empty").hidden = records.length > 0;
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
  try { await fetch(`/api/records/${id}`, { method: "DELETE" }); } catch { /* 下面重新拉列表会反映真实状态 */ }
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
