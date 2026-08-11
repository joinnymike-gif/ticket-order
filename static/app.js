const $ = (id) => document.getElementById(id);
const STEPS = ["queued", "login", "select", "apply", "issue", "shot", "sent"];
const POLL_MS = 2000;

async function api(path, body) {
  const r = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
  } : undefined);
  let data = {};
  try { data = await r.json(); } catch (e) { /* 204 など */ }
  return { ok: r.ok, code: r.status, data };
}

// ---- 初期表示

async function loadConfig() {
  const { data } = await api("/api/config");
  $("title").textContent = data.event_title || "無料チケット";
  $("sub").textContent = data.open ? `残り ${data.remaining} 枚` : "";
  if (data.dry_run) $("dry").classList.remove("hide");
  if (!data.open) {
    $("closed").classList.remove("hide");
    $("applyCard").classList.add("hide");
  }
}

// ---- 進捗

function paint(stage) {
  const i = STEPS.indexOf(stage);
  document.querySelectorAll("#steps li").forEach((li, n) => {
    li.classList.toggle("done", i >= 0 && n < i);
    li.classList.toggle("now", n === i);
  });
}

async function poll(token) {
  const { ok, data } = await api("/api/status?token=" + encodeURIComponent(token));
  if (!ok) return setTimeout(() => poll(token), POLL_MS);

  if (data.status === "sent") {
    paint("sent");
    document.querySelectorAll("#steps li").forEach((li) => li.classList.add("done"));
    $("progressCard").classList.add("hide");
    $("doneMsg").innerHTML =
      `お申し込みが完了しました。メールをご確認ください。<br>` +
      `<a href="/t/${encodeURIComponent(token)}">チケットを表示する</a>`;
    $("doneMsg").classList.remove("hide");
    return;
  }
  if (data.status === "failed") {
    $("progressCard").classList.add("hide");
    $("failMsg").textContent = "お申し込みに失敗しました。お手数ですが主催者までお問い合わせください。";
    $("failMsg").classList.remove("hide");
    return;
  }
  if (data.status === "dry-run") {
    $("progressCard").classList.add("hide");
    $("failMsg").className = "msg warn";
    $("failMsg").textContent = "テストモードのため、実際の申し込みは行われませんでした。";
    $("failMsg").classList.remove("hide");
    return;
  }
  paint(data.stage);
  setTimeout(() => poll(token), POLL_MS);
}

function startProgress(token) {
  localStorage.setItem("ticketToken", token);
  $("applyCard").classList.add("hide");
  $("progressCard").classList.remove("hide");
  paint("queued");
  poll(token);
}

// ---- フォーム

$("applyForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("applyBtn"), err = $("applyErr");
  btn.disabled = true; btn.textContent = "送信中…"; err.classList.add("hide");
  const { ok, data } = await api("/api/apply", {
    nickname: $("nickname").value.trim(), email: $("email").value.trim()
  });
  if (ok) return startProgress(data.token);
  err.textContent = data.error || "エラーが発生しました";
  err.classList.remove("hide");
  btn.disabled = false; btn.textContent = "申し込む";
});

$("tokenForm").addEventListener("submit", (e) => {
  e.preventDefault();
  location.href = "/t/" + encodeURIComponent($("token").value.trim());
});

$("resendForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  await api("/api/resend", { email: $("remail").value.trim() });
  $("resendOk").classList.remove("hide");
  $("resendForm").reset();
});

loadConfig();
// リロードしても進捗を追える
const saved = localStorage.getItem("ticketToken");
if (saved) {
  api("/api/status?token=" + encodeURIComponent(saved)).then(({ ok, data }) => {
    if (ok && !["sent", "failed", "dry-run"].includes(data.status)) startProgress(saved);
  });
}
