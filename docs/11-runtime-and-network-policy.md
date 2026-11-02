# 正常启动、脱敏观察与网络配置

更新：2026-11-03。用户已授权代理自主决定常规技术操作，不再等待逐步确认。
范围：独立 MuMu 原 App 正常启动、只读元数据和有界脱敏日志；未进行运动或身份操作。

## 1. 正常启动：实际观察

启动前核实工作区/远端，保留 `AGENTS.md` 修改；现有 MuMuBridge 确认 6.8.8 管理器、
index 1、实时 loopback 地址、Root 关闭、shell uid2000 与 SDK32。index 0 未启动。

| 操作 | 真实结果 | REA Evidence |
| --- | --- | --- |
| 启动前窗口 | Lawnchair launcher | `ev_5f5482621f1323bd0e64f414eba81e1af850330e1f20c820e356ddb8e12d67c3` |
| `start_adb_app` | 原入口 `.activities.splash.SplashActivity`，am status `ok` | `ev_c1d63ad54cbf2290b7b7129a2a13bace337189622e8e315fe06bb6a69005dc9a` |
| 即时窗口 | SplashActivity 的 window/focused app 一致 | `ev_e6ab9e16142c385e587639b204dba775ac5a1d847025c74057b5db7cc8440df6` |
| 稍后及收尾窗口 | `.activities.main.MainActivity` 的 window/focused app 一致 | `ev_b4749ec6d755e3aa2cc0164f5834d18cc351ea65088f5dd06b2ff35fad319e48` |
| 进程枚举 | 主进程存在；后续 cmdline 前后核实为目标包 | `ev_0511969d66e23ba64ac2843b57ab9840305297c42a27abe62887fc7c4865d2d5` |

已证明正常启动后焦点到达 MainActivity，不只依赖 am 返回码。
这不是登录状态、页面所有控件、服务器健康、身份结果或成绩验收；未截图或读取账户 UI。

## 2. ABI 与原生库：仍未确认的边界

- package metadata：`primaryCpuAbi=arm64-v8a`、`secondaryCpuAbi=null`。
- 实际 getprop：`ro.dalvik.vm.native.bridge=libnb.so`，`ro.dalvik.vm.isa.arm=x86`，
  `ro.dalvik.vm.isa.arm64=x86_64`。这是安装/系统配置，不是已观察的进程执行 ISA。
- 核实 PID/cmdline 后，REA 仅尝试一次提取 `/proc/<pid>/maps`，返回 `Permission denied`。
  未提权、Root、ptrace、run-as 或读取进程内存。
- exe readlink 也未成功，其具体失败原因未知；status 的有限状态字段可读。
- 安装 native 目录为空，REA 覆盖 complete；Manifest 为 `extractNativeLibs=false`。
  不能由空目录推出没有加载库，也不能把前轮 4 个 JNI unknown 改写为 matched。
- **实际加载库、JNI 注册关系和实现地址仍未确认。**

## 3. 可复用的只读脱敏观察器

```powershell
uv run budao observe-mumu-startup `
  --receipt .local/setup/mumu-owned-instance-6.8.8.json `
  --lines 500 --age-seconds 1800
```

实现：`src/budao/research_observe.py`，测试：`tests/test_research_observe.py`。
该命令不启动 App、点击 UI、改配置、清日志或拦截网络。

- 只接受现有受守卫的 MuMu；前后核实唯一主进程 PID/cmdline。
- main buffer 上限 500 行/1800 秒，超过 2 MiB 拒绝。
- 原消息只在 Python 内存中投影，不写文件、打印或进入 REA 原始日志 Evidence。
- 只输出固定计数、策略允许主机名和两个已知 Loader 库名。未知主机仅计数；
  URI 路径/查询、认证头、消息正文、未知库名均不返回。
- 无法解析的格式单列 partial，避免误判为没有事件。实际 epoch 列有前导空格，已修复并回归测试。

实际结果 `startup-sanitized-summary.json`：455 行中 454 行在范围内、1 行标题，
unparsed/wrong PID/outside window 均为 0，format coverage=complete-within-returned-lines。
有 2 条 native-load-error 关键词、0 条 FATAL EXCEPTION 关键词，没有命中 kadp/kadp_x86 库名；
不可泛化为 Loader 失败或 App 无错误。API2 主机候选 0，其他主机出现 2 次但值省略。
`http_requests_proven=false`：日志 URL 不证明发包，缺日志也不证明无网络。
18 个观察器测试覆盖脱敏、假冒主机、上限、PID/时间、进程变化、格式及错误投影。

## 4. 实际网络配置

同一摘要 APK 的 Manifest：usesCleartextTraffic=true、networkSecurityConfig=@0x7f14000b。
`aapt dump --values resources` 定位实际路径 **res/4uC.xml**，而非猜测标准文件名。
实际 `aapt dump xmltree` 取得：

| 原 XML 行 | 声明 |
| --- | --- |
| 2–6 | debug-overrides：user + system CA |
| 10–12 | base-config：cleartextTrafficPermitted=true，只有 system CA |
| 16–21 | domain 文本为 **`https://api2.lptiyu.com`**，includeSubdomains=true，user + system CA |

带 scheme 的文本是原件事实，不能悄悄修成裸 hostname 再声称已匹配。
[Android 官方说明](https://developer.android.com/privacy-and-security/security-config)
区分 per-domain 配置和只在 app debuggable 时适用的 debug-overrides。
Manifest 未显式声明 debuggable，package flags 也未见 DEBUGGABLE；不能因存在此段就认定接受用户 CA。

对照 AOSP Android12.1：
[XmlConfigSource.parseDomain](https://github.com/aosp-mirror/platform_frameworks_base/blob/android-12.1.0_r1/core/java/android/security/net/config/XmlConfigSource.java#L143-L160)
保留 trim/lowercase 后的文本；
[ApplicationConfig.getConfigForHostname](https://github.com/aosp-mirror/platform_frameworks_base/blob/android-12.1.0_r1/core/java/android/security/net/config/ApplicationConfig.java#L64-L106)
做 hostname 精确/后缀匹配。
**推断：**按该标准实现，带 scheme 的规则不会匹配裸主机 api2.lptiyu.com；
未逆向 MuMu 定制框架或实测 TLS，不能冒充设备验证。XML 未见 pin-set，不证明代码/SDK 无证书固定。
本轮未装 CA、改代理/证书、关闭校验或向真实 API 发自造请求。

## 5. 静态候选，不是业务接口

对带值资源输出只保留 hostname、资源 ID/名称和显示行，HTTP(S) 共 8 处：

| 候选 | 来源 | 边界 |
| --- | --- | --- |
| auth.snapchat.com，1处 | 0x7f030020 / array/snap_connect_scopes，行851 | SDK scope 文本，不是实际请求证明 |
| www.mob.com，7处 | 0x7f110315、0x7f110318、0x7f110590，行71662/71668/72932/73547 | 授权说明/网站文本，不是跑步 API |
| api2.lptiyu.com | 前节 XML domain 文本 | 配置候选，不是流量或路径证明 |

首次 aapt 未带 --values，零命中不算阴性证据；已按本机 help 修正。
查询不覆盖全部 DEX/native/压缩资产/动态字符串，不建立 HTTP 方法、路径、字段或响应状态。

## 6. 原件与剩余工作

`.local/research/runtime-start-20261103/` 保存启动/窗口/进程元数据、6 条 REA Evidence、
maps 失败原因、有限状态/属性、aapt 原件、候选投影、脱敏摘要及验证/同步结果。
没有原始 logcat、抓包、账户或生物资料。实际库映射、业务 API、登录/运动/打卡/人脸迁移仍未知。
常规只读调查自主推进，不用 Root、绕过或假模型填补真实未知。
