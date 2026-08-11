const $ = (id) => document.getElementById(id);
const CFG = ["open", "dry_run", "capacity", "event_title", "event_slug", "ticket_name", "artist_answer", "td_email"];
const STAGE_JA = {
  queued: "受付", login: "ログイン中", select: "選択中", apply: "送信中",
  issue: "発券待ち", shot: "取得中", sent: "完了", failed: "失敗", "dry-run": "テスト"
};

async function api(path, body) {
  const r = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
  } : undefined);
  let data = {};
  try { data = await r.json(); } catch (e) { /* 空ボディ */ }
  return { ok: r.ok, code: r.status, data };
}

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---- ログイン

$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const { ok, data } = await api("/api/admin/login", { password: $("pw").value });
  if (!ok) {
    $("loginErr").textContent = data.error || "ログインできません";
    return $("loginErr").classList.remove("hide");
  }
  $("pw").value = "";
  show();
});

$("logout").addEventListener("click", async (e) => {
  e.preventDefault();
  await api("/api/admin/logout", {});
  location.reload();
});

// ---- 設定

async function loadSettings() {
  const { data } = await api("/api/admin/settings");
  CFG.forEach((k) => { if ($(k)) $(k).value = data[k] ?? ""; });
  $("pwSet").textContent = data.td_password_set ? "（設定済み）" : "（未設定）";
}

$("cfgForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {};
  CFG.forEach((k) => { body[k] = $(k).value; });
  body.td_password = $("td_password").value;      // 空なら既存を維持
  const { ok } = await api("/api/admin/settings", body);
  if (!ok) return;
  $("td_password").value = "";
  $("saved").classList.remove("hide");
  setTimeout(() => $("saved").classList.add("hide"), 2500);
  loadSettings();
});

// ---- 一覧

async function loadClaims() {
  const { ok, data } = await api("/api/admin/claims");
  if (!ok) return;
  $("counts").textContent = `使用 ${data.used} 件 / 残り ${data.remaining} 枚`;
  $("rows").innerHTML = data.claims.map((c) => {
    const st = c.status === "queued" ? (c.stage || "queued") : c.status;
    return `<tr>
      <td class="mono">${esc(c.ts)}</td>
      <td>${esc(c.nickname)}</td>
      <td class="mono">${esc(c.email)}</td>
      <td><span class="pill ${esc(st)}">${esc(STAGE_JA[st] || st)}</span></td>
      <td class="mono">${esc(c.ticket_id)}</td>
      <td class="mono" style="color:var(--ng)">${esc(c.error)}</td>
      <td><div class="row">
        <button class="sm ghost" data-act="retry" data-email="${esc(c.email)}">再実行</button>
        <button class="sm ghost" data-act="resend" data-email="${esc(c.email)}">再送</button>
      </div></td></tr>`;
  }).join("");
}

$("rows").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  b.disabled = true;
  await api("/api/admin/" + b.dataset.act, { email: b.dataset.email });
  loadClaims();
});

// ---- 起動

async function show() {
  const { ok } = await api("/api/admin/settings");
  if (!ok) return;                                  // 未ログイン
  $("loginBox").classList.add("hide");
  $("main").classList.remove("hide");
  $("logout").classList.remove("hide");
  loadSettings();
  loadClaims();
  setInterval(loadClaims, 5000);                    // 進捗を追う
}
show();
