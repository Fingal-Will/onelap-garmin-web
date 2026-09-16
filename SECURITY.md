# 安全说明

请不要在 Issue、日志、截图或提交中公开以下内容：GitHub Token、OneLap 密码或
Token、Garmin OAuth 会话、Garmin Unit ID、`.env`、会话 JSON、SQLite 台账。

发现控制台可能泄露凭据时，应先撤销受影响的 GitHub Token、更新相应平台凭据，
然后通过私下渠道向仓库维护者报告。报告中使用已脱敏的请求和响应，不要附带
真实 Secret。

控制台没有站点后端。敏感配置从浏览器直接加密并提交到用户指定的 GitHub 私有
仓库，实际同步在该仓库 Actions 中运行。GitHub Pages 仓库、第三方浏览器扩展和
用户设备仍属于需要信任的执行环境。

自动获取 Garmin OAuth 时，明文账号、密码和 OAuth 只会在当前页面内存及执行
登录的 GitHub Actions runner 进程中短暂处理。浏览器使用仓库公钥加密后才向
GitHub 提交，用户私有仓库配置中仅以加密 Actions Secrets 保存这些值；返回
分支中的敏感负载仅为带认证保护的 AES-256-GCM 密文。

授权期间应保持当前标签页开启，由页面轮询结果、保存正式 Secrets 并执行清理。
正常完成后页面会删除临时 Secrets 和结果分支，但删除分支只是删除 Git 引用，
不代表相关 Git 对象会立即从 GitHub 的存储中物理清除。若浏览器崩溃、断网或被
强制关闭，清理可能无法完成；重新连接后应使用“清理授权临时数据”，并在私有
仓库的 Actions Secrets 与分支列表中复核。
