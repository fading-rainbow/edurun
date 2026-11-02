# 当前 APK 的静态加载边界

更新：2026-11-03。范围：离线类/方法/引用、常量推导和两份原生库的静态结构。
没有请求启动 App、执行目标代码、改动模拟器、采集账户/人脸或操作成绩。

## 1. 目标与证据保存

- 目标：上一轮从已核验第二台 MuMu 提取的 `base.apk`，183,732,002 字节。
- 本轮先重新计算 SHA-256：
  `8e0f86cd7a3ebdce35e99b01f04b3e2f7d5105a16d32097875b64fa2c4a2440a`，与安装包取证一致。
- 复用已完成的包信息/Manifest 解析失败证据，不重复同一失败请求。
- 引擎：REA 6.8.0 / JADX headless 0.7.1；所有被选方法的实际 overload index 均为 0。
- 本机证据：`.local/research/loader-static-20261103/`。
  `rea-evidence.json` 已导出 36 条 Evidence，然后清理本轮 investigation；原件不上库。
- `source-text/` 和 `source-text-index.json` 保存方法显示文本、摘要与对应 parent Evidence。
  下文“显示行”只指该派生文本的行号，**不是原始 Java 源码行、DEX 文件偏移或运行地址**。

## 2. 类与生命周期声明（已证实）

实际先调用 `inspect_android_class`，再根据其返回的方法名、prototype 和 index 检查方法。

| 对象 | 观察 | parent Evidence / 本机文件 |
| --- | --- | --- |
| `com.kiwivm.security.StubApplication` | 有 `LibName`、两个字符串字段、反射 Method/Object 字段及生命周期声明 | `ev_6213b262ac0412560eb1f048e565e721be8f05cb6b87d64a678912ccb8b3b883`；`StubApplication-class.json` |
| `com.kiwivm.security.SoLoader` | 声明 `load`、`l`、`I`、`O`、`nt`、`rn` | `ev_ea45168eef5387c047c44988eeba711f1b9c45a6b536f07eed856eaa51194bf3`；`SoLoader-class.json` |
| `attachBaseContext(Landroid/content/Context;)V` | `native_method`，`body_status=not_available` | `ev_d0f8e0654670a344a3acffbe797fbf35418985e4384a1e903b7ab8694dfd676a`；`StubApplication-attachBaseContext.json` |
| `onCreate()V` | `native_method`，`body_status=not_available` | `ev_71ab3e2281ff0427cc9ab9ab9c39b2d75e7be852308b2944e276354ebe256c6b`；`StubApplication-onCreate.json` |

两个方法源文本为空是 native 声明导致的真实结果，不是“反编译得到了空函数”，
也不能据此认定生命周期没有逻辑。Manifest 的 application 声明指向此类，但不是运行时调用记录。

## 3. 最小直接调用链（已证实的 DEX 静态关系）

```text
StubApplication.<clinit>()V
  -> StubApplication.lI()V
  -> SoLoader.load(Ljava/lang/String;)V
  -> System.loadLibrary(...)
```

| 边 / 方法 | 精确位置与覆盖 | Evidence |
| --- | --- | --- |
| `<clinit>` → `lI` | `classes.dex`；source raw method ID `com.kiwivm.security.StubApplication.<clinit>()V`；DEX code-unit offset **120 / 0x78**；method index 40；`INVOKE_STATIC` | `ev_195b511ec03ca5eca15abc409cd10ad283d5ac2725db0c83fe28447b85c88db7` |
| `lI` → `SoLoader.load` | `classes.dex`；source raw method ID `com.kiwivm.security.StubApplication.lI()V`；DEX code-unit offset **176 / 0xb0**；method index 5；`INVOKE_STATIC` | `ev_a0bd6d2fcd67b0ab6c0e907ad8049ca737e4cc8e95549492dce1c2e9aa1dec6f` |
| `<clinit>` 本体 | smali 回退，`static_initializer_disassembly`，文本 complete；显示行 9 起为常量初始化，显示行 142 的 `invoke-static` 调用 `lI` | `ev_bcef8cc5c600e1bda3b5e1f0e8e21414bc9ec8d3bf1b421e9e9d1b1a041ce26a` |
| `lI` 本体 | Java 派生文本 complete；显示行 3–17 是反射访问；显示行 20 为 `SoLoader.load(LibName)` | `ev_ff8d6c811d244c8fdb10137e97afc85d5fd1603cf1196574b4a53ffb45f269fa` |
| `SoLoader.load` 本体 | Java 派生文本 complete；显示行 7/12/29/34 包含 `System.loadLibrary`；源码带 duplicated-block 警告 | `ev_ed12a5d8707b173d5fde9da5258fe4cf01655c1e25b0cd979b141f8837907467` |

两个引用查询各报告一个 using node、一个直接 invoke 位置，节点内 `direct_invokes_exhaustive=true`。
这个 complete 仅覆盖提供者返回的直接引用范围，不涵盖反射、动态加载或运行时分派。
code-unit offset 单位为 DEX 的 16 位代码单元，不能当作整个 APK/DEX 的字节偏移。

`SoLoader.load` 的派生 Java 有明确 JADX 警告；这里只据此识别库加载调用及辅助检查，
不把可能有误的重复块、循环、异常/分支重建当成已证明的完整执行语义。

## 4. 库名及环境探测（有源码支持的常量推导）

`Xkd.l(Ljava/lang/String;[B)Ljava/lang/String;` 的方法体直接显示：每个数组字节与循环 key 字符 XOR，
随后构造 String。parent Evidence：
`ev_05a37ad0457a05f13ba9e24990e610b8d2cd7a852857b73d3323a98f8000f4ec`。
本轮用 Python 对选定 ASCII 常量做相同数学运算，**不执行 APK/Java/native 代码**。
输入、显示行和结果保存在 `decoded-loader-literals.json`；非 ASCII/默认编码的普遍等价性未被推广。

| 来源 | 推导结果 | 位置 / 含义边界 |
| --- | --- | --- |
| `<clinit>` 的 `array_7c` | `LibName = "kadp"` | smali 显示行 9–17；静态赋值推导，不是实际加载确认 |
| `SoLoader.load` 字符串调用 | `_x86`、ISA 属性名和本地文件路径 | 显示行 7/10/17/29；存在条件性的名称后缀选择 |
| `SoLoader.l(String[])` | 检查字符串是否包含 `x86` | 显示行 6；`ev_fc4daeaa9176b07b2dbf1935579e80bd824326797c5a13f9292714f704ce465f` |
| `SoLoader.O(String)` | 反射读取系统属性并检查 `x86` | 显示行 4/10；`ev_4c7e4448af01653f840a18111deb0a5a0038fd4afe2fceeafbf6fdfd2cb55d10` |
| `SoLoader.I(String)` | 读取文件前 20 字节，把第 18/19 字节组合为整数 | 显示行 8–20；`ev_04ad2209185c66666ed90f069b9ec3d065aaf5a2fed059ef121fe285e2672e38`；不是网络请求 |
| `StubApplication.III()` | 获取 Android 平台运行时的反射方法和对象 | `ev_3218777d748349dc11eb2a33529846879d393d743248b89bcb3dedbc235f23c1`；不证明身份认证/证书校验行为 |

Manifest/DEX/原生库中的网络 SDK、登录、定位、打卡、人脸调用和 API 协议尚无本轮业务代码证据。
上述系统属性、文件路径和反射不是已观察的 HTTP 接口。

## 5. 两份打包库与 JNI 绑定覆盖

从 APK 只提取明确的两个条目，核实摘要后作为本机只读分析目标，没有加载它们。

| 条目 | 大小 | SHA-256 |
| --- | --- | --- |
| `lib/arm64-v8a/libkadp.so` | 638,976 | `52a16796afd25d1034180b2e0ddd88ccf152a32bf22804f3c148a47a4c01a251` |
| `lib/armeabi-v7a/libkadp.so` | 393,216 | `7a35aea2f6ad67d254ec1251b395a57f064384948d3587a93b2398c106d0e7a7` |

REA `inspect_android_jni_bindings` 精确选择上述两个 ABI/库和两个生命周期方法。
parent Evidence：`ev_ce559f11a0aca4a75694bde32c9adf5a0a39efdfd3959afd7d9ec6f7da2c488b`。

- 根 DEX native inventory 报告 35 个声明，本次选中 2 个声明；不能把 root DEX 覆盖当作完整业务代码恢复。
- 2 方法 × 2 ABI = **4 个 binding 均 `unknown` / `coverage=partial`**；0 个 candidate。
- 两个库的 exports/tables/relocations 均 partial，包含未解析重定位和不能完整覆盖的可写区。
- **不能说“没有 JNI / 没有 JNI_OnLoad / 这些方法没有实现 / 这些库一定没有加载”。**

为定位 partial 的来源，继续用 REA `inspect_native_exports`，再读取保留 Evidence 的 issues 页：

| ABI | native-exports parent Evidence | 结果 |
| --- | --- | --- |
| ARM64 | `ev_4e4ed512a38446c4125f67ba10c14c938a9085bab17e2a257b98a970c074e123` | examined entries 34，exports 0，source coverage **partial** |
| ARMv7 | `ev_b91b9bbf694d926d91d59579420434d7d81fb591be89958d2b00e0d20d640dfe` | 同上 |

两库 issues 均为 `version_unresolved`：version requirements 没有链接到字符串表。
不要把摘要视图的 `complete-within-view` 误当底层导出表完整。

补充实际使用已安装 GNU readelf / Binutils **2.45**：两库分别报告 ELF64/AArch64 和 ELF32/ARM，
两次退出 0，同时输出 section link 和 local symbol/`sh_info` 警告。
原始结果保存在 `readelf-arm64-v8a.txt`、`readelf-armeabi-v7a.txt`，退出 0 不消除这些局限，
也不证明保护方案、可装载性、函数实现或发布者真实性。

## 6. 当前结论、图及下一阶段

**已证实：** application 声明、native 生命周期声明、两个带 DEX invoke 位置的加载边、
源文本中的库加载调用、常量推导、打包库身份，以及静态 JNI 绑定的实际 unknown 结果。

**有证据的推断：** 该根 DEX 主要提供一个 Java/native 引导边界；6 类清单不足以恢复业务模块。
此推断不指定具体加固厂商、动态解密方案或审核/认证实现。

**未验证：** 当前 App 真正加载的库/ABI、native 注册方式和实现地址、动态业务代码、
登录/运动/随机打卡/人脸接口、后续服务器确认。没有运行 App，不能从静态链证明它们。

初版图见 [loader-boundary.mmd](../diagrams/loader-boundary.mmd)。实线是静态声明/调用，
虚线是候选或未建立映射；它不是业务流程图，也不替代后续正常运行观察。
已用现有 Mermaid CLI 11.17.0 实际生成本机 SVG/PNG，并视觉检查，无需安装依赖。
10 个方法显示文本与所保留 Evidence 的 UTF-8 字节一致；行号在写入时保留原换行，已逐项核对。

下一阶段仅建议经用户同意启动原版 App，读取可访问的启动/进程 ABI/库元数据，
不操作登录、人脸、跑步，不改代理/证书或绕过校验。授权已询问，未获同意前继续离线。
