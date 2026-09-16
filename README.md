# OneLap 同步控制台

一个完全依托 GitHub 运行的个人同步控制台：网页托管在 GitHub Pages，用户的
OneLap、Garmin 凭据保存在用户自己的私有仓库 Actions Secrets 中，同步任务
由该私有仓库的 GitHub Actions 执行。

控制台不会运营公共后端，也不会把 GitHub Token、账号密码或 Garmin OAuth
会话发送给站点作者。GitHub Token 只保存在当前浏览器标签页的内存中，刷新或
关闭页面后需要重新输入。

## 组成

```text
公共 GitHub Pages
  └─ 浏览器直接调用 GitHub API
       └─ 用户自己的专用私有仓库
            ├─ Actions Secrets：账号、密码、OAuth、Unit ID
            ├─ Actions Variables：设备与同步选项
            ├─ Actions：Garmin 授权、手动/定时同步及核对补同步
            └─ data/sync.db：该用户自己的同步台账
```

GitHub Packages 不承载网页或任务，本项目不依赖 Packages。以后需要提供容器
自部署版本时，可以另外将镜像发布到 GitHub Container Registry。

## 部署控制台

1. 新建一个用于本项目源码的 GitHub 仓库，把本目录提交到 `main` 分支。
2. 推送后，`.github/workflows/pages.yml` 会尝试启用 Pages，测试、构建并部署
   `dist/`。
3. 打开 Actions 中的 Pages 任务，或在仓库 Pages 设置中取得网址。如果组织策略
   禁止工作流自动启用 Pages，再在 `Settings → Pages` 中把 Source 设为
   **GitHub Actions**，然后重新运行部署任务。

本地验证只需要 Node.js 22.12 或更高版本：

```bash
npm ci --ignore-scripts
npm test
npm run check
npm run build
```

生产构建位于 `dist/`。项目使用相对资源路径，可部署到
`https://用户名.github.io/仓库名/` 这类项目 Pages 地址。

## 用户首次使用

1. 在 GitHub 新建一个**专用私有仓库**，建议勾选“Add a README file”。不要选择
   已有代码或资料的仓库；控制台只会向空仓库或仅含 README、LICENSE、
   `.gitignore` 的仓库安装同步程序。
2. 创建 Fine-grained personal access token，只选择这个私有仓库，并授予：
   `Metadata: Read`、`Contents: Read and write`、`Actions: Read and write`、
   `Secrets: Read and write`、`Variables: Read and write`、
   `Workflows: Read and write`。
3. 在控制台连接仓库并安装同步程序。安装使用一次 Git 提交，不强制更新分支，
   仓库在安装期间发生变化会自动停止。
4. 填写 OneLap 和 Garmin 设置。Garmin 中国区和国际区均可在弹窗中输入账号密码
   自动获取 OAuth，也可导入本机会话文件。空白的 Secret 输入框表示保留已保存
   内容，GitHub API 不允许网页读取 Secret 明文。自动获取期间请保持当前标签页
   开启，页面需要在任务结束后取回并保存 OAuth，再清理临时数据。
5. 先运行默认开启的“仅验证”。确认任务成功后，再执行正式上传并单独开启
   自动同步。自动同步默认关闭。

## Garmin 会话

控制台可为中国区或国际区打开 Garmin 登录弹窗。明文账号、密码和 OAuth 只会在
当前页面内存及执行登录的 GitHub Actions runner 进程中短暂处理；浏览器提交前
会使用 GitHub 仓库公钥加密，用户私有仓库配置中仅以加密 Actions Secrets 保存
这些值。专用 `garmin-session.yml` Action 登录 Garmin 后，将 OAuth 用页面生成的
一次性 AES-256-GCM 密钥加密，并发布到只含 `result.json` 的临时结果分支。页面
解密、验证并将 OAuth1/OAuth2 加密保存为正式 Secrets 后，会删除临时 Secrets
和结果分支。明文账号、密码和 OAuth 不会进入 Git 提交、工作流参数或日志。

授权期间必须保持当前标签页开启，由页面持续轮询任务、解密结果、保存正式
Secrets 并执行清理。删除结果分支只是删除 Git 引用，不代表相关 Git 对象会立即
从 GitHub 的存储中物理清除；这些对象中的敏感负载仅为带认证保护的 AES-256-GCM
密文，不含明文 OAuth。

GitHub-hosted runner 无法安全处理 Garmin 的验证码、双因素验证、设备确认或
CAPTCHA。遇到这些情况时，请在可信的本机生成会话，再将文件导入网页：

```bash
cp .env.example .env
# 仅在本地 .env 填写相应区域的 Garmin 用户名和密码
npm ci --ignore-scripts
npm run session:cn
npm run session:global
```

上述命令可以在安装后的私有同步仓库中执行。生成的文件位于
`data/garmin-cn.session.json` 和 `data/garmin-global.session.json`，已被
`.gitignore` 排除。导入网页并保存成功后，应从本机安全删除不再需要的副本。

若 Garmin 触发验证码、双因素验证、429 限流或其他风控，需要先在常用网络环境
完成登录。Garmin 上传依赖非公开个人客户端接口，不是 Garmin 官方 Activity API。

## 安全设计

- 只允许连接私有、未归档且用户可写的仓库；保存设置、触发任务时会再次检查。
- Secret 在浏览器内使用 GitHub 提供的公钥和 LibSodium sealed box 加密后发送。
- 自动获取 Garmin OAuth 使用随机命名的临时 Secrets；返回值额外使用一次性
  AES-256-GCM 密钥加密，并在成功或失败后尽力清理。授权期间应保持标签页开启；
  页面意外关闭时，可重新连接后使用“清理授权临时数据”。
- 安装器只接受构建时生成的固定路径模板，不接受表单提供的任意仓库文件。
- 工作流只接受白名单参数，且只允许私有仓库默认分支运行；第三方 Actions 固定到
  完整提交 SHA，Python Actions 依赖使用带哈希的锁文件。
- 网页不会回显或读取 Secret 明文，断开连接会清除内存中的 Token 和表单敏感值。
- 汇总文件只包含状态计数，不包含活动名称、账号、Token 或错误原文。
- 同步台账包含活动 ID、时间、文件哈希等个人运动元数据，因此目标仓库必须私有。

浏览器扩展、恶意脚本、设备上的其他用户仍可能读取当前页面输入。应使用受信任
设备，令牌只授权一个专用私有仓库，并设置合理的过期时间。完成配置后可撤销
令牌；定时 Actions 不依赖该令牌继续运行。

## 更新同步程序

为避免覆盖用户对私有仓库的修改，首版安装器不会自动升级已经带有
`.onelap-web.json` 标记的仓库。升级前先备份 `data/sync.db`，审阅新模板变化，
再通过 Git 合并或创建新的专用私有仓库。

## 来源和许可

本项目和内置同步引擎快照当前标记为 `UNLICENSED`，用于个人、私有仓库内的
技术验证。同步引擎来源、上游依赖及再分发注意事项见
[`runner/NOTICE.md`](runner/NOTICE.md)。公开发布或向第三方提供服务前，应先完成
上游授权与平台条款核实。
