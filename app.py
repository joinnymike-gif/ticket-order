#!/usr/bin/env python3
"""TicketDive 无料チケット 自动申领 → 电子票截图 → 邮件发送。

前端表单收「昵称 + 邮箱」，后台用运营方账号在 ticketdive.com 走完申込流水线，
再把 /ticket/<id> 票面截图寄给用户。单文件、单进程、串行执行。

自检：python3 app.py --selfcheck
空跑：DRY_RUN=1 python3 app.py   # 走到最后一步不点「申し込みを完了する」
"""
import html
import logging
import os
import pathlib
import queue
import re
import secrets
import smtplib
import sqlite3
import sys
import threading
import time
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

BASE = "https://ticketdive.com"
EVENT_SLUG = os.environ.get("EVENT_SLUG", "nico_202608221")
TICKET_NAME = os.environ.get("TICKET_NAME", "無料チケット0円")   # 票种名前缀，用于在活动页定位卡片
ARTIST_ANSWER = os.environ.get("ARTIST_ANSWER", "ニコニコ♡CREAM")  # 「お目当ての出演者」必填项的答案
DB_PATH = os.environ.get("DB_PATH", "/data/jobs.db")
SHOTS = pathlib.Path(os.environ.get("SHOTS_DIR", "/data/tickets"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000").rstrip("/")
PORT = int(os.environ.get("PORT", "8000"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

log = logging.getLogger("ticket")


def env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"缺少环境变量 {name}")
    return v


# ---------------------------------------------------------------- 纯逻辑

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")


def pick_new(before, after):
    """申请前后的票 id 集合做差 —— 定位刚下单的那张票。

    共用账号下票面无法按昵称区分，只能靠差集；因此流水线必须串行（见 WORKER）。
    """
    new = after - before
    if not new:
        return None
    if len(new) > 1:
        raise RuntimeError(f"出现多张新票 {sorted(new)}，无法确定归属，已中止")
    return new.pop()


def claim(db, email, nickname):
    """登记一次申领，返回查看用 token；同一邮箱重复提交返回 None。

    名额是主办方的，别超发。token 是查看票面的唯一凭据 —— 只随邮件发给本人，
    不用邮箱查票：邮箱可猜，等于谁都能调出别人的入场二维码。
    """
    token = secrets.token_urlsafe(16)
    try:
        with db:
            db.execute(
                "INSERT INTO claim(email, nickname, ts, status, token) VALUES (?,?,?,'queued',?)",
                (email.strip().lower(), nickname, time.strftime("%F %T"), token),
            )
        return token
    except sqlite3.IntegrityError:
        return None


def open_db(path):
    db = sqlite3.connect(path, check_same_thread=False)
    db.execute(
        "CREATE TABLE IF NOT EXISTS claim("
        "email TEXT PRIMARY KEY, nickname TEXT, ts TEXT, status TEXT,"
        " ticket_id TEXT, error TEXT, token TEXT UNIQUE)"
    )
    return db


# ---------------------------------------------------------------- 浏览器流水线

def login(page):
    page.goto(f"{BASE}/signin", wait_until="domcontentloaded")
    page.locator("input[type=email]").fill(env("TD_EMAIL"))
    page.locator("input[type=password]").fill(env("TD_PASSWORD"))
    page.get_by_role("button", name="ログインする").click()
    page.wait_for_url(lambda u: "/signin" not in u, timeout=30_000)


def ticket_ids(page):
    """/ticket 列表里的票 id 集合。

    列表按 (applicationId, stageId) 分组，每组渲染一个 <a href="/ticket/{组内第一张票.id}">，
    而每次申込 = 一个新 applicationId = 一个新 <a>，所以集合差能唯一定位本次下的票。
    走 #all：默认视图只显示未来场次，活动日一过就再也 diff 不出来。
    必须整页 goto —— 列表用 React Query 缓存（staleTime:Infinity, refetchOnMount:false），
    客户端路由跳回来拿到的是旧数据。
    """
    page.goto(f"{BASE}/ticket#all", wait_until="networkidle")
    hrefs = page.eval_on_selector_all(
        "a[href^='/ticket/']", "els => els.map(e => e.getAttribute('href'))"
    )
    return {h.split("/")[2] for h in hrefs if h.count("/") >= 2 and h.split("/")[2]}


def fill_labeled(page, label, value):
    """按可见文案填输入框。CSS module 的类名带哈希，每次发版都变，只能锚文案。"""
    try:
        page.get_by_label(label).fill(value)
        return
    except Exception:
        pass
    box = page.get_by_text(label, exact=True).first.locator("xpath=ancestor::*[.//input][1]")
    box.locator("input").first.fill(value)


def apply_free_ticket(page, nickname):
    page.goto(f"{BASE}/event/{EVENT_SLUG}", wait_until="networkidle")
    # 票种卡片 = 含票名文案、且往上第一个带 <select> 的祖先节点
    card = page.get_by_text(TICKET_NAME).first.locator("xpath=ancestor::*[.//select][1]")
    card.locator("select").first.select_option("1")
    page.get_by_role("button", name="申し込みをする").click()
    page.wait_for_url("**/apply", timeout=30_000)

    fill_labeled(page, "ニックネーム", nickname)
    for sel in page.locator("select").all():          # 「お目当ての出演者」必填
        if ARTIST_ANSWER in sel.inner_text():
            sel.select_option(label=ARTIST_ANSWER)
    # ponytail: 不自动勾任何 checkbox —— 页面上可能混着 DM 订阅之类的可选项，
    # 空跑一次确认这页到底需不需要勾选，需要就在这里点名勾。

    if DRY_RUN:
        log.warning("DRY_RUN：停在申込页，未提交。%s", page.url)
        return False
    page.get_by_role("button", name="申し込みを完了する").click()
    page.get_by_text("申込完了").wait_for(timeout=90_000)
    return True


def wait_for_ticket(page, before, tries=15):
    """发券有延迟，轮询到新票出现为止。"""
    for _ in range(tries):
        tid = pick_new(before, ticket_ids(page))
        if tid:
            return tid
        time.sleep(2)
    raise RuntimeError("申込已完成，但 /ticket 未出现新票")


def run_pipeline(nickname, email):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(locale="ja-JP", viewport={"width": 430, "height": 932})
        try:
            login(page)
            before = ticket_ids(page)
            if not apply_free_ticket(page, nickname):
                return None, None
            tid = wait_for_ticket(page, before)
            page.goto(f"{BASE}/ticket/{tid}", wait_until="networkidle")
            page.wait_for_timeout(1500)  # 等二维码画完
            # 绝不点「入場する」—— 站点写明该操作不可撤销
            return tid, page.screenshot(full_page=True)
        finally:
            browser.close()


# ---------------------------------------------------------------- 邮件

def send_mail(to, nickname, tid, png, token):
    m = EmailMessage()
    m["Subject"] = f"【ニコくり周年ライブ0822】無料チケットのお申し込みが完了しました（{nickname} 様）"
    m["From"] = env("MAIL_FROM")
    m["To"] = to
    m.set_content(
        f"{nickname} 様\n\n無料チケットのお申し込みが完了しました。\n"
        f"控えページ: {PUBLIC_URL}/t/{token}\n"
        f"チケット画面: {BASE}/ticket/{tid}\n\n"
        "※控えページのURLはあなた専用です。他人に共有しないでください。\n"
        "※ご入場の際は必ずご自身でチケット画面を開いてご提示ください。\n"
        "　スクリーンショットの提示では入場をお断りする場合があります。\n"
    )
    m.add_attachment(png, maintype="image", subtype="png", filename=f"ticket-{tid}.png")
    with smtplib.SMTP_SSL(env("SMTP_HOST"), int(os.environ.get("SMTP_PORT", "465"))) as s:
        s.login(env("SMTP_USER"), env("SMTP_PASS"))
        s.send_message(m)


# ---------------------------------------------------------------- 队列 + HTTP

JOBS = queue.Queue()


def shot_path(token):
    return SHOTS / f"{token}.png"


def worker(db):
    while True:
        email, nickname, token = JOBS.get()
        try:
            tid, png = run_pipeline(nickname, email)
            if tid:
                shot_path(token).write_bytes(png)     # 落盘后再发信，失败可重发不用重截
                send_mail(email, nickname, tid, png, token)
                status, err = "sent", None
            else:
                status, err = "dry-run", None
        except Exception as e:                       # 失败留痕，允许人工重放
            log.exception("申领失败 %s", email)
            status, err, tid = "failed", str(e)[:500], None
        with db:
            db.execute("UPDATE claim SET status=?, ticket_id=?, error=? WHERE email=?",
                       (status, tid, err, email))
        log.info("%s %s %s", email, status, tid or "")


def resend(db, email):
    """按邮箱重发 —— 结果只进本人信箱，所以不泄露任何东西。"""
    row = db.execute(
        "SELECT nickname, ticket_id, token FROM claim WHERE email=? AND status='sent'",
        (email.strip().lower(),)).fetchone()
    if not row:
        return False
    nickname, tid, token = row
    png = shot_path(token)
    if not png.exists():
        return False
    send_mail(email, nickname, tid, png.read_bytes(), token)
    return True


PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>無料チケット お申し込み</title>
<style>body{font-family:system-ui,sans-serif;max-width:26rem;margin:3rem auto;padding:0 1rem;line-height:1.7}
input,button{width:100%;font-size:1rem;padding:.7rem;margin:.3rem 0 1rem;box-sizing:border-box}
button{background:#5ab9ff;color:#fff;border:0;border-radius:.4rem}small{color:#666}</style>
<h1>無料チケット お申し込み</h1>
<p>ニコくり周年ライブ 0822 ＠日暮里ネコシアター</p>
"""

FORM = """<form method=post action=/apply>
<label>ニックネーム<input name=nickname required maxlength=40></label>
<label>メールアドレス<input name=email type=email required></label>
<button>申し込む</button>
<small>※この無料枠は「荒川区民・女性・学生」限定です。該当する方のみお申し込みください。<br>
※チケットは受付後、数分でメールにてお送りします。</small></form>
<hr><h2>チケットを表示する</h2>
<form action=/t method=get>
<label>メールに記載のチケットコード<input name=token required minlength=16 maxlength=64
 pattern="[A-Za-z0-9_-]+" autocapitalize=off autocorrect=off spellcheck=false></label>
<button>表示する</button></form>
<hr><h2>メールが届かない方</h2>
<form method=post action=/resend>
<label>お申し込み時のメールアドレス<input name=email type=email required></label>
<button>控えを再送する</button>
<small>※控えはご登録のメールアドレスにのみ再送されます。</small></form>"""

# ponytail: token 只做格式校验，不查库就不读盘 —— 挡住 ../ 之类的路径穿越
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

VIEW = """<h2>チケット控え（{nick} 様）</h2>
<p><img src="/t/{token}.png" style="width:100%;border:1px solid #ddd"></p>
<p><a href="{base}/ticket/{tid}">入場用のチケット画面を開く</a></p>
<small>※このURLはあなた専用です。共有しないでください。<br>
※入場時は上のリンクからご自身で開いてご提示ください。画像の提示では入場できません。</small>"""


class Handler(BaseHTTPRequestHandler):
    db = None

    def reply(self, code, body):
        b = (PAGE + body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path == "/":
            return self.reply(200, FORM)
        if path == "/t":                                   # 首页输入框提交过来的
            return self.serve_ticket(parse_qs(qs).get("token", [""])[0])
        if path.startswith("/t/"):                         # 邮件里的直达链接
            return self.serve_ticket(path[3:])
        self.send_error(404)

    def serve_ticket(self, name):
        token, png = (name[:-4], True) if name.endswith(".png") else (name, False)
        if not TOKEN_RE.match(token):
            return self.send_error(404)
        row = self.db.execute(
            "SELECT nickname, ticket_id FROM claim WHERE token=? AND status='sent'",
            (token,)).fetchone()
        if not row or not shot_path(token).exists():
            return self.send_error(404)
        if not png:
            return self.reply(200, VIEW.format(nick=html.escape(row[0]), token=token,
                                               tid=row[1], base=BASE))
        b = shot_path(token).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "private, no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4096:
            return self.send_error(413)
        f = parse_qs(self.rfile.read(n).decode())
        email = (f.get("email", [""])[0]).strip()

        if self.path == "/resend":
            if EMAIL_RE.match(email):
                resend(self.db, email)
            # 无论有没有这条记录都回同一句 —— 否则这个表单就成了邮箱存在性探测器
            return self.reply(200, "<p>ご登録があれば、控えを再送しました。メールをご確認ください。</p>")

        if self.path != "/apply":
            return self.send_error(404)
        nickname = (f.get("nickname", [""])[0]).strip()[:40]
        if not nickname or not EMAIL_RE.match(email):
            return self.reply(400, "<p>入力内容をご確認ください。</p>" + FORM)
        token = claim(self.db, email, nickname)
        if not token:
            return self.reply(409, "<p>このメールアドレスは既にお申し込み済みです。</p>")
        JOBS.put((email.lower(), nickname, token))
        self.reply(202, f"<p>お申し込みを受け付けました、{html.escape(nickname)} 様。<br>"
                        "数分以内にチケット画像をメールでお送りします。</p>")

    def log_message(self, fmt, *a):
        log.info("%s - %s", self.address_string(), fmt % a)


# ---------------------------------------------------------------- 自检 / 入口

def selfcheck():
    assert pick_new({"a"}, {"a", "b"}) == "b"
    assert pick_new({"a"}, {"a"}) is None
    try:
        pick_new(set(), {"a", "b"}); assert False, "多张新票必须报错"
    except RuntimeError:
        pass
    db = open_db(":memory:")
    tok = claim(db, "A@x.com", "太郎")
    assert TOKEN_RE.match(tok)
    assert claim(db, "a@x.com", "次郎") is None, "同一邮箱不得重复占名额"
    assert EMAIL_RE.match("a.b+c@d.co.jp") and not EMAIL_RE.match("a@b")
    for bad in ("../../etc/passwd", "a/b", "short", ""):
        assert not TOKEN_RE.match(bad), bad
    print("selfcheck ok")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "--selfcheck" in sys.argv:
        selfcheck()
        sys.exit()
    for k in ("TD_EMAIL", "TD_PASSWORD", "MAIL_FROM", "SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
        env(k)
    SHOTS.mkdir(parents=True, exist_ok=True)
    Handler.db = open_db(DB_PATH)
    threading.Thread(target=worker, args=(Handler.db,), daemon=True).start()  # 单线程 = 串行
    log.info("listening on :%s  dry_run=%s", PORT, DRY_RUN)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
