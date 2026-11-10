// 设置页：选生成来源、选档位、填 ComfyUI 地址、自检、保存。
// 站内 URL 一律不带前导斜杠 —— 挂在 /mfr/ 这种子路径下才解析得对（设计文档 §6.7）。

const $ = (id) => document.getElementById(id);

// 已存盘的地址：拿它判断"输入框里的和盘上的对不上"，也就是那条「地址没存」
let savedUrl = "";
// 被来源收回那一档的说明（"2K 在本机这条不给选，已改回 1K"）：自动保存紧接着会把状态行改写一次，
// 所以这句话得存着，等保存结果一起说，不然人只看到"已保存"，不知道自己点的那档被换过。
let foldMsg = "";

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

const SOURCE_NAME = { api: "token plan API", comfyui: "本机 ComfyUI", comfyui_llama: "本机 · llama.cpp 编码" };
// 第 3 档换的只是编码那颗节点，DiT/VAE 还在本机那台 ComfyUI 上 —— 所以"按住 2K""不花额度"这些
// 都跟着本机那条走。后端同一份规则在 tryon/prefs.py 的 is_local()，两处必须一起改。
const isLocal = (s) => s === "comfyui" || s === "comfyui_llama";
const SHORT = { saved: "已保存", pending: "地址没存", bad: "没存下来", fold: "已改回 1K" };

// 状态在两处说：屏幕下沿那条常驻保存条说整句，顶栏那颗只说三个字的结论 ——
// 人要点了「← 试衣间」走的时候看的是顶栏，那儿没地方放长句。
function setState(text, kind) {
  $("save-state").textContent = text;
  const chip = $("top-state");
  chip.textContent = SHORT[kind] || text;
  chip.className = `tb-state${kind && kind !== "saved" ? ` ${kind}` : ""}`;
}

// 第 3 档依赖一个常驻的取数服务（systemd 管）。这台没起时把它按住并把话说清：
// 让人点得到一个"ComfyUI 报错"而不知道根因在自己机器上，比少一个选项糟得多。
// 已经存着第 3 档的人不按住（他得能看见自己现在选的是哪条，哪怕正跑不动）。
function markLlama(ready, chosen) {
  const input = document.querySelector('input[name="source"][value="comfyui_llama"]');
  if (!input) return;
  const sub = input.parentElement.querySelector(".sub");
  const base = "不花额度 · 实测 480p 10 秒、720p 19~23 秒、1K 60 秒 · 出的不是同一张图：同构图的另一个细节版本";
  if (ready) {
    input.disabled = false;
    if (sub) sub.textContent = base;
    return;
  }
  input.disabled = !chosen; // 存着它的时候不许按住，否则这一档根本改不回来
  if (sub) sub.textContent = `${base} · 这台机上取数服务（llama-gate）没起，先看 systemctl status llama-gate`;
}

function renderNow() {
  $("sb-now").textContent = `${SOURCE_NAME[chosenSource()] || chosenSource()} · ${BAND_LABEL[chosenSize()] || chosenSize()}`;
}

function urlPending() {
  return typedUrl() !== savedUrl;
}

// 低于 1K 的两档是本地专属：接口按面积分档、最低就是 1K（实测请求 0.31M 照样扣 58，出的还是参考图尺寸）。
// 这里按住不给选，后端 clamp_size 还会再收一次（那份表在 tryon/prefs.py 的 LOCAL_ONLY_SIZES），两边说的是同一件事。
const LOCAL_ONLY = ["480p", "720p"];
// 反方向：2K 在本机 ComfyUI 这条按住 —— 同机实测 543 秒，而线上 uwsgi 的 harakiri 是 120 秒，
// 跑到两分钟就被掐，选了只会拿到一个失败。后端 prefs.API_ONLY_SIZES 是同一份规则。
const API_ONLY = ["2k"];
const BAND_LABEL = { "480p": "480p", "720p": "720p", "1k": "1K", "2k": "2K" };

function applySourceLock() {
  const api = !isLocal(chosenSource()); // 按住哪几档只看"是不是本机那两条"，第 3 档和第 2 档同进同出
  const chosen = chosenSize();
  const blocked = api ? LOCAL_ONLY : API_ONLY; // 来源决定按住哪几档
  for (const value of [...LOCAL_ONLY, ...API_ONLY]) {
    const input = document.querySelector(`input[name="size"][value="${value}"]`);
    if (input) input.disabled = blocked.includes(value);
  }
  if (blocked.includes(chosen)) {
    document.querySelector('input[name="size"][value="1k"]').checked = true;
    const why = api ? "只有本机 ComfyUI 有这一档" : "本机这条要跑五分钟以上，线上两分钟就被掐";
    foldMsg = `${BAND_LABEL[chosen] || chosen} ${why}，已改回 1K`;
    setState(foldMsg, "fold");
  } else {
    foldMsg = "";
  }
  renderNow();
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
    markLlama(body.llama_ready === true, body.source === "comfyui_llama");
    savedUrl = body.comfy_url || "";
    $("comfy-url").value = savedUrl;
    $("comfy-url").placeholder = body.default_comfy_url || "";
    applySourceLock(); // 放在后面：它要是把档位收回了 1K，那句提示不该被"已保存"盖掉
    if (!foldMsg) setState("已保存", "saved");
  } catch {
    setState("读不到现有设置：确认本机服务还开着", "bad");
  }
}

// autosave=true 时送的是**盘上那份地址**，不是输入框里正在敲的：
// 人不该因为顺手敲了半截 URL 就把刚点的来源档位一起赔进去。
async function save(autosave) {
  const payload = { source: chosenSource(), comfy_url: autosave ? savedUrl : typedUrl(), size: chosenSize() };
  const body = await post("api/settings", payload);
  if (!body.ok) {
    showProblems([], `没保存：${body.message || "未知原因"}`);
    setState(autosave && urlPending() ? `来源档位没存下来（${body.message || "未知原因"}），地址先改对` : "没保存成功", "bad");
    foldMsg = "";
    return;
  }
  clearProblems();
  savedUrl = body.comfy_url || "";
  $("comfy-url").value = savedUrl;
  // 后端把 480p 收成 1K 时它给的那句原样亮出来；前端自己收回的那句（foldMsg）比"已保存"更要紧
  const note = body.size_note || foldMsg;
  foldMsg = "";
  if (urlPending()) setState(`设置存了，地址还是没存的那半截（${typedUrl()}）`, "pending");
  else setState(note || "已保存", note ? "fold" : "saved");
}

async function checkNow() {
  setState("自检中…", "pending");
  const body = await post("api/settings/check", { comfy_url: typedUrl() });
  if (body.ok) {
    clearProblems();
    setState("都通", urlPending() ? "pending" : "saved");
    return;
  }
  showProblems(body.problems || [], "自检没通过：");
  setState("自检没通过", "bad");
}

for (const radio of document.querySelectorAll('input[name="source"], input[name="size"]')) {
  radio.addEventListener("change", () => {
    // 来源和档位是离散选择，点完就算定了 —— 改完就地存，不该有"忘了点保存，下次进来还是旧的"这种坏法。
    // 顺序要紧：先让 applySourceLock 把被新来源排除的那档收回 1K，再把收回之后的值存下去。
    applySourceLock();
    save(true);
  });
}
$("comfy-url").addEventListener("input", () => {
  renderNow();
  setState(urlPending() ? "地址改了还没存 · 右边「保存地址」" : "地址和存着的一样", "pending");
});
$("save").addEventListener("click", () => save(false));
$("check").addEventListener("click", checkNow);

renderNow();
load();
