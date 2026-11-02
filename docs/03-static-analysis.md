# 静态分析证据索引

更新：2026-11-02。状态：仅完成旧样本 DEX 类清单验证，未进入业务方法分析。

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
本轮没有方法体、调用者/被调用者或业务网络库的新证据，不能凭这些名称制作业务模块图。

## 后续记录要求

后续每条记录必须包含：APK 摘要、原始类名、方法名/DEX prototype/重载 index、
REA Evidence ID 或文件行号/偏移、直接引用、置信度、反射/动态加载等覆盖局限。
尚不预设网络库、定位路径、随机点位来源或认证 SDK。

## 当前设备证据复用

本轮从第二台独立 MuMu 提取的当前 `base.apk` 摘要与 S01 完全一致；因此 S01 的 6 类清单
现在也绑定到本次安装包。新取 aapt Manifest 的 application 同样明确声明
`com.kiwivm.security.StubApplication`。未请求启动乐跑、未进行运行时解包或绕过，仍无业务方法链。
最小下一步是用 REA 检查该已知类的字段/方法声明，再选择实际重载和静态引用，不猜测入口。
