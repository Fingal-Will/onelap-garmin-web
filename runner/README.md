# OneLap Garmin 私有同步程序

同步区域由 `SYNC_TARGETS` 控制：`GLOBAL` 仅国际区，`CN` 仅中国区，
`CN,GLOBAL` 为双区，`AUTO` 按同时配置 OAuth1/OAuth2 的区域自动判断。
未设置时默认 `AUTO`。私有仓库 Actions Variables 中设置 `SYNC_TARGETS=GLOBAL`
即可长期只同步国际区，不需要配置中国区会话。手动同步与核对默认使用此设置；
手动指定其他区域只影响当次运行。旧台账的中国区记录会保留，但不参与单区重试。

此目录是由网页控制台安装到专用 GitHub 私有仓库的同步程序。它从 OneLap 下载
FIT 活动，分别上传到 Garmin 中国区和国际区，并在私有仓库的 `data/sync.db` 中
保存同步台账。台账包含活动 ID、时间、名称和文件哈希等个人运动元数据，因此运行
仓库必须保持私有。

网页安装会一次性写入此目录中的文件。不要把 `.env`、Garmin OAuth 会话、OneLap
密码、token 或设备 Unit ID 提交到仓库。GitHub Actions 通过 Repository Secrets
保存凭据，并通过 Repository Variables 保存非敏感运行设置。

## GitHub Actions 设置

安装后，在私有仓库的 Actions 设置中允许工作流读取和写入仓库内容。同步工作流会
提交 `data/sync.db` 和私有报告 `data/web-status.json`；它不会提交下载的
FIT 文件、处理后的文件、临时锁或 SQLite WAL 文件。

网页控制台会设置以下 Secrets：

- `ONELAP_ACCOUNT`、`ONELAP_PASSWORD`、`ONELAP_SESSION_KEY`，或临时的
  `ONELAP_TOKEN`
- 各目标区域的 `GARMIN_<REGION>_OAUTH1` 和 `GARMIN_<REGION>_OAUTH2`
- `GARMIN_DEVICE_UNIT_ID`

网页自动获取 Garmin OAuth 时还会短暂创建
`GARMIN_OAUTH_<REQUEST_ID>_{USERNAME,PASSWORD,RESULT_KEY}`。认证结果只以
AES-256-GCM 密文发布到 `oauth-result-<REQUEST_ID>` 临时分支；网页保存正式
OAuth Secrets 后会删除这些临时 Secrets 和分支。若页面意外关闭，可重新连接后
使用“清理授权临时数据”。

非敏感 Variables 包括 `GARMINIZE_FIT_ENABLED`、
`FIT_COORDINATE_TRANSFORM_ENABLED`、`GARMIN_DEVICE_PRODUCT_ID`、
`GARMIN_DEVICE_SOFTWARE_VERSION`、`SYNC_TARGETS`、`SYNC_ENABLED`、
`ONELAP_DISCOVERY_PAGES`、`RECONCILE_RECENT_LIMIT`、`SYNC_MAX_RETRIES`、
`SYNC_DRY_RUN` 和 `STRAVA_SYNC_ENABLED`。

默认发现 10 页、核对最近 20 条、最大重试 8 次。手动任务默认使用已保存的页数和
核对条数，选择具体数值只影响当次任务。`SYNC_DRY_RUN=true` 是全局仅验证开关，
即使手动任务取消“仅验证”，也不会上传。自动同步仍由 `SYNC_ENABLED` 单独控制。

默认情况下定时同步关闭。只有将 `SYNC_ENABLED` 设为 `true` 后，`sync.yml` 才会
按计划运行。三项工作流都会拒绝非私有仓库和非默认分支；工作流输入在写入环境变量
前经过固定白名单验证。账号、密码和 OAuth 只注入实际使用它们的步骤，不会暴露给
依赖安装步骤。

## 在本机生成 Garmin 会话

网页可以通过私有 Action 自动获取 Garmin OAuth。若 Garmin 要求验证码、双因素
验证、设备确认或 CAPTCHA，请在可信的本机克隆此私有仓库，安装 Node.js 22，
然后生成所需区域的会话文件：

```bash
cp .env.example .env
# 只在本机 .env 中填写 GARMIN_CN_USERNAME / GARMIN_CN_PASSWORD，
# 或 GARMIN_GLOBAL_USERNAME / GARMIN_GLOBAL_PASSWORD。
npm ci --ignore-scripts
npm run session:cn
npm run session:global
```

命令分别生成 `data/garmin-cn.session.json` 和
`data/garmin-global.session.json`。把需要的文件导入网页控制台；保存成功后，
可删除本机会话副本。`.env` 和 `*.session.json` 已被 `.gitignore` 排除，仍不要
运行会把忽略文件强制加入 Git 的命令，也不要在聊天、Issue 或截图中公开内容。
如果 Garmin 返回 429、验证码或其他风控，应暂停重试并在常用网络环境处理。

## 本地检查

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes --requirement requirements.lock
npm ci --ignore-scripts
python -m unittest discover -s tests
npm run check
```

复制 `.env.example` 为 `.env` 后，可用 `set -a; source .env; set +a` 导入本地配置。
先用 `SYNC_DRY_RUN=true python -m onelap_garmin_sync sync` 验证下载和 FIT 处理。
运行时行为还会受 OneLap 和 Garmin 的非公开接口、服务条款和风控策略影响；详细来源
和许可限制见 [NOTICE.md](NOTICE.md)。

## Strava 同步（可选）

Strava 默认关闭，Garmin 中国区和国际区仍可以独立选择。开启时额外保存
`STRAVA_CLIENT_ID`、`STRAVA_CLIENT_SECRET`、`STRAVA_REFRESH_TOKEN`、
`STRAVA_SESSION_KEY` 四项 Secrets。首次开启时网页自动生成 Strava 会话加密密钥，
以后留空会保留原值。动态刷新后的授权加密存入现有台账，不保存明文 token。

在可信的本机副本中安装 Python 依赖后，执行：

```bash
python -m onelap_garmin_sync.strava_auth
```

跟随提示完成 `activity:read_all,activity:write` 授权，把输出的值填入网页中的
“Strava 同步（可选）”表单。该命令是本机交互工具，不在 Actions 中执行。
重新授权只替换 Refresh Token，保留原 `STRAVA_SESSION_KEY` 和 `data/sync.db`。

同步任务先批量查询 Strava 活动，用时间、距离、时长判断已有活动。已存在则跳过，
确认缺失才上传；匹配有歧义、列表读取不完整或授权不足时暂缓 Strava，Garmin
继续执行。上传中的任务会在下次查询结果，避免重复提交。指定活动“强制重新上传”
会跳过 Strava 预核对，请在明确需要时使用。“核对并补同步”只处理 Garmin，
Strava 的核对在普通同步任务中完成。网页状态汇总包含 Strava 的上传计数。

## 升级已有私有仓库

网页安装入口仍只支持首次安装。更新公共网页不会自动改动已有私有运行仓库。
升级前暂停自动同步并等待已有任务结束，备份 `data/sync.db`；在私有仓库默认分支
审阅并更新本模板的 `onelap_garmin_sync/`、`uploader/`、`scripts/`、`tests/`、
`.github/workflows/`、依赖清单与锁文件、`.env.example`、`.gitignore`、
`.onelap-web.json` 及本文说明。只复制模板里的源码文件，保留私有仓库原有 `data/`、
Secrets 和 Variables。不要用本项目或其他用户的台账替换自己的台账。

提交后重新连接网页，检查安装版本为 `1.3.1`，保存运行设置并执行一次“仅验证”，
确认成功后再恢复原来的自动同步设置。旧版功能标记缺失时，网页会拒绝保存新运行
选项或开启 Strava，避免设置被静默忽略；原有凭据设置仍可保存。

## 快照来源

此安装包的共享引擎文件来自 `Fingal-Will/onelap-garmin-dual-sync` 的
`df8ce91cc61ebbc42936214953fd6b09e9da7317` 已提交快照。Web 保留独立的私有仓库
校验、加密凭据保存、网页请求处理和状态导出。安装包版本与功能标记由
`.onelap-web.json` 标识；本版为 `1.3.1`。


## 私有网页活动报告

1.3.0 导出 schema 2：累计活动与目标计数，以及最近 200 条活动的编号、名称、时间、
下载/处理状态、原始文件 SHA-256 和逐目标上传/核对证据。明细按活动时间降序排列。
账号、密码、Token、Unit ID、原始来源响应、文件路径和错误原文均不进入报告；
此报告包含个人运动元数据，随台账仅保存在用户专用私有仓库。

网页对 schema 1 降级展示聚合计数；旧表缺少明细所需列时不生成虚构记录。未能读取
台账时 `ledger.available=false`，网页显示未知数量。报告中的运行状态在保存步骤前
写入，因此网页另查 Actions 的“保存台账与汇总”步骤判定持久化，不能用该字段或
整个工作流成功与否代替台账保存结果。保存失败时先查看恢复附件并核对远端。


## 2026-10-29 最新骑行发现修复（1.3.1）

读取 OTM 活动列表前，运行器会先调用 `/api/otm/ride_record/capability_list`，
将顽鹿 App 的最新骑行导入 OTM，再读取列表。仅查询列表可能遗漏尚未导入的活动。
每次运行只在首次读取前导入，日志显示候选、新增、跳过及失败计数；导入失败会
明确报错，避免把失败当作“没有新活动”。同时兼容列表的嵌套分页字段。

账号 UID 从登录响应获取，与访问令牌一起加密保存在现有台账中。旧会话首次升级后
会用已有账号密码重新登录一次，之后复用保存的 UID，无需新增 Secrets。仅配置
手工 Token、且没有账号密码及已缓存 UID 时，日志会提示无法导入最新骑行，
该次仅查询 OTM 已有记录。

已有私有运行仓库需要按上文升级步骤更新到 1.3.1。升级后的首次运行可检查
“OneLap OTM 导入”计数，再确认活动列表及网页报告；公共网页更新本身不会升级
已有私有仓库的代码。
