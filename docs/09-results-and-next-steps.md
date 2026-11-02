# 阶段进度与下一步

更新：2026-11-02（Asia/Shanghai）。本轮从环境审计开始，没有进行跑步/认证实验。

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
| 1. 环境审计 | 主机、补装、当前聊天 MCP 与离线身份已核实 | 在线 getprop/当前包检查尚缺；MuMu 尚未启动 |
| 2. APK 获取/元数据 | 仅旧本地样本预检 | 在线读取完整 pm path、提取全部 split、核对签名/Manifest |
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

## 下一步（仅一项）

**经用户同意后，仅启动已核验的独立 MuMu，再只读核实 SDK/ABI、当前包版本及 APK 路径。**
用户已重启 Codex；本聊天的 ADB 实调与 REA 身份比较均通过，Android/ADB provider 可用。
23:51 的设备列表仍为空，独立 MuMu 管理器报告实例停止；不再要求重复重启。
启动授权已单独询问，未获同意前不启动；不自动开始跑步。
