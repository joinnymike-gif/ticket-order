"""ticketdive.com 申领流水线 + 发信。

站点结构（实测 2026-08-11）：Next.js SSR + Firebase Auth，会话是 HTTP-only cookie。
/event/<slug> 选票 → /event/<slug>/apply 填表 → 申込完了 → /ticket 列表 → /ticket/<id> 票面。
类名带构建哈希（TicketTypeCard_numberSelector__iaKhN 之类），发版即变，所以一律锚可见文案。
"""
import os
import smtplib
import time
from email.message import EmailMessage

BASE = "https://ticketdive.com"
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000").rstrip("/")


def env(name):
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"缺少环境变量 {name}")
    return v


# ---------------------------------------------------------------- 定位新票

def pick_new(before, after):
    """申请前后的票 id 集合做差 —— 定位刚下的那张票。

    /ticket 列表按 (applicationId, stageId) 分组，每组渲染一个 <a href="/ticket/{组内首张.id}">，
    一次申込 = 一个新 applicationId = 一个此前不存在的 id。共用账号下票面长得一样、
    昵称未必印在票上，按内容匹配不可靠，只能按「这次多出来的那张」。
    因此流水线必须串行（app.py 单 worker 线程）。
    """
    new = after - before
    if not new:
        return None
    if len(new) > 1:
        raise RuntimeError(f"出现多张新票 {sorted(new)}，无法确定归属，已中止")
    return new.pop()


# ---------------------------------------------------------------- 各步骤

def login(page, cfg):
    page.goto(f"{BASE}/signin", wait_until="domcontentloaded")
    page.locator("input[type=email]").fill(cfg["td_email"])
    page.locator("input[type=password]").fill(cfg["td_password"])
    page.get_by_role("button", name="ログインする").click()
    page.wait_for_url(lambda u: "/signin" not in u, timeout=30_000)


def ticket_ids(page):
    """/ticket 列表里的票 id 集合。

    走 #all：默认视图只显示未来场次，活动日一过就再也 diff 不出来。
    必须整页 goto —— 列表用 React Query 缓存（staleTime:Infinity, refetchOnMount:false），
    客户端路由跳回来拿到的是旧数据。首屏查询是 startStage>=now 且无 limit，不必翻页。
    """
    page.goto(f"{BASE}/ticket#all", wait_until="networkidle")
    hrefs = page.eval_on_selector_all(
        "a[href^='/ticket/']", "els => els.map(e => e.getAttribute('href'))")
    return {h.split("/")[2] for h in hrefs if h.count("/") >= 2 and h.split("/")[2]}


def fill_labeled(page, label, value):
    try:
        page.get_by_label(label).fill(value)
        return
    except Exception:
        pass
    page.get_by_text(label, exact=True).first \
        .locator("xpath=ancestor::*[.//input][1]").locator("input").first.fill(value)


def apply_free_ticket(page, cfg, nickname):
    page.goto(f"{BASE}/event/{cfg['event_slug']}", wait_until="networkidle")
    # 票种卡片 = 含票名文案、且往上第一个带 <select> 的祖先节点
    card = page.get_by_text(cfg["ticket_name"]).first.locator("xpath=ancestor::*[.//select][1]")
    card.locator("select").first.select_option("1")
    page.get_by_role("button", name="申し込みをする").click()
    page.wait_for_url("**/apply", timeout=30_000)

    fill_labeled(page, "ニックネーム", nickname)
    for sel in page.locator("select").all():            # 「お目当ての出演者」必填
        if cfg["artist_answer"] in sel.inner_text():
            sel.select_option(label=cfg["artist_answer"])
    # ponytail: 不自动勾任何 checkbox —— 可能混着 DM 订阅之类的可选项。
    # 空跑一次确认这页需不需要勾选，需要就在这里点名勾。

    if cfg["dry_run"] == "1":
        return False
    page.get_by_role("button", name="申し込みを完了する").click()
    page.get_by_text("申込完了").wait_for(timeout=90_000)
    return True


def wait_for_ticket(page, before, tries=15):
    for _ in range(tries):
        tid = pick_new(before, ticket_ids(page))
        if tid:
            return tid
        time.sleep(2)
    raise RuntimeError("申込已完成，但 /ticket 未出现新票")


def run(cfg, nickname, on_stage):
    """跑完一单，返回 (ticket_id, png)；空跑返回 (None, None)。"""
    from playwright.sync_api import sync_playwright

    if not cfg["td_email"] or not cfg["td_password"]:
        raise RuntimeError("未配置 TicketDive 账号，请在管理后台填写")

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(locale="ja-JP", viewport={"width": 430, "height": 932})
        try:
            on_stage("login")
            login(page, cfg)
            on_stage("select")
            before = ticket_ids(page)
            on_stage("apply")
            if not apply_free_ticket(page, cfg, nickname):
                return None, None
            on_stage("issue")
            tid = wait_for_ticket(page, before)
            on_stage("shot")
            page.goto(f"{BASE}/ticket/{tid}", wait_until="networkidle")
            page.wait_for_timeout(1500)                # 等二维码画完
            # 绝不点「入場する」—— 站点写明该操作不可撤销
            return tid, page.screenshot(full_page=True)
        finally:
            browser.close()


# ---------------------------------------------------------------- 邮件

def send_mail(cfg, to, nickname, tid, png, token):
    m = EmailMessage()
    m["Subject"] = f"【{cfg['event_title']}】無料チケットのお申し込みが完了しました（{nickname} 様）"
    m["From"] = env("MAIL_FROM")
    m["To"] = to
    m.set_content(
        f"{nickname} 様\n\n無料チケットのお申し込みが完了しました。\n\n"
        f"控えページ: {PUBLIC_URL}/t/{token}\n"
        f"チケットコード: {token}\n"
        f"入場用チケット画面: {BASE}/ticket/{tid}\n\n"
        "※控えページのURLとチケットコードはあなた専用です。他人に共有しないでください。\n"
        "※ご入場の際は必ずご自身でチケット画面を開いてご提示ください。\n"
        "　スクリーンショットの提示では入場をお断りする場合があります。\n")
    m.add_attachment(png, maintype="image", subtype="png", filename=f"ticket-{tid}.png")
    with smtplib.SMTP_SSL(env("SMTP_HOST"), int(os.environ.get("SMTP_PORT", "465"))) as s:
        s.login(env("SMTP_USER"), env("SMTP_PASS"))
        s.send_message(m)
