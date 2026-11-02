# APK 元数据：当前 MuMu 安装包与旧样本对照

更新：2026-11-02。状态：**已获取当前设备完整 APK 路径集并完成首轮静态元数据/签名核对**。

## 上轮旧样本预检（历史）

| 项目 | 已观察值 | 证据 |
| --- | --- | --- |
| 本地样本 | `.local/apks/budao-0.apk`，183,732,002 字节 | 本机文件属性 |
| SHA-256 | `8e0f86cd7a3ebdce35e99b01f04b3e2f7d5105a16d32097875b64fa2c4a2440a` | `Get-FileHash -Algorithm SHA256` |
| 包名 | `com.lptiyu.tanke` | SDK build-tools 35.0.1 的 `aapt dump badging` |
| 版本 | `4.3.0` / code `230` | 同上 |
| minimum / target SDK | `24` / `33` | 同上 |
| APK 包含的 native ABI | `arm64-v8a`、`armeabi-v7a` | `native-code:` 行 |

以上事实只适用于该文件，不建立其与当前模拟器安装包的对应关系。
MuMu 未启动，尚未运行设备 `pm path`、提取 base/split、核对签名/权限/导出组件。
ABI 列表是 APK 内容，不是本轮实际设备的 ABI 观测。

下一阶段必须先在已核验的独立设备读取包名/版本及全部 `pm path`，
再提取完整 APK 集并逐文件计算摘要；签名与 Manifest 另行取证。

## 本轮设备绑定与提取

在用户授权启动的第二台 `BudaoRun_MuMu`（index 1）上，通过 REA 实际执行：

| 操作 | 结果 | Evidence |
| --- | --- | --- |
| `resolve_adb_packages("tanke")` | 唯一匹配 `com.lptiyu.tanke` | `ev_f64718067f82d839953d1b2ba370cf9507afe6114ff026704b5c16885601ebeb` |
| `inspect_adb_package` | 版本 `4.3.0`、code `230`，requested permissions `59` | `ev_2fc30533252b1bdb12762c30ca2be3c48ea91411bf48a05d3a022e9f12c9ac63` |
| `pull_adb_package` | 完整路径集仅一项 `base.apk`，183,732,002 字节，零失败，coverage complete | `ev_f17eb494f1a7c06264135161087c39d8b1607d140b79963b93b48bda5a8fcfd3` |

当前设备报告的安装位置脱敏为 `/data/app/<install-slot>/com.lptiyu.tanke-<slot>/base.apk`。
本次 `pm path` 未报告 split，不将这个观察泛化到其他版本或设备。
本机提取位置：`.local/research/mumu-readonly-20261102/apks/com.lptiyu.tanke/base.apk`。
SHA-256 与上表旧样本完全相同；因此旧样本已解析类和 REA Manifest 失败证据可以复用，
无需对同一字节重复已知失败请求。没有提取账号、数据库、偏好或登录资料。

## 签名：结构声明与密码学校验分开

- REA `inspect_apk_signature(detail="summary")` 观察到 v1、v2 各一个声明 signer，
  证书 SHA-256 均为
  `a6af18d3f047f3a210e9762f41b6f923f445151625fb5533c41748c7b02a6a24`。
  parent Evidence：`ev_6da398aab034860ad6117a7b8d99cb9695d56258318dddcc42927fc114a35123`。
  REA 不做签名密码学验证；4 个非已知签名字段字节未解释，不自行推断为恶意或损坏。
- 补充实际执行 SDK build-tools 35.0.1
  `apksigner verify --verbose --print-certs <base.apk>`，退出 `0`，输出 `Verifies`，
  **v2=true**，一个 signer，证书摘要与 REA 相同；其报告 v1/v3/v3.1/v4=false。
- “存在 v1 声明”不等于“v1 校验通过”。以上也不建立与独立官方渠道签名基线的对应关系，
  未将此处的密码学自洽误称为发布者真实性已验收。

## SDK/Manifest/原生库首轮索引

同一当前提取 APK 的 `aapt dump badging`、`dump xmltree` 和 `dump permissions` 均退出 `0`。
min/target SDK 仍为 `24` / `33`；ZIP 目录包含一个 `classes.dex`。

- application 声明：`com.kiwivm.security.StubApplication`。
- 组件声明：activity `423`、activity-alias `1`、service `47`、receiver `14`、provider `29`，共 `514`。
- `android:exported` 显式 true `29`、false `149`、未声明 `336`；未声明不在本轮推断默认值或可访问性。
- Manifest 权限声明 `60` 条、不同权限 `59` 项；`SYSTEM_ALERT_WINDOW` 重复声明两次。
  这解释了设备 requested permissions `59` 与原始声明 `60` 的差别，不能混作同一个计数。
- ZIP 内 `.so`：`arm64-v8a` `39` 项、`armeabi-v7a` `32` 项，共 `71` 项。
  这是包内文件，不代表已观察到哪一个 ABI/库实际加载或依赖调用。
- 组件/权限/库索引只存本机 `metadata-index.json`，来源为 aapt 的明确树结构和 ZIP 目录。
  未解释的属性、合并来源、运行时权限授予、组件可达性和第三方模块关系仍未验证。

## REA 实测与解析局限

补装后在新 MCP 进程实际调用 `inspect_android_package(detail="summary")`：
JAR/Java 成功启动，样本 `load_apk` 为 LOADED，局部观察报告 6 个已解析类、6,428 个资源。
但 `get_app_info` 因 `Root AndroidManifest.xml is not supported well-formed XML` 失败，
因此未得到成功的 REA Manifest/组件/权限投影。

对同一摘要样本，SDK `aapt dump xmltree ... AndroidManifest.xml` 能读取 manifest 根和版本属性。
这只证明不同解析器表现不同，不能直接说 APK 损坏、REA 已完整解析，或确定保护实现。
失败原始诊断保存在本机 `fresh-mcp-apk-summary.json`，详见环境审计。
该文件位于上一轮 `.local/research/environment-20261102/`；本轮已经通过摘要将其绑定当前包，
没有因为路径不同而重复相同失败操作。
