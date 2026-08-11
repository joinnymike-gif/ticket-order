# 無料チケット 自动申领

前端收「昵称 + 邮箱」→ 后台用运营方账号在 ticketdive.com 走完申込 → 把 `/ticket/<id>` 票面截图寄给用户，
同时留一份在 `./data`，凭邮件里的专属链接 `/t/<token>` 在线查看。

| 路由 | 作用 |
|---|---|
| `GET /` | 申领表单 + 「メールが届かない方」重发表单 |
| `POST /apply` | 登记（邮箱去重）→ 入队 → 202 |
| `POST /resend` | 把控え重发到该邮箱；无论有没有记录都回同一句 |
| `GET /t?token=…` | 首页输入框提交的查询 |
| `GET /t/<token>` | 控え页（截图 + 入场链接），邮件里的直达链接 |
| `GET /t/<token>.png` | 截图本体 |

## 截图为什么不进 git 仓库

评估过「GitHub 图床」方案，否掉了：

- 本仓库是 **public**，截图里的二维码是入场凭证，推上去等于把入场码挂到 `raw.githubusercontent.com`，先扫的先进场
- 邮箱和昵称一旦进 git 历史就删不掉（`git rm` 只动当前树，得改写历史 + 强推）
- VPS 还得揣一个仓库写权限的 token

改成：截图落 `./data/tickets/<token>.png`（已在 `.gitignore` 里），token 是 `secrets.token_urlsafe(16)`，
只随邮件发给本人。**没做「输入邮箱直接看票」** —— 邮箱可猜，那等于谁都能调出别人的入场码；
想找回只能触发重发，结果进本人信箱。

## 部署（VPS）

```bash
git clone <repo> && cd ticket-order
cp .env.example .env && vi .env      # 填账号与 SMTP
docker compose up -d --build
```

`compose.yaml` 只绑 `127.0.0.1:8000`。对外用 Caddy 上 TLS：

```
tickets.example.com { reverse_proxy 127.0.0.1:8000 }
```

## 上线前必做

1. `DRY_RUN=1` 起一次，提交一条测试数据，看日志停在 `/apply` 页且未提交。
2. 到那台机器上 `docker compose exec ticket python3 -c "..."` 或本地 headed 跑一次，确认申込页的
   「ニックネーム」「お目当ての出演者」被正确填上、没有漏勾的必选项。确认后把 `DRY_RUN` 删掉。
3. 真跑一单，核对邮件里的截图确实是新下的那张票。

## 已知边界

- **共用账号**：站点不支持游客下单，所有票挂在 `TD_EMAIL` 这一个账号下。因此靠「申请前后 `/ticket` id 差集」
  定位新票，流水线必须串行（单 worker 线程，勿加并发）。
- **截图不能入场**：活动页写明出示截图可能被拒绝入场。邮件里的图只是控え，入场要本人开 `/ticket/<id>`。
- **限定名额**：该票种限「荒川区民・女性・学生」。本服务只按邮箱去重，不校验资格。
- **选择器锚文案**：站点是 Next.js + CSS Modules，类名带哈希、发版即变，所以全用可见文案定位。
  站点改文案会断，`app.py` 顶部的 `TICKET_NAME` / `ARTIST_ANSWER` 是唯一需要改的地方。
- **绝不点「入場する」**：该操作站点声明不可撤销，代码里没有这个动作，改代码时也别加。

## 自检

```bash
python3 app.py --selfcheck
```
