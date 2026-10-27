// 设置页：选生成来源、填 ComfyUI 地址、自检、保存。
// 站内 URL 一律不带前导斜杠 —— 挂在 /mfr/ 这种子路径下才解析得对（设计文档 §6.7）。

const $ = (id) => document.getElementById(id);

let dirty = false;

function chosenSource() {
  const hit = document.querySelector('input[name="source"]:checked');
  return hit ? hit.value : "api";
}

function typedUrl() {
  return $("comfy-url").value.trim();
}

function setState(text) {
  $("save-state").textContent = text;
}

function markDirty() {
  dirty = true;
  setState("有没保存的改动");
}

function showProblems(items, headline) {
  const box = $("problems");
  box.replaceChildren(
    ...[headline, ...items].map((text) => {
      const li = document.createElement("li");
      li.textContent = text; // 地址和后端报错都是用户输入，不走 innerHTML
      return li;
    })
  );
  box.hidden = false;
}

function clearProblems() {
  $("problems").replaceChildren();
  $("problems").hidden = true;
}

async function post(path, payload) {
  try {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return await r.json();
  } catch {
    return { ok: false, message: "连不上本机服务，确认 python dev_run.py 还开着。" };
  }
}

async function load() {
  try {
    const body = await (await fetch("api/settings")).json();
    const hit = document.querySelector(`input[name="source"][value="${body.source}"]`);
    if (hit) hit.checked = true;
    $("comfy-url").value = body.comfy_url || "";
    $("comfy-url").placeholder = body.default_comfy_url || "";
    setState("已保存");
  } catch {
    setState("读不到现有设置：确认本机服务还开着");
  }
}

async function save() {
  const payload = { source: chosenSource(), comfy_url: typedUrl() };
  const body = await post("api/settings", payload);
  if (!body.ok) {
    showProblems([], `没保存：${body.message || "未知原因"}`);
    setState("没保存成功");
    return;
  }
  clearProblems();
  dirty = false;
  setState("已保存");
}

async function checkNow() {
  setState("自检中…");
  const body = await post("api/settings/check", { comfy_url: typedUrl() });
  if (body.ok) {
    clearProblems();
    setState("都通");
    return;
  }
  showProblems(body.problems || [], "自检没通过：");
  setState("自检没通过");
}

for (const radio of document.querySelectorAll('input[name="source"]')) {
  radio.addEventListener("change", markDirty);
}
$("comfy-url").addEventListener("input", markDirty);
$("save").addEventListener("click", save);
$("check").addEventListener("click", checkNow);

load();
