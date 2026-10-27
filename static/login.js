// 登录页：一个口令 → api/login → 拿到 cookie 就回主页。
// 站内 URL 一律不带前导斜杠 —— 挂在 /mfr/ 这种子路径下才解析得对（设计文档 §6.7）。

const $ = (id) => document.getElementById(id);

function say(text, bad) {
  const msg = $("msg");
  msg.textContent = text;
  msg.classList.toggle("bad", Boolean(bad));
}

async function sendPw(pw) {
  // 只收 JSON：跨站表单发不出 application/json，服务端那条登录路由顺带就有了 CSRF 兜底
  const r = await fetch("api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: pw }),
  });
  try {
    return await r.json();
  } catch {
    return { ok: false, message: `服务返回读不懂（HTTP ${r.status}）` };
  }
}

async function submit() {
  const pw = $("pw").value;
  if (!pw.trim()) {
    say("先输口令。", true);
    $("pw").focus();
    return;
  }
  $("go").disabled = true;
  say("正在核对…");
  let body;
  try {
    body = await sendPw(pw);
  } catch {
    body = { ok: false, message: "连不上本机服务。" };
  }
  $("go").disabled = false;
  if (body.ok) {
    window.location.replace("."); // 回挂载点根目录；用 replace 免得返回键又弹回登录页
    return;
  }
  say(body.message || "口令不对。", true);
  $("pw").select();
}

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  submit();
});
$("pw").focus();
