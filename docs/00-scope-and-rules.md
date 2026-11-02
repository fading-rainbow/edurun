# REA 研究执行范围与证据规则

更新：2026-11-02（Asia/Shanghai）。主执行代理：Codex / GPT。

## 规范来源与优先级

用户要求采用本机 `C:/Users/asus/Downloads/budao_codex_gpt_rea_v2.md` 作为研究规范。
原文件 SHA-256：
`007ad3ceddbf3b07ea4b5fb932d696912ef72288c1e39b1f044d3ad94176ff2f`。
原件副本只存于被忽略的 `.local/research/environment-20261102/`，不上传。

- 在现有 `E:/edurun` 仓库开展研究，不另建重复仓库，不重置既有实现或设备。
- 执行原规范的阶段划分、REA 优先、只读优先、先验证再记录及脱敏要求。
- 本机 `AGENTS.md` 的现有设备归属、身份验证、提交条件和数据保护限制继续有效。
  原规范中的目标描述不构成绕过本人验证、修改认证结果、伪造成绩或自动提交的授权。
- 每轮先运行最小检查，区分“已证实 / 有证据的推断 / 未验证假设”，保留失败证据。
- 安装工具、改变 MCP/模拟器/代理配置、Root 或修改 App 前说明并取得用户同意。
  本轮用户已单独同意补装固定版本 JADX headless JAR 和增加 REA 两个工具路径。
- 用户随后明确同意由代理启动 MuMu，并指定“第二台”安装了步道乐跑；现场清单将其唯一
  对应至本项目 `BudaoRun_MuMu` / index 1。本次授权是启动和只读检查，不扩大为运动或提交授权。
- 用户随后指定代理为架构师并授予常规技术决策权；工具选择、正常启动、只读调查和隔离假数据
  实验自主推进，不再逐步询问。本人验证、现有数据保护及真实提交条件不被改写。

## 工具与设备

1. 实际调用 REA MCP 并读取当前 schema；skill 文件存在不等于 MCP/provider 可用。
2. Android 优先顺序：`inspect_android_package` → `search_android_classes` →
   `inspect_android_class` → `inspect_android_method` → `trace_android_references`。
3. 不可用时记录具体 blocker，再用 ADB、JADX CLI、Android SDK、Python 补充。
4. ADB 始终显式选择已核验设备；多设备先让用户选择，不默认操作真机。
5. 只对本项目独立 MuMu 进行经过授权的写操作；不改旧 AVD、不 wipe-data、
   不改全局 JAVA_HOME、不复制登录数据、不终止用户应用。
6. 复杂 GUI 操作先向用户请求使用 Computer Use；本轮不需要 GUI。

## 证据与资料

- 结论绑定样本 SHA-256、版本、工具版本、命令或 REA Evidence ID、位置和覆盖局限。
- 静态声明不等于实际调用；字符串不等于已观察接口；旧样本不等于当前已安装包。
- 不保存/重放生物识别资料。账号、凭据、设备标识、精确轨迹、原始流量和日志不上库。
- `.local/` 为现有私有工作区；`private/` 也被忽略。不批量收集设备应用数据或日志。
- 网络观察仅限正常授权行为；遇到信任策略阻塞先记录，不配置绕过机制。
- 本地 Mock 使用假账号、假点位、假认证结果，与生产 App/服务隔离。

## 交付与同步

每轮更新环境/假设/进度，报告运行结果、证据、未知项和一项最值得做的下一步。
本轮原有 `AGENTS.md` 脏修改不纳入提交。
任务改动须通过 pytest、ruff、diff 检查和 gitleaks，普通 commit/push 后复核远端完整 HEAD。
源码同步不代表部署、身份验证、真实运动或服务端审核验收。

## 目录映射

| 原规范目录 | 本仓库用途 |
| --- | --- |
| `docs/00-09` | 当前研究的规范、证据摘要、假设和阶段状态 |
| `diagrams/` | 后续有证据支持的 Mermaid 图 |
| `scripts/` | 复用现有工具；新研究脚本按需添加 |
| `mock-server/`、`mock-client/` | 自建隔离实验，当前仅占位说明 |
| `samples/` | 仅假数据，当前无业务样本 |
| `.local/`、`private/` | 忽略的本机资料，不提交 APK/日志/账户数据 |
