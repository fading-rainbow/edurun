# 静态分析证据索引

更新：2026-11-03。状态：已完成最小 Java/native 加载边界；业务模块/接口仍未恢复。

本轮先处理 REA Android provider 缺口，随后实际完成一次类清单检索。
既有 `QitRun-analysis.md` 针对另一个样本，不作为步道乐跑当前 APK 的证据。

## S01：实际类清单

- APK SHA-256：`8e0f86cd7a3ebdce35e99b01f04b3e2f7d5105a16d32097875b64fa2c4a2440a`。
- 实际工具：`search_android_classes`，`query=""`，JADX headless `0.7.1`。
- Evidence：`ev_f8e65bedea44ca2cf8732d4bf19534a9bfdbc9f3e323448571b07f1854d4bdf3`。
- 结果：`total_classes=6`、`duplicate_names=[]`、provider 类清单 coverage 为 complete。

```text
com.kiwivm.security.SoLoader
com.kiwivm.security.StubApplication
com.kiwivm.security.Xkd
com.lptiyu.tanke.R
kdp.AR
kotlin.coroutines.jvm.internal.DebugProbesKt
```

**已证实：** provider 能实际加载该样本并枚举上述类，不再只是配置就绪。
**局限：** complete 仅覆盖该 provider 的已解析类清单，不表示整个业务代码已恢复。
Loader/Stub 命名提示需调查加载边界，但尚不能证明其运行时行为或具体保护方案。
S01 当轮没有方法体、调用者/被调用者或业务网络库证据，不能凭类名制作业务模块图。
后续加载边界证据见 S02。

## 后续记录要求

后续每条记录必须包含：APK 摘要、原始类名、方法名/DEX prototype/重载 index、
REA Evidence ID 或文件行号/偏移、直接引用、置信度、反射/动态加载等覆盖局限。
尚不预设网络库、定位路径、随机点位来源或认证 SDK。

## 当前设备证据复用

本轮从第二台独立 MuMu 提取的当前 `base.apk` 摘要与 S01 完全一致；因此 S01 的 6 类清单
现在也绑定到本次安装包。新取 aapt Manifest 的 application 同样明确声明
`com.kiwivm.security.StubApplication`。未请求启动乐跑、未进行运行时解包或绕过，仍无业务方法链。
最小下一步是用 REA 检查该已知类的字段/方法声明，再选择实际重载和静态引用，不猜测入口。

## S02：当前 APK 的最小加载链（已执行）

详见 [10-loader-boundary.md](10-loader-boundary.md)，含类/方法 parent Evidence、
DEX 代码单元位置、原生库摘要、静态解码结果、覆盖局限与初版 Mermaid 图。

- `attachBaseContext`、`onCreate` 已实测为 native，方法体不可用；不是空实现。
- REA 直接引用确认 `<clinit>` → `lI` → `SoLoader.load`；源码中存在 `System.loadLibrary`。
- `LibName="kadp"` 来自已核实 XOR 方法与常量的离线推导，不是运行时观测。
- 两方法 × 两 ABI 的 JNI binding 为 4/4 unknown；导出/重定位 partial，不能推断 absence。
- 没有请求启动 App、执行代码或观察网络；业务调用链仍未建立。

S02 后已进行原版 App 正常启动和有限脱敏观察，见 [运行报告](11-runtime-and-network-policy.md)。
焦点到达 MainActivity，但映射权限拒绝，未将 S02 的 JNI unknown 改为 matched。

## S03：Web资产、原始DEX定义与业务组件字面量

详见 [13-web-assets-and-component-correlation.md](13-web-assets-and-component-correlation.md)。
根DEX原始class_defs为4；之前REA的6项是反编译器节点清单，不能直接等同原始定义数。
头记载data范围外仍有58,492,172字节，7936个不同目标命名空间描述符样式字面量，
其中262个Manifest组件名可匹配。它们不是恢复的方法/调用边，尚不能据此声称完整反编译。
仅有一个通用JS桥和三份无script的HTML文档；嵌套SDK DEX未找到目标命名空间定义。
