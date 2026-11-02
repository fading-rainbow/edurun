# 阶段进度与下一步

更新：2026-11-03（Asia/Shanghai）。本轮在线设备取证于 11 月 2 日夜，文档/同步于跨日后收尾；
从环境审计推进至当前 APK 元数据，没有进行跑步/认证实验。

## 本轮结果

- 已读取用户 v2 规范，保存原件摘要，建立现有仓库内的 `docs/00-09` 研究入口和阶段目录。
- 先实际调用 REA MCP，而非根据 skill 推断连接：6.8.0、234 工具；Android/ADB 初始缺口已明确。
- 已实测 ADB、JADX CLI、Java、Python、Node/npm、SDK 工具和 gitleaks；不重复安装已有环境。
- 已只读核实 MuMu 6.8.8、NetEase 签名、独立实例归属、Root 关闭、实例停止及空设备列表。
- 已核实本地旧 APK 的摘要、包名、版本、SDK 声明和 native ABI；未把它等同于当前安装包。
- 用户已单独同意并完成固定 headless JAR 补装、配置备份和两个 REA 路径增加。
- 新 MCP 进程实际验证 ADB 和类枚举成功；旧 APK 的 REA Manifest 解析失败，已保存局部证据。

## 阶段验收

| 阶段 | 状态 | 剩余条件 |
| --- | --- | --- |
| 1. 环境审计 | 已完成本轮目标 | 用户授权启动第二台；SDK32/ABI/当前安装包已实际读取 |
| 2. APK 获取/元数据 | 当前 APK 集已提取；v2 校验、权限/组件/库首轮索引已完成 | REA Manifest 解析仍有限制；运行时行为/发布者真实性不由静态证明 |
| 3. 静态类/方法/引用链 | 旧样本 6 类清单；无业务调用链 | 确认当前 APK、处理解析/代码覆盖边界，再定位方法/引用 |
| 4. 正常网络观察 | 未开始 | 正常授权场景、采集兼容性与本地脱敏 |
| 5. 状态机/信任边界 | 未开始 | 静态与正常动态证据交叉验证 |
| 6. 隔离 Mock/Demo | 仅目录说明 | 假数据协议与可运行测试；不连接生产服务 |
| 真实成绩/审核 | 本轮未操作/未验收 | 不从环境、旧记录、合成数据或静态代码推断 |

## 真实验证结果

本轮首次完整检查：`uv run pytest -q` 为 **1198 passed in 32.85s**；
`uv run ruff check .`、`git diff --check` 均通过。
最终研究文档加入后，staged gitleaks 扫描约 **23.33 KB、零发现**；
独立 staged 源码导出扫描约 **1.53 MB、零发现**。
新增忽略规则通过 15/15 样例路径检查，15 个 Markdown 文件的本地链接无缺失。
最终提交、普通推送及远端完整 HEAD 的真实回读结果存于本机
`.local/research/environment-20261102/git-sync.json`；该验收文件生成前不算同步完成。
`AGENTS.md` 的原有脏改动不打包；不部署、不操作 VPS、不创建 Cloud 旧快照。

重启后复核轮再次执行：**1198 passed in 30.77s**，ruff、diff 检查及 staged gitleaks 均通过。
本轮同步回读单独记录于 `.local/research/restart-verification-20261102/git-sync.json`，
不覆盖上一轮的测试/同步证据。

随后用户授权启动第二台：已启动 `BudaoRun_MuMu`（index 1），第一台保持停止。
REA 实测 Android12/SDK32、ABI 和乐跑 4.3.0/code230，提取完整 APK 集（仅 base）；
其摘要与旧样本一致。已完成 REA 签名结构索引和 apksigner v2 实际校验，原件留本机。
本轮后续验证及同步记录在 `.local/research/mumu-readonly-20261102/`，不覆盖前两轮。
本轮 **1198 passed in 32.82s**，ruff、diff 检查及 staged gitleaks 均通过；
只提交研究文档，不提交 APK、签名材料、原始设备输出或现有 `AGENTS.md` 改动。

## 下一步（仅一项）

**用 REA 检查已识别的 `com.kiwivm.security.StubApplication` 类声明，建立最小静态加载引用边界。**
用户启动授权已取得且执行完毕，不再等待启动确认或重复重启。
MuMu 保持开启；没有请求启动目标 App、身份验证或跑步，不将本次授权扩大为成绩操作。
