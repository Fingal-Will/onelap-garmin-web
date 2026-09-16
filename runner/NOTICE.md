# 来源与许可说明

本项目目前用于个人、私有仓库内的技术验证，不作为一个已获完整再分发授权的开源项目发布，因此 `package.json` 标记为 `UNLICENSED`。

实现过程中参考了以下项目的公开代码、接口行为和工程思路：

- `moruoxian/SyncOnelapToXoss`
  - https://github.com/moruoxian/SyncOnelapToXoss
  - 参考 OneLap 活动列表签名、活动详情和 FIT 下载接口行为。
  - 截至本项目创建时，所检查的仓库快照中未发现明确的 LICENSE 文件。
- `Fingal-Will/dailysync-rev`
  - https://github.com/Fingal-Will/dailysync-rev
  - 参考 Garmin 中国区/国际区客户端选择、OAuth 会话加载和 FIT 上传思路。
  - 该项目声明为 GPL-3.0。
- `@gooin/garmin-connect`
  - https://github.com/Pythe1337N/garmin-connect
  - 本项目通过 npm 依赖调用，依赖本身声明为 MIT。
- `jat255/Fit-File-Faker`
  - https://github.com/jat255/Fit-File-Faker
  - 本项目通过 PyPI 固定使用 `fit-file-faker==2.2.1`，用于重写 FIT
    中的 Garmin manufacturer、product、Unit ID 和固件元数据。
  - Fit File Faker 声明为 MIT；其内置的 `fit_tool` 代码使用 BSD
    风格许可证，具体条款以对应发布包内文件为准。

SQLite 台账、双目标状态机、重试策略、命令行编排、GCJ-02 到 WGS84
坐标转换和 GitHub Actions 工作流在本项目中重新组织实现。坐标纠正与
Garmin 设备元数据重写属于同一个活动级 FIT 预处理步骤；每条活动只生成
一个处理文件，并由 Garmin 中国区和国际区共同使用。

Garmin 设备伪装属于非官方兼容性实验。即使 FIT 成功导入 Garmin
Connect，也不保证中国区或国际区一定生成 Training Effect、Training Load、
VO2 Max、恢复时间或训练准备度。设备 product ID、Unit ID 和固件版本应与
用户自己的真实 Garmin 设备匹配；Unit ID 不应公开、提交到仓库或写入日志。

在公开仓库发布、向第三方分发或改为正式产品前，应先核实并取得上游代码和 OneLap/Garmin 非公开接口的必要授权，再确定最终许可证。仅把本项目改成 MIT 或 GPL 并不能代替该授权核实。
