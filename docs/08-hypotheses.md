# 假设、证据与未知项

更新：2026-11-02。

| ID | 判断 | 状态 | 证据 / 仍缺内容 |
| --- | --- | --- | --- |
| E01 | REA MCP 已连接 | 已证实 | 实际 `binary_session` 返回服务器 6.8.0 和 234 工具目录 |
| E02 | skill 存在即可分析 APK | 已否定 | 当前 MCP Android provider 明确返回缺少 JAR |
| E03 | 本机未安装 ADB | 已否定 | SDK ADB 37.0.0 实际可执行；MCP 只是未配置路径 |
| E04 | JADX CLI 能替代 REA 所需 headless MCP JAR | 不成立 | 两者是不同制品；CLI 1.5.6 已存在，headless 0.7.1 缺失 |
| E05 | MuMu 当前可用且能读 App 包 | 未证实 | 管理器确认实例停止，设备列表为空 |
| E06 | 旧 APK 就是当前安装版本且 split 完整 | 未证实 | 只核实本地文件；没有当前 `pm path` 对照 |
| E07 | 点位由服务器生成 / 某认证 SDK / 证书固定已启用 | 未验证假设 | 本轮无类、方法、流量证据 |
| E08 | 旧有效次数证明当前运行或最终审核成功 | 不成立 | 本轮没有启动运动或查询成绩，历史证据不更新当前状态 |
| E09 | 补装后 REA 能实际处理该旧样本 | 部分证实 | 新 MCP ADB/类清单成功；包信息因 Manifest 解析失败，不是全量成功 |
| E10 | 仅 6 类说明业务源码已全部获得 | 未证实 | 只覆盖 provider 已解析 DEX 类；动态加载/保护边界仍未知 |
| E11 | 第二台就是项目独立 MuMu 且可在线读取 | 已证实 | 管理器 index 1 与凭据匹配；守卫启动成功；REA SDK32/ABI 实测 |
| E12 | 旧 APK 与当前安装包相同且当前无 split | 本次设备范围已证实 | 完整 pm path 集仅 base；REA 提取 SHA-256 与旧样本相同 |
| E13 | requested permissions 59 与 Manifest 60 矛盾 | 已解释 | Manifest 60 条有一项重复，唯一权限 59，与设备计数一致 |
| E14 | APK 签名校验代表官方来源已确认 | 未证实 | apksigner v2 通过；缺独立官方签名基线，不能扩大结论 |
| E15 | application 生命周期在 DEX 中有可读 Java 业务体 | 已否定此表述 | 选定 attachBaseContext/onCreate 为 native_method，body not_available |
| E16 | 存在通向 System.loadLibrary 的静态引导链 | 已证实 | clinit→lI 与 lI→load 各有直接 DEX invoke；方法体有库加载调用 |
| E17 | JNI candidate 为零就证明没有实现 | 不成立 | 四 binding 均 unknown，导出/表/重定位 partial，动态构造仍未知 |
| E18 | 当前真实加载的是 ARM64 libkadp.so | 未验证 | 只有打包身份与选择分支，未观测 App 运行/加载 |
| E19 | Loader 的系统属性/文件路径就是 API 清单 | 不成立 | 仅本地探测，无 HTTP/正常通信证据 |

环境修复的“文件安装”“doctor 就绪”“新 MCP 实际调用”“当前聊天 MCP 生效”
分别验收，不相互替代；最终结果见环境审计。
