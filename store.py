"""SQLite 存取。表两张：claim（申领记录）、setting（可在后台改的配置）。"""
import os
import pathlib
import secrets
import sqlite3
import time

DB_PATH = os.environ.get("DB_PATH", "/data/jobs.db")
SHOTS = pathlib.Path(os.environ.get("SHOTS_DIR", "/data/tickets"))

# 后台可改的配置项 -> 首次启动时的默认值（取自环境变量）
DEFAULTS = {
    "open": "1",                                                  # 是否接受申领
    "capacity": os.environ.get("CAPACITY", "50"),                 # 名额上限
    "dry_run": "1",                                               # 空跑：走到最后一步不提交
    "event_title": os.environ.get("EVENT_TITLE", "ニコくり周年ライブ0822＠日暮里ネコシアター"),
    "event_slug": os.environ.get("EVENT_SLUG", "nico_202608221"),
    "ticket_name": os.environ.get("TICKET_NAME", "無料チケット0円"),
    "artist_answer": os.environ.get("ARTIST_ANSWER", "ニコニコ♡CREAM"),
    "td_email": os.environ.get("TD_EMAIL", ""),
    "td_password": os.environ.get("TD_PASSWORD", ""),
}

SECRET_KEYS = {"td_password"}          # 绝不回显、绝不进日志、绝不进 API 响应

STAGES = ["queued", "login", "select", "apply", "issue", "shot", "sent"]


def connect(path=None):
    db = sqlite3.connect(path or DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS claim(
            email TEXT PRIMARY KEY, nickname TEXT, ts TEXT,
            status TEXT, stage TEXT, ticket_id TEXT, error TEXT, token TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS setting(k TEXT PRIMARY KEY, v TEXT);
    """)
    with db:
        for k, v in DEFAULTS.items():
            db.execute("INSERT OR IGNORE INTO setting(k, v) VALUES (?,?)", (k, v))
        db.execute("INSERT OR IGNORE INTO setting(k, v) VALUES ('secret', ?)",
                   (secrets.token_hex(32),))
    # ponytail: 数据库里存着 ticketdive 明文口令。加密密钥只能放在同一台机器上，
    # 那是自欺；真正的防线是 0600 + 卷不外挂 + 后台加密传输。要更强的隔离就上 KMS。
    try:
        os.chmod(path or DB_PATH, 0o600)
    except OSError:
        pass
    return db


def settings(db):
    return {r["k"]: r["v"] for r in db.execute("SELECT k, v FROM setting")}


def public_settings(db):
    """给 API 用：抹掉口令，只留「是否已设置」。"""
    s = settings(db)
    for k in SECRET_KEYS:
        s[k + "_set"] = bool(s.pop(k, ""))
    s.pop("secret", None)
    return s


def save_settings(db, new):
    with db:
        for k, v in new.items():
            if k in DEFAULTS:
                if k in SECRET_KEYS and not v:
                    continue                      # 留空 = 不改动原口令
                db.execute("UPDATE setting SET v=? WHERE k=?", (str(v), k))


def used(db):
    """已占用的名额：失败的不算，可重试。"""
    return db.execute("SELECT count(*) FROM claim WHERE status != 'failed'").fetchone()[0]


def remaining(db):
    return max(0, int(settings(db)["capacity"]) - used(db))


def claim(db, email, nickname):
    """登记一次申领，返回查看用 token；重复邮箱或名额用尽返回 None。"""
    token = secrets.token_urlsafe(16)
    try:
        with db:
            if remaining(db) <= 0:
                return None
            db.execute(
                "INSERT INTO claim(email, nickname, ts, status, stage, token)"
                " VALUES (?,?,?,'queued','queued',?)",
                (email.strip().lower(), nickname, time.strftime("%F %T"), token))
        return token
    except sqlite3.IntegrityError:
        return None


def set_stage(db, email, stage):
    with db:
        db.execute("UPDATE claim SET stage=? WHERE email=?", (stage, email))


def finish(db, email, status, ticket_id=None, error=None):
    with db:
        db.execute("UPDATE claim SET status=?, stage=?, ticket_id=?, error=? WHERE email=?",
                   (status, status, ticket_id, error, email))


def by_token(db, token):
    return db.execute("SELECT * FROM claim WHERE token=?", (token,)).fetchone()


def by_email(db, email):
    return db.execute("SELECT * FROM claim WHERE email=?", (email.strip().lower(),)).fetchone()


def all_claims(db):
    return db.execute("SELECT * FROM claim ORDER BY ts DESC").fetchall()


def shot_path(token):
    return SHOTS / f"{token}.png"
