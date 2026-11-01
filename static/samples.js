// sample 图库管理页：树从 api/samples 来，四个写端点（add / mkdir / move / remove）都在这页用。
// 站内 URL 一律不带前导斜杠 —— 挂在 /mfr/ 这种子路径下才解析得对。

const $ = (id) => document.getElementById(id);
let tree = [];

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function state(text, bad) {
  const line = $("state");
  line.textContent = text;
  line.className = bad ? "hint bad" : "hint"; // 空的那行也占位，成功提示跳一下不该把整页顶得动一下
}

async function ask(path, payload, form) {
  const init = form
    ? { method: "POST", body: payload }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) };
  let body;
  try {
    const r = await fetch(path, init);
    body = await r.json();
    if (!r.ok && r.status !== 400) return { ok: false, message: `服务端答了 HTTP ${r.status}` };
  } catch {
    return { ok: false, message: "连不上本机服务" };
  }
  return body.ok ? { ok: true, body } : { ok: false, message: body.message || "没做成" };
}

async function reload() {
  try {
    const r = await fetch("api/samples");
    tree = r.ok ? (await r.json()).groups || [] : [];
  } catch {
    tree = [];
    state("图库读不到：连不上本机服务。", true);
  }
  render();
}

// 一节对外的路径：没有细分类的那节用大类自己的名字当节名，所以它的"架子路径"就是大类本身
const shelfOf = (group, sub) => (sub.name && sub.name !== group.group ? `${group.group}/${sub.name}` : group.group);
const allShelves = () => tree.flatMap((g) => g.subs.map((s) => shelfOf(g, s)));

function countPics() {
  return tree.reduce((n, g) => n + g.subs.reduce((m, s) => m + s.items.length, 0), 0);
}

function render() {
  $("tally").textContent = tree.length
    ? `${tree.length} 个大类 · 共 ${countPics()} 张图`
    : "图库还是空的";
  $("group-names").replaceChildren(...tree.map((g) => el("option", null, g.group)));
  $("tree").replaceChildren(...tree.map(groupBlock));
}

function groupBlock(group) {
  const block = el("section", "lib-group");
  const head = el("div", "lib-head");
  const loose = group.subs.find((s) => s.name === group.group); // 没有细分类时那一节就用大类自己的名字
  const shelves = group.subs.filter((s) => s !== loose);
  const total = group.subs.reduce((n, s) => n + s.items.length, 0); // 整类的张数：写"躺在大类下面那节有几张"会被读成整类
  head.append(el("h2", null, group.group), el("span", "lib-count", total ? `${total} 张` : "还没有图"));
  // 类标题那一行的「加图」存的就是大类自己那一节，所以只有这一节时不再重复一个同名的小标题
  head.append(groupTools(group, uploader(group.group, "")));
  block.append(head);
  if (loose) block.append(gridFor(loose.items, group.group, shelves.length ? "躺在大类下面" : ""));
  for (const sub of shelves) block.append(subBlock(group, sub));
  if (!group.subs.length) block.append(el("p", "hint", "这一类还是空的，先建个细分类或直接往这里加图。"));
  return block;
}

function gridFor(items, shelf, note) {
  const wrap = el("div", "lib-sub");
  if (note) {
    const head = el("div", "lib-sub-head");
    head.append(el("span", "lib-note", note));
    wrap.append(head);
  }
  const grid = el("div", "lib-grid");
  grid.append(...items.map((item) => picture(item, shelf)));
  if (!items.length) grid.append(el("p", "hint", "这一节还没有图。"));
  wrap.append(grid);
  return wrap;
}

function subBlock(group, sub) {
  const shelf = shelfOf(group, sub);
  const wrap = el("div", "lib-sub");
  const head = el("div", "lib-sub-head");
  head.append(el("h3", null, sub.name), el("span", "lib-count", `${sub.items.length} 张`));
  const tools = el("div", "lib-tools");
  tools.append(
    btn("改名", () => startRename(shelf, head, (to) => move(shelf, to))),
    btn("删这一节", () => remove(shelf, `空架子「${shelf}」`)),
    uploader(group.group, sub.name)
  );
  head.append(tools);
  wrap.append(head);
  wrap.append(gridFor(sub.items, shelf));
  return wrap;
}

function picture(item, shelf) {
  const fig = el("figure", "lib-pic");
  const img = el("img");
  img.src = item.thumb;
  img.alt = item.name;
  img.loading = "lazy";
  const cap = el("figcaption", null, item.name);
  const tools = el("div", "lib-pic-tools");
  const mover = el("select");
  mover.append(el("option", null, "移到…"), ...allShelves()
    .filter((s) => s !== shelf)
    .map((s) => el("option", null, s)));
  mover.onchange = () => {
    if (!mover.value) return;
    move(item.path, `${mover.value}/${item.path.split("/").pop()}`);
  };
  tools.append(mover, btn("删", () => remove(item.path, `「${item.path}」`)));
  fig.append(img, cap, tools);
  return fig;
}

function btn(label, click, kind) {
  const b = el("button", kind ? `lib-${kind}` : null, label);
  b.type = "button";
  b.addEventListener("click", click);
  return b;
}

function groupTools(group, upload) {
  const wrap = el("div", "lib-tools");
  wrap.append(
    btn("改名", () => startRename(group.group, wrap, (to) => move(group.group, to.split("/").pop()), true)),
    btn("删这一类", () => remove(group.group, `大类「${group.group}」（含它下面所有空架子）`)),
    upload
  );
  return wrap; // 那颗「加图」也放进这一排：它是 .lib-tools 的孩子才拿得到同一套小字号
}

// 改名不用弹新页面：把那行标题原地换成输入框，改完再换回来
function startRename(current, where, done, isGroup) {
  const input = el("input");
  input.value = isGroup ? current : current.split("/").pop();
  input.placeholder = "新名字";
  const row = el("div", "lib-rename");
  row.append(input, btn("好", () => commit()), btn("算了", () => revert()));
  where.replaceChildren(row);
  input.focus();
  function revert() {
    render(); // 整棵树重画：cloneNode 不带事件，把旧按钮复制回去会得到一堆点不动的按钮
  }
  function commit() {
    const name = input.value.trim();
    if (!name) return state("名字是空的。", true);
    done(isGroup ? name : `${current.split("/").slice(0, -1).join("/")}/${name}`);
  }
}

async function move(from, to) {
  if (from === to) return render();
  const out = await ask("api/samples/move", { from, to });
  if (!out.ok) return state(out.message, true);
  state(`已改成 ${to}`);
  await reload();
}

async function remove(path, what) {
  if (!confirm(`删掉${what}？图库里的图删了就没了（试穿记录里的不受影响）。`)) return;
  const out = await ask("api/samples/remove", { path });
  if (!out.ok) return state(out.message, true);
  state(`已删掉 ${path}`);
  await reload();
}

// 一颗「加图」按钮挂一个隐藏的 file input：多选，选完直接 POST
function uploader(group, sub) {
  const input = el("input");
  input.type = "file";
  input.accept = "image/*";
  input.multiple = true;
  input.hidden = true;
  const button = btn("加图", () => input.click());
  input.onchange = async () => {
    const picked = [...input.files];
    if (!picked.length) return;
    const form = new FormData();
    form.append("group", group);
    if (sub) form.append("sub", sub);
    for (const file of picked) form.append("image", file, file.name);
    state(`存 ${picked.length} 张中…`);
    const out = await ask("api/samples/add", form, true);
    if (!out.ok) return state(out.message, true);
    state(`存进去了 ${out.body.added.length} 张：${out.body.added.join("、")}`);
    await reload();
  };
  const wrap = el("span", "lib-upload");
  wrap.append(button, input);
  return wrap;
}

$("new-room").addEventListener("submit", async (e) => {
  e.preventDefault();
  const group = $("nr-group").value.trim();
  const sub = $("nr-sub").value.trim();
  if (!group) return state("大类名字得写。", true);
  const out = await ask("api/samples/mkdir", { group, sub });
  if (!out.ok) return state(out.message, true);
  state(sub ? `建好了 ${group}/${sub}` : `建好了 ${group}`);
  $("nr-group").value = "";
  $("nr-sub").value = "";
  await reload();
});

reload();
