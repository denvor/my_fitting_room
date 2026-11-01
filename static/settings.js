// 设置页：选生成来源、填 ComfyUI 地址、自检、保存。
// 站内 URL 一律不带前导斜杠 —— 挂在 /mfr/ 这种子路径下才解析得对（设计文档 §6.7）。

const $ = (id) => document.getElementById(id);

let dirty = false;

function chosenSource() {
  const hit = document.querySelector('input[name="source"]:checked');
  return hit ? hit.value : "api";
}

function chosenSize() {
  const hit = document.querySelector('input[name="size"]:checked');
  return hit ? hit.value : "1k";
}

function typedUrl() {
  return $("comfy-url").value.trim();
}

function setState(text) {
  $("save-state").textContent = text;
}

// 低于 1K 的两档是本地专属：接口按面积分档、最低就是 1K（实测请求 0.31M 照样扣 58，出的还是参考图尺寸）。
// 这里按住不给选，后端 clamp_size 还会再收一次（那份表在 tryon/prefs.py 的 LOCAL_ONLY_SIZES），两边说的是同一件事。
const LOCAL_ONLY = ["480p", "720p"];
// 反方向：2K 在本机 ComfyUI 这条按住 —— 同机实测 543 秒，而线上 uwsgi 的 harakiri 是 120 秒，
// 跑到两分钟就被掐，选了只会拿到一个失败。后端 prefs.API_ONLY_SIZES 是同一份规则。
const API_ONLY = ["2k"];
const BAND_LABEL = { "480p": "480p", "720p": "720p", "1k": "1K", "2k": "2K" };

function applySourceLock() {
  const api = chosenSource() === "api";
  const chosen = chosenSize();
  const blocked = api ? LOCAL_ONLY : API_ONLY; // 来源决定按住哪几档
  for (const value of [...LOCAL_ONLY, ...API_ONLY]) {
    const input = document.querySelector(`input[name="size"][value="${value}"]`);
    if (input) input.disabled = blocked.includes(value);
  }
  if (blocked.includes(chosen)) {
    document.querySelector('input[name="size"][value="1k"]').checked = true;
    const why = api ? "只有本机 ComfyUI 有这一档" : "本机 ComfyUI 这条要跑九分钟，不给选";
    setState(`${BAND_LABEL[chosen] || chosen} ${why}，已改回 1K`);
  }
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
    for (const [name, value] of [["source", body.source], ["size", body.size || "2k"]]) {
      const hit = document.querySelector(`input[name="${name}"][value="${value}"]`);
      if (hit) hit.checked = true;
    }
    $("comfy-url").value = body.comfy_url || "";
    $("comfy-url").placeholder = body.default_comfy_url || "";
    setState("已保存");
    applySourceLock(); // 放在后面：它要是改回了 1K，那句提示不该被"已保存"盖掉
  } catch {
    setState("读不到现有设置：确认本机服务还开着");
  }
}

async function save() {
  const payload = { source: chosenSource(), comfy_url: typedUrl(), size: chosenSize() };
  const body = await post("api/settings", payload);
  if (!body.ok) {
    showProblems([], `没保存：${body.message || "未知原因"}`);
    setState("没保存成功");
    return;
  }
  clearProblems();
  dirty = false;
  setState(body.size_note || "已保存"); // 后端把 480p 收成 1K 时，它给的那句原样亮出来
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

for (const radio of document.querySelectorAll('input[name="source"], input[name="size"]')) {
  radio.addEventListener("change", () => {
    markDirty();
    applySourceLock(); // 来源一切到 API，480p 就不该还留在选中状态
  });
}
$("comfy-url").addEventListener("input", markDirty);
$("save").addEventListener("click", save);
$("check").addEventListener("click", checkNow);

load();
