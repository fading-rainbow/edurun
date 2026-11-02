# APK 元数据：旧本地样本预检

更新：2026-11-02。状态：**第二阶段仅完成旧样本预检，未完成设备 APK 获取**。

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

## REA 实测与解析局限

补装后在新 MCP 进程实际调用 `inspect_android_package(detail="summary")`：
JAR/Java 成功启动，样本 `load_apk` 为 LOADED，局部观察报告 6 个已解析类、6,428 个资源。
但 `get_app_info` 因 `Root AndroidManifest.xml is not supported well-formed XML` 失败，
因此未得到成功的 REA Manifest/组件/权限投影。

对同一摘要样本，SDK `aapt dump xmltree ... AndroidManifest.xml` 能读取 manifest 根和版本属性。
这只证明不同解析器表现不同，不能直接说 APK 损坏、REA 已完整解析，或确定保护实现。
失败原始诊断保存在本机 `fresh-mcp-apk-summary.json`，详见环境审计。
