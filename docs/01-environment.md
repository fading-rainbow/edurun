# 环境审计

观测日期：2026-11-02（Asia/Shanghai）。范围：本轮实际主机/MCP/只读设备管理检查。
原始结果仅保存在 `.local/research/environment-20261102/`。

## 1. 仓库与主机

- 工作目录：`E:/edurun`；Windows 11 家庭中文版 x64，`10.0.26200`。
- PowerShell `7.6.6`；Codex CLI `0.163.0`。
- 当前 MCP 握手报告客户端 `codex-mcp-client 0.166.0-alpha.5.1`；与单独 CLI 版本分开记录。
- 仓库：`fading-rainbow/edurun`（现有公开仓库），开发分支 `main`。
- 开始时本地、fetch 后 `origin/main`、`git ls-remote` 均为
  `f509a0bf8b4ca6ce3e885ab2bb93733d2df66ce3`。
- 开始时只有 `AGENTS.md` 有未提交修改；保留，不纳入本轮提交。

## 2. 实际可执行工具

| 工具 | 真实版本/位置 | 结论 |
| --- | --- | --- |
| Node.js | `v26.13.0`，`C:/Program Files/nodejs/node.exe` | 可用 |
| npm | `11.20.0` | 可用；未运行 npx 拉取最新版 REA |
| REA | `6.8.0`，`E:/REA/scripts/rea.mjs` | MCP 实际响应；CLI 用 node 显式路径调用 |
| Java / javac（shell） | Temurin `17.0.20`，完整 JDK，含 `jdk.compiler` | 可用 |
| Java（已运行 REA） | `E:/DevTools/jdk-25.0.5+8`，`jdk.compiler@25.0.5` | 保留现有 MCP 专用选择，不改全局 JAVA_HOME |
| Python | `3.13.15`（shell 与 `uv run` 均核实） | 可用 |
| uv | `0.12.11` | 可用，复用现有项目环境 |
| ADB | `1.0.41` / platform-tools `37.0.0-14910828` | 已安装，但不在当前 shell/MCP PATH |
| JADX CLI | `1.5.6`，`.local/tools/jadx-1.5.6/bin/jadx.bat` | 实际版本命令成功；不是 headless MCP JAR |
| aapt | `v0.2-12874835`，SDK build-tools `35.0.1` | 已实际读取旧 APK 的 badging |
| apksigner | `0.9`，SDK build-tools `35.0.1` | 版本命令成功；本轮尚未核验目标签名 |
| apktool | `Get-Command` 未找到，REA provider 不可用 | 选配缺口，当前有其他元数据工具，不盲目补装 |
| gitleaks | `8.30.1` | 可用 |

已核实 ADB 路径：
`C:/Users/asus/AppData/Local/Android/Sdk/platform-tools/adb.exe`。
MuMu 自带 ADB 也存在，但本轮不切换/终止用户 ADB 服务。

REA 本机及[官方 package.json](https://github.com/morluto/rea/blob/main/package.json)
的 Node 条件为 `^22.23.4 || ^24.22.1 || >=26.12.1`，当前 Node 满足。

## 3. REA MCP 实际连接与工具

证据 E-MCP-01：实际调用 `mcp__rea__binary_session({detail:"complete"})`。
服务器包/进程版本均为 `6.8.0`，当前未打开分析目标；工具目录共 `234` 项。
tools SHA-256：`6b18b37b70019f78f35f49a9130ab318e61395e66746a30e440945369097b937`。
skill 版本 `59`，但判断以实际 MCP 为准。

| 已暴露工具 | 初始 provider 状态 | 本轮实际调用 |
| --- | --- | --- |
| `inspect_android_package` | `provider_missing`，缺 headless JAR | 初始未进行 APK 分析 |
| `search_android_classes` | 同上 | 未调用 |
| `inspect_android_class` | 同上 | 未调用 |
| `inspect_android_method` | 同上 | 未调用 |
| `trace_android_references` | 同上 | 未调用 |
| `inspect_adb_client` | `provider_unavailable`，未选到 ADB | 已调用，返回 `capability_unavailable` |
| `list_adb_devices`、`inspect_adb_device`、`inspect_adb_package` | 同一 ADB 路径缺口 | 未谎称调用成功；改用已有 SDK ADB |
| `resolve_adb_packages`、`pull_adb_package` | 同一 ADB 路径缺口 | 未调用；离线设备不进行提取 |
| `decode_android_resources` | apktool 路径不可用 | 未调用 |

证据 E-MCP-02：`inspect_adb_client({})` 的实际错误说明找不到 Windows PATH/PATHEXT
中的 adb，要求设置 `REA_ADB_PATH`。不代表本机未安装 ADB。

证据 E-CLI-01：使用现有 `node E:/REA/scripts/rea.mjs doctor --client codex --provider jadx --json`。
Codex 注册 `aligned`，Node 检查通过，JADX 缺 JAR。
shell doctor 与已运行 MCP 的环境不同：shell 未带 Ghidra 环境，MCP 则已报告 Ghidra 可用；
不能用 shell 信息覆盖实时 MCP 判断，也不修复无关客户端注册。

根据[REA Android 官方说明](https://github.com/morluto/rea/blob/main/docs/android-analysis.md)，
需另供 headless `0.7.1` JAR 和完整 JDK 17+。当前 JDK 无需重装。
JAR 固定 SHA-256：`6e5eacf500b64292bfb73c49797c1958f6ee44646e43e868039ae7feb573ff75`。
用户已同意补装及仅增加 REA 工具路径，实际修复结果见第 6 节。

## 4. MuMu 与设备

- 卸载注册项实际版本：MuMu `6.8.8.0`，安装目录 `E:/Android/MuMu-BudaoRun`。
- `MuMuManager.exe` Authenticode：`Valid`，NetEase 签名。
- 管理器 SHA-256：`415c884b965332b4107f1744acd187603595edc56fe43d4e4ec61e387a2acfcd`。
- 使用已有 `.local/setup/mumu-owned-instance-6.8.8.json`，
  通过现有 `MuMuBridge(..., allow_stopped=True)` 的管理器哈希、路径和实例归属校验。
  未改写旧凭据文件；旧版默认凭据的哈希不代表当前管理器。
- `MuMuManager info --vmindex 1` 实际返回：`BudaoRun_MuMu`、index `1`、
  `android_version=12.0`、`is_android_started=false`、`is_process_started=false`、
  `error_code=0`、`hyperv_enabled=true`。
- 只读 Root 设置为 `false`。相关后台服务运行不等于安卓实例已启动。
- SDK ADB `devices -l`：列表为空；第一次查询启动了本机 5037 ADB daemon。
- 设备别名：`mumu-owned`；既有 loopback 序列号脱敏为 `127.0.0.1:<owned-port>`。
  停止状态未返回实时端口，不根据历史值硬编码连接。
- **实际 Android release/SDK/ABI、当前 App 包名和版本均未在线查询。**
  管理器配置值不是 getprop；没有操作真机、启动 MuMu 或 App。

## 5. 证据位置与边界

- `.local/research/environment-20261102/rea-mcp-audit.json`：实时 MCP 身份、相关 provider 和实际 ADB 错误。
- 同目录 `rea-shell-doctor.json`：补装前本机 CLI doctor 原始结果。
- 同目录 `host-audit.json`：主机版本、管理器停止状态、空设备列表与旧样本摘要。
- 同目录 `execution-spec-v2.md`：用户规范原件副本。
- [旧 APK 预检](02-apk-metadata.md)只证明本地旧文件，不证明当前安装或 split 完整性。

本轮不做代理/CA/Root/App 修改，不读取登录/相机/轨迹材料，不开始、结束或提交运动。
Git 验证和同步状态见[阶段进度](09-results-and-next-steps.md)。

## 6. 已授权修复及实际验证

1. 从 upstream GitHub release 下载固定 `jadx-headless-mcp 0.7.1`，42,619,858 字节，
   SHA-256 与上面固定摘要完全一致。未用普通 JADX CLI JAR 冒充 provider。
2. JAR 安装到
   `C:/Users/asus/.local/share/rea/android/jadx-headless-mcp-0.7.1-all.jar`。
   普通下载过慢后改用同一官方 URL 的 Range 下载并核对完整文件摘要；仅清理本次临时分片。
3. 精确备份 `C:/Users/asus/.codex/config.toml` 至本机
   `C:/Users/asus/.codex/backups/config.rea-android-20261102-233815.toml.bak`。
   备份内容不输出、不入库。TOML 结构对比确认仅在 `[mcp_servers.rea.env]` 新增：
   - `REA_JADX_MCP_JAR`：上述固定 JAR。
   - `REA_ADB_PATH`：已存在的 SDK ADB。
4. 保留其他配置、现有 JDK25/Ghidra、全局 PATH/JAVA_HOME，不执行 `rea setup`。
   配置来源参见[官方 OpenAI MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。
5. 在与保存配置一致的环境中运行 doctor：**所选 Codex/JADX 范围 `healthy=true`**。
   `environment_healthy=false` 仍存在，不能说全部可选 provider 已安装；未修复无关工具。
6. 用现有 MCP SDK 连接一个本轮独立启动的新 REA stdio 进程：实际 `tools/list` 为 234；
   Android 与 ADB provider 均为 available。
   - `inspect_adb_client` 成功：SDK ADB `37.0.0-14910828`，path_source 为 environment。
     Evidence：`ev_f0d19f28a2ccf50be78538a1807d08123a09014fa70c42b5e266aa2d260ee6ac`。
   - `list_adb_devices` 成功，`devices=[]`。
     Evidence：`ev_7f149229307fecd220cc0a3a899a6a139860f90eb836e264de17bf5dacd9df48`。
   - `inspect_android_package` 实际加载旧样本成功，但 `get_app_info` 报错
     `Root AndroidManifest.xml is not supported well-formed XML`。
     记录为 **execution_failure**，不重复同一失败请求，不称包分析完成。
   - 独立后续 `search_android_classes(query="")` 成功，返回 6 类；详见静态索引。
     Evidence：`ev_f8e65bedea44ca2cf8732d4bf19534a9bfdbc9f3e323448571b07f1854d4bdf3`。
   - 新进程结束后关闭其连接；类检索 Evidence 已导出，再 reset 本轮独立 investigation。
7. **当前聊天原有 MCP 进程未重启**，不会因为文件已修改自动得到新环境。
   上述新进程验证不冒充当前聊天 provider 已刷新。用户重启 Codex 后再实际检查。

新增本机证据：`config-change-receipt.json`、`rea-doctor-after.json`、
`fresh-mcp-session.json`、`fresh-mcp-adb-client.json`、`fresh-mcp-adb-devices.json`、
`fresh-mcp-apk-summary.json`（名称为 summary，但内容是完整失败/局部观察）、
`fresh-mcp-class-search.json`、`fresh-mcp-evidence.json`。
当前环境已能运行 provider；**目标 Manifest 兼容性和在线设备仍是下一阶段的独立问题**。

## 7. 用户重启后的当前聊天复核（2026-11-02 23:51 +08:00）

用户报告已重启后，直接从本聊天调用，不再通过临时独立 MCP 代验：

- `inspect_adb_client` 成功，SDK ADB `37.0.0-14910828`，`path_source=environment`。
  Evidence：`ev_f0d19f28a2ccf50be78538a1807d08123a09014fa70c42b5e266aa2d260ee6ac`。
- `binary_session` 报告服务器启动于 `2026-11-02T15:50:20.272Z`（本地 23:50:20），
  包版本 `6.8.0`，234 个工具；Android/JADX 和 ADB provider 均 available。
- 与指定版本、`E:/REA/scripts/rea.mjs` 和 combined catalog 摘要比较，`alignment=aligned`。
  首次比较误传 tools 摘要；根据本机 `src/mcpDoctor.ts:326` 改为 combined 摘要后已纠正，
  **无需因此再次重启**。局部摘要和完整目录摘要不可混用。
- `list_adb_devices` 成功，仍为 `devices=[]`。
  Evidence：`ev_40ff645d715a9026848e90b51d652d9e0d2fc6e28d759cf6f136bfd003d8187e`。
- 再次只读查询 MuMu index 1，`is_android_started=false`、`is_process_started=false`。
  本轮未启动模拟器或 App，尚无实际设备 SDK/ABI/当前安装包的新证据。

本轮证据：`.local/research/restart-verification-20261102/rea-restart-verification.json`。
此前第 6 节的“当前聊天未重启”是上轮历史状态，本节取代其当前状态判断。

## 8. 用户授权启动第二台 MuMu 后的在线审计

观测：2026-11-02 23:56–23:58 +08:00。用户明确要求代理启动 MuMu，指定第二台已装乐跑。

- 管理器 `info --vmindex all` 只读清单包含 index 0 `MuMu安卓设备` 和 index 1 `BudaoRun_MuMu`。
  第二台与现有 6.8.8 所有权凭据吻合，不依据显示顺序或历史端口盲选。
- 实际执行 `uv run budao start-mumu --receipt .local/setup/mumu-owned-instance-6.8.8.json`。
  现有守卫完成管理器哈希、实例归属、实时 loopback 端口、Root 关闭、shell uid 2000 和 SDK 32 校验；
  返回 `project_instance_started=true`、`target_app_launch_requested=false`。
- index 1 已启动并显示窗口；index 0 启动前后均停止。没有 wipe、改配置、复制登录数据或关闭用户应用。
- REA `list_adb_devices` 实际为一个在线设备，精确匹配管理器实时地址与项目凭据。
- REA `inspect_adb_device`：Android release `12`，SDK `32`；
  `ro.product.cpu.abilist=x86_64,arm64-v8a,x86,armeabi-v7a,armeabi`，`ro.debuggable=0`。
  Evidence：`ev_6c16246e41f6dfafabcc070d0bbc4b7e0fe2e9703b16b250032fda7f86b0ca78`。
- REA 未观察到 `ro.kernel.qemu`，`emulator_observed=null`，不能仅凭这个字段判断设备种类。
  MuMu 归属来自本机管理器/凭据匹配，不是 Android 声明的 Xiaomi 品牌或机型。
- REA `resolve_adb_packages(query="tanke")` 唯一匹配 `com.lptiyu.tanke`；
  `inspect_adb_package` 实测 `4.3.0` / code `230`、requested permissions `59`。
- REA `pull_adb_package` 根据实际完整 `pm path` 清单仅取得 `base.apk` 一项，零失败；
  本次设备清单未报告 split。摘要与旧本地样本完全一致，后续可以复用绑定相同摘要的静态证据。

本轮证据目录：`.local/research/mumu-readonly-20261102/`，其中包含启动前后管理器清单、
守卫启动结果、REA 设备/包信息、APK 集、签名/Manifest 工具输出和导出的 `rea-evidence.json`。
APK、完整设备路径和原始输出都未入库。完整在线设备信息见本节，取代此前“仍离线”的当前判断。

## 9. 原 App 正常启动后的补充

2026-11-03 用户授权自主常规决策后，REA 正常启动原包，已取得 SplashActivity 和 MainActivity
窗口焦点。package ABI 为 arm64-v8a；系统原生桥声明 libnb.so 和 ARM→x86 映射。
maps 读取被拒绝、exe读取未成功，未提权，实际库身份仍未知。
详见 [运行与网络配置报告](11-runtime-and-network-policy.md)，不将包 ABI 误作实际进程 ISA。
