# 無料チケット 自动申领

用户在网页填「昵称 + 邮箱」→ 后台用运营方账号在 ticketdive.com 走完申込 →
截取 `/ticket/<id>` 票面发邮件，并留一份在 `./data`，凭专属 token 在线查看。

```
static/index.html   受付页：申领表单、实时进度、凭 token 查票、重发
static/ticket.html  控え页：票面截图 + 入场链接
static/admin.html   管理后台：设置、TicketDive 账号、名额开关、申込一览
app.py              HTTP 服务 + 路由 + 单线程 worker
pipeline.py         Playwright 流水线 + 发信
store.py            SQLite（claim / setting 两张表）
```

零第三方依赖 —— 只用标准库，Playwright 已在基础镜像里。

## 部署

```bash
cp .env.example .env && vi .env      # 至少填 ADMIN_PASSWORD / PUBLIC_URL / SMTP
docker compose up -d --build
```

只绑 `127.0.0.1:8000`，对外用 Caddy 上 TLS：

```
tickets.example.com { reverse_proxy 127.0.0.1:8000 }
```

然后开 `https://tickets.example.com/admin`，填 TicketDive 账号密码，其余配置也在这里改，改完即时生效不用重启。

## 上线前必做

1. 默认是**テストモード**（`dry_run=1`）：走到申込页填完表单但不点「申し込みを完了する」。
   先在这个模式下跑一单，确认「ニックネーム」「お目当ての出演者」被正确填上、没有漏勾的必选项。
2. 确认后在管理后台切到「本番」，真跑一单，核对邮件里的截图确实是新下的那张票。
3. **注意：テストモード不拦登录。** 它只拦最后一步提交，登录动作照常发往 ticketdive.com，
   所以别拿假账号密码反复测 —— 那是在对真站点刷失败登录。

## 路由

| 路由 | 作用 |
|---|---|
| `GET /` | 受付页 |
| `GET /api/config` | 活动名、是否受付中、剩余名额 |
| `POST /api/apply` | 登记（邮箱去重 + 名额校验）→ 入队 → 返回 token |
| `GET /api/status?token=` | 进度轮询：queued→login→select→apply→issue→shot→sent |
| `POST /api/resend` | 重发控え到该邮箱；有无记录都回同一句 |
| `GET /t/<token>` | 控え页 |
| `GET /t/<token>.png` | 截图本体 |
| `GET /admin` | 管理后台 |
| `POST /api/admin/login`｜`logout` | 管理登录（`ADMIN_PASSWORD`，HMAC 签名 cookie） |
| `GET`｜`POST /api/admin/settings` | 读写配置（口令只写不读） |
| `GET /api/admin/claims` | 申込一览 |
| `POST /api/admin/retry`｜`resend` | 手工重试 / 重发 |

## 已知边界

- **共用账号**：站点不支持游客下单，所有票挂在后台配置的那一个账号下。因此靠「申请前后
  `/ticket` id 集合差」定位新票，流水线必须串行（单 worker 线程，勿加并发）。
- **截图不能入场**：活动页写明出示截图可能被拒绝入场。邮件和控え页里的图只是控え，
  入场要本人开 `/ticket/<id>`。
- **限定名额**：该票种限「荒川区民・女性・学生」。本服务只按邮箱去重，不校验资格。
- **选择器锚文案**：站点是 Next.js + CSS Modules，类名带哈希、发版即变，所以全用可见文案定位。
  站点改文案会断，改管理后台里的「券種名」「出演者の回答」即可，不用改代码。
- **口令明文存库**：`data/jobs.db` 里存着 TicketDive 密码，文件权限 0600。
  加密密钥只能放同一台机器上，那是自欺 —— 真正的防线是别把 `data/` 卷外挂、别把库随手拷走。
- **失败不自动重试**：失败留在库里标 `failed` 并释放名额，管理后台手工「再実行」。

## 截图为什么不进 git 仓库

评估过「GitHub 图床」方案，否掉了：

- 本仓库是 public，截图里的二维码是入场凭证，推上去等于把入场码挂到 `raw.githubusercontent.com`
- 邮箱和昵称一旦进 git 历史就删不掉
- 私有仓库也不行：`raw.githubusercontent.com` 取私有文件要短时效签名 token，
  拿不到稳定 URL；走 Pages 则需 Pro 以上，且**发布出来的站点仍是公开的**
  （要站点也私有需 Enterprise Cloud）

改成：截图落 `./data/tickets/<token>.png`（已在 `.gitignore` 里），token 是
`secrets.token_urlsafe(16)`，只随邮件发给本人。**没做「输入邮箱直接看票」** ——
邮箱可猜，那等于谁都能调出别人的入场码；想找回只能触发重发，结果进本人信箱。

## 自检

```bash
python3 app.py --selfcheck
```

覆盖：新票差集定位、名额上限与释放、邮箱去重、口令留空不清、口令不进 API 响应、token 格式与路径穿越。
