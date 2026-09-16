# OneLap Garmin 私有同步程序

此目录是由网页控制台安装到专用 GitHub 私有仓库的同步程序。它从 OneLap 下载
FIT 活动，分别上传到 Garmin 中国区和国际区，并在私有仓库的 `data/sync.db` 中
保存同步台账。台账包含活动 ID、时间、名称和文件哈希等个人运动元数据，因此运行
仓库必须保持私有。

网页安装会一次性写入此目录中的文件。不要把 `.env`、Garmin OAuth 会话、OneLap
密码、token 或设备 Unit ID 提交到仓库。GitHub Actions 通过 Repository Secrets
保存凭据，并通过 Repository Variables 保存非敏感运行设置。

## GitHub Actions 设置

安装后，在私有仓库的 Actions 设置中允许工作流读取和写入仓库内容。同步工作流会
提交 `data/sync.db` 和仅含聚合数量的 `data/web-status.json`；它不会提交下载的
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
`GARMIN_DEVICE_SOFTWARE_VERSION`、`SYNC_TARGETS` 和 `SYNC_ENABLED`。

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

## 快照来源

此安装包的引擎快照来自 `Fingal-Will/onelap-garmin-dual-sync` 的
`002fff7c096884472ee0eb4601b49f5105787515`，并包含生成模板时该工作区中的未提交
引擎修改。精确的安装包版本由 `.onelap-web.json` 中的 `version` 标识。
