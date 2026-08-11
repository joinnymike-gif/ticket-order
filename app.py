#!/usr/bin/env python3
"""无料チケット 自动申领：HTTP 服务 + 单线程 worker。

前端 static/ 直出，API 走 JSON。后台可配 TicketDive 账号、名额、开关。
串行执行 —— 定位新票靠申请前后集合差，并发会认错人（见 pipeline.pick_new）。

自检：python3 app.py --selfcheck
"""
import hmac
import json
import logging
import mimetypes
import os
import queue
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pipeline
import store

PORT = int(os.environ.get("PORT", "8000"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")     # 挡住 ../ 之类的路径穿越

log = logging.getLogger("ticket")
JOBS = queue.Queue()


# ---------------------------------------------------------------- worker

def worker(db):
    while True:
        email = JOBS.get()
        row = store.by_email(db, email)
        if not row:
            continue
        cfg, token, nickname = store.settings(db), row["token"], row["nickname"]
        try:
            tid, png = pipeline.run(cfg, nickname, lambda s: store.set_stage(db, email, s))
            if tid:
                store.shot_path(token).write_bytes(png)   # 先落盘再发信，重发不用重截
                pipeline.send_mail(cfg, email, nickname, tid, png, token)
                store.finish(db, email, "sent", tid)
            else:
                store.finish(db, email, "dry-run")
        except Exception as e:                            # 失败留痕，后台可手工重试
            log.exception("申领失败 %s", email)
            store.finish(db, email, "failed", error=str(e)[:500])
        log.info("job done %s -> %s", email, store.by_email(db, email)["status"])


def enqueue(email):
    JOBS.put(email)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    db = None
    server_version = "ticket/1.0"

    # ---- 基础

    def json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def blob(self, code, body, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def static(self, name):
        path = os.path.normpath(os.path.join(STATIC, name))
        if not path.startswith(STATIC) or not os.path.isfile(path):
            return self.send_error(404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            self.blob(200, f.read(), ctype + ("; charset=utf-8" if "text" in ctype
                                              or "javascript" in ctype else ""))

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 8192:
            raise ValueError("payload too large")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    # ---- 管理端鉴权

    def admin_ok(self):
        want = hmac.new(store.settings(self.db)["secret"].encode(), b"admin", "sha256").hexdigest()
        got = ""
        for c in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = c.strip().partition("=")
            if k == "admin":
                got = v
        return hmac.compare_digest(got, want)

    def need_admin(self):
        if self.admin_ok():
            return True
        self.json(401, {"error": "unauthorized"})
        return False

    # ---- 路由

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)

        if p == "/":
            return self.static("index.html")
        if p == "/admin":
            return self.static("admin.html")
        if p.startswith("/static/"):
            return self.static(p[len("/static/"):])

        if p == "/t":                                     # 首页输入框提交过来
            return self.redirect_token(q.get("token", [""])[0])
        if p.startswith("/t/"):
            name = p[3:]
            if name.endswith(".png"):
                return self.serve_png(name[:-4])
            return self.static("ticket.html")             # 票面数据由 JS 走 API 取

        if p == "/api/config":
            s = store.public_settings(self.db)
            return self.json(200, {"event_title": s["event_title"],
                                   "open": s["open"] == "1" and store.remaining(self.db) > 0,
                                   "remaining": store.remaining(self.db),
                                   "dry_run": s["dry_run"] == "1"})
        if p == "/api/status":
            return self.status(q.get("token", [""])[0])

        if p == "/api/admin/claims":
            if not self.need_admin():
                return
            return self.json(200, {"claims": [dict(r) for r in store.all_claims(self.db)],
                                   "used": store.used(self.db),
                                   "remaining": store.remaining(self.db)})
        if p == "/api/admin/settings":
            if not self.need_admin():
                return
            return self.json(200, store.public_settings(self.db))
        self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            data = self.body()
        except ValueError:
            return self.send_error(413)

        if p == "/api/apply":
            return self.apply(data)
        if p == "/api/resend":
            return self.resend(data)

        if p == "/api/admin/login":
            return self.login(data)
        if p == "/api/admin/logout":
            self.send_response(200)
            self.send_header("Set-Cookie", "admin=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
            self.send_header("Content-Length", "0")
            return self.end_headers()

        if not p.startswith("/api/admin/"):
            return self.send_error(404)
        if not self.need_admin():
            return
        if p == "/api/admin/settings":
            store.save_settings(self.db, data)
            return self.json(200, store.public_settings(self.db))
        if p == "/api/admin/retry":
            row = store.by_email(self.db, data.get("email", ""))
            if not row:
                return self.json(404, {"error": "not found"})
            store.finish(self.db, row["email"], "queued")
            enqueue(row["email"])
            return self.json(200, {"ok": True})
        if p == "/api/admin/resend":
            ok = self.do_resend(data.get("email", ""))
            return self.json(200 if ok else 404, {"ok": ok})
        self.send_error(404)

    # ---- 处理器

    def apply(self, data):
        nickname = str(data.get("nickname", "")).strip()[:40]
        email = str(data.get("email", "")).strip()
        s = store.settings(self.db)
        if s["open"] != "1":
            return self.json(403, {"error": "受付は終了しました"})
        if not nickname or not EMAIL_RE.match(email):
            return self.json(400, {"error": "入力内容をご確認ください"})
        if store.by_email(self.db, email):
            return self.json(409, {"error": "このメールアドレスは既にお申し込み済みです"})
        token = store.claim(self.db, email, nickname)
        if not token:
            return self.json(409, {"error": "満席のため受付を終了しました"})
        enqueue(email.lower())
        return self.json(202, {"token": token})

    def status(self, token):
        if not TOKEN_RE.match(token or ""):
            return self.json(404, {"error": "not found"})
        row = store.by_token(self.db, token)
        if not row:
            return self.json(404, {"error": "not found"})
        return self.json(200, {"nickname": row["nickname"], "status": row["status"],
                               "stage": row["stage"], "ticket_id": row["ticket_id"],
                               "error": row["error"],
                               "shot": store.shot_path(token).exists()})

    def redirect_token(self, token):
        if not TOKEN_RE.match(token or ""):
            return self.send_error(404)
        self.send_response(303)
        self.send_header("Location", f"/t/{token}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def serve_png(self, token):
        if not TOKEN_RE.match(token) or not store.by_token(self.db, token):
            return self.send_error(404)
        f = store.shot_path(token)
        if not f.exists():
            return self.send_error(404)
        self.blob(200, f.read_bytes(), "image/png", cache="private, no-store")

    def do_resend(self, email):
        row = store.by_email(self.db, email) if EMAIL_RE.match(email or "") else None
        if not row or row["status"] != "sent":
            return False
        f = store.shot_path(row["token"])
        if not f.exists():
            return False
        pipeline.send_mail(store.settings(self.db), row["email"], row["nickname"],
                           row["ticket_id"], f.read_bytes(), row["token"])
        return True

    def resend(self, data):
        try:
            self.do_resend(str(data.get("email", "")).strip())
        except Exception:
            log.exception("重发失败")
        # 有没有这条记录都回同一句 —— 否则这个接口就成了邮箱存在性探测器
        self.json(200, {"ok": True})

    def login(self, data):
        want = os.environ.get("ADMIN_PASSWORD", "")
        if not want or not hmac.compare_digest(str(data.get("password", "")), want):
            return self.json(401, {"error": "パスワードが違います"})
        tok = hmac.new(store.settings(self.db)["secret"].encode(), b"admin", "sha256").hexdigest()
        self.send_response(200)
        self.send_header("Set-Cookie",
                         f"admin={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *a):
        log.info("%s %s", self.address_string(), fmt % a)


# ---------------------------------------------------------------- 自检 / 入口

def selfcheck():
    assert pipeline.pick_new({"a"}, {"a", "b"}) == "b"
    assert pipeline.pick_new({"a"}, {"a"}) is None
    try:
        pipeline.pick_new(set(), {"a", "b"}); assert False, "多张新票必须报错"
    except RuntimeError:
        pass
    db = store.connect(":memory:")
    store.save_settings(db, {"capacity": "2", "td_password": "p1"})
    assert store.settings(db)["td_password"] == "p1"
    store.save_settings(db, {"td_password": ""})
    assert store.settings(db)["td_password"] == "p1", "留空不得清掉已存口令"
    assert "td_password" not in store.public_settings(db), "口令不得出现在 API 响应里"
    assert store.public_settings(db)["td_password_set"] is True
    tok = store.claim(db, "A@x.com", "太郎")
    assert TOKEN_RE.match(tok)
    assert store.claim(db, "a@x.com", "次郎") is None, "同一邮箱不得重复占名额"
    assert store.claim(db, "b@x.com", "花子") and store.remaining(db) == 0
    assert store.claim(db, "c@x.com", "三郎") is None, "名额用尽必须拒绝"
    store.finish(db, "b@x.com", "failed", error="boom")
    assert store.remaining(db) == 1, "失败的应释放名额"
    assert EMAIL_RE.match("a.b+c@d.co.jp") and not EMAIL_RE.match("a@b")
    for bad in ("../../etc/passwd", "a/b", "short", ""):
        assert not TOKEN_RE.match(bad), bad
    print("selfcheck ok")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if "--selfcheck" in sys.argv:
        selfcheck()
        sys.exit()
    if not os.environ.get("ADMIN_PASSWORD"):
        sys.exit("缺少环境变量 ADMIN_PASSWORD")
    store.SHOTS.mkdir(parents=True, exist_ok=True)
    Handler.db = store.connect()
    threading.Thread(target=worker, args=(Handler.db,), daemon=True).start()
    log.info("listening on :%s", PORT)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
