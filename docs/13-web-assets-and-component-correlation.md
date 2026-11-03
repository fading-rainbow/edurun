# 预置 Web 资产与业务组件关联

更新：2026-11-03。仅离线读取设备绑定 APK，不执行 JS/native，不访问发现的域名或业务接口。
APK SHA-256：`8e0f86cd7a3ebdce35e99b01f04b3e2f7d5105a16d32097875b64fa2c4a2440a`。

## 1. REA 总体清单

实际按 archive 路由 `open_binary` → `inspect_artifact(summary)` →
`project_android_application_graph`，不是让 Ghidra 把 APK 当 ARM 程序执行。

- APK ZIP 中 6,428 条目；REA occurrence 6,429 包含 root，6,180 个内容节点，0 个 integrity contradiction。
- inventory parent：`ev_d757c22aba9f07234a080c5671600e7df66219a94d1f779d0d25a8fe934e5ce8`。
- inspect parent：`ev_c748caf7322cf4781a2a017b8d0d7c7d94d622554bb9b6d28abd3ffcdbbc2f9a`。
- Android projection：`ev_a238bf1d133d691a8e5a09261b6912f9c49f9cf35298acbe0c7bd57330331a2b`。
  路径提示 dalvik-art/javascript/native；1 root DEX、1 JS、71 native library。
  71 条 bridge candidate 是路径约定假设，**不是 71 个已解析 JNI 调用**。
- 清单指出部分路径存在大小写折叠冲突；本轮只按已知条目提取 4 个互不冲突文件，未整包覆盖解压。

## 2. Web 资产实际内容

assets 子树 227 文件、11,066,332 未压缩字节；按指定文本后缀/目录选出 36 个候选。
其中仅 1 个 JS、3 个 HTML、30 个 JSON、2 个 XML；不把文件名当用途结论。

| 文件 | 大小/摘要 | 实际读取结果 |
| --- | --- | --- |
| WebViewJavascriptBridge.js | 5,123；`ee40c590a41b40d59fabfce3a32d56dc51c475f6692d11f6fc32f702d0722a59` | 通用 JS/native 传递与回调桥 |
| open.html | 1,501；`6f798e1fc6fb23b63f8425ec8c96ce2a0bac564b4e30272a8243496cf823e7d2` | title 为“注销账号”，0 script |
| privacy_protocol.html | 640,151；`0ebe4d510731a31dc8b20a5d1d3e023b448369386109e230e3ae05c94a7ebcc5` | 隐私说明/SDK链接，0 script |
| user_protocol.html | 152,291；`6d680a84083084cc9d65d46716832b0338ee6a1da33a3a6295b8cfce73fdb9a9` | 协议文档，0 script |

HTML 只静态解析标签，没有加载页面/链接。SDK 隐私链接不能证明库实际运行或被用于人脸验证。
30 个 JSON 中 27 个具有 Lottie 结构，余下为 city 数组、ksad_idc、supplier 配置结构。
这些结果不支持把预置文件当完整跑步 H5 前端；未覆盖未知私有格式或远程动态下载内容。

### 桥脚本可证实的行为

REA `analyze_javascript_application` 只选该 JS：5123字节、1个语法解析成功文件、453个AST节点。
parent：`ev_5d079f9a79f52513797ced23bf88b49926f558ba262d1c3acd683b002a71fb0d`。

| 显示行 | 符号/行为 |
| --- | --- |
| 34–35 | registerHandler 写入 messageHandlers |
| 38–44 | callHandler / _doSend 接收动态 handlerName |
| 55 | 动态解析 window.android 对应成员 |
| 71 | 回退到 window.android.send |
| 76–99 | JSON消息/responseId/handlerName分派与回调 |
| 114 | _handleMessageFromNative |
| 135 | WebViewJavascriptBridgeReady 事件 |

未执行 eval、注册 handler、调用 native bridge 或伪造返回。
提供者 request 关系0、routes页0项，但语义图有153个unknown，整体application coverage partial；
这只是该来源的静态结果，不证明整个App没有网络/路由，也没有得到真实业务 handler 名或 Java peer。
选择外的3个HTML没有交给JS分析器，它们的标签检查由独立静态解析完成，来源范围分别记录。

## 3. 嵌套容器和根 DEX 的实际区别

精确检查 assets 中2个JAR和1个ZIP的中央目录，未发现 HTML/JS/CSS/map 条目。
JAR的两个 classes.dex 仅提取到私有目录，再用SDK build-tools35.0.1的 dexdump 检查，未忽略校验。

| 来源 | DEX大小 | class descriptor数量 | 目标命名空间匹配 |
| --- | --- | --- | --- |
| root classes.dex | 58,501,904 | **4** | 0 |
| bdxadsdk.jar 内DEX | 4,781,592 | 3449，主要 com/baidu、com/component、com/style | 0 |
| gdtadv2.jar 内DEX | 3,061,596 | 3375，主要 com/qq、com/tencent、com/android | 0 |

3次checksum检查和3次dump均退出0；仅保存描述符计数/namespace，未保存完整dump正文。
根DEX的4个原始定义为SoLoader、StubApplication、Xkd、kdp.AR。
REA此前6项清单另含R和DebugProbesKt节点，它们不在本次根DEX class_defs中；具体生成/依赖来源未核实。
**反编译器节点数不能当原始DEX类定义数**，也不能把嵌套广告SDK当已恢复的业务代码。

## 4. 关键新增：普通索引之外仍有大量业务字面量

根DEX SHA-256：`152b7ae721f3d4716896a3ef3cf103b33fa070ed59b44f06cac206fb2c023a9b`。
只读核对DEX035头和map：

- file_size=实际文件大小=58,501,904；class_defs_size=4。
- data_off=2792、data_size=6940，头记载data范围结束于 **9732**。
- map_offset=9560，其14条map记录也结束于9732。
- 之后还有 **58,492,172 字节**处于file_size内、但不在头记载的data范围内。
  不能误称为file_size之外的追加文件，也没有因此认定文件损坏。
- 按[Android DEX格式说明](https://source.android.com/docs/core/runtime/dex-format)，这些字段在本次035版本有对应含义；
  不使用041的新container语义解释此样本。

REA `inspect_byte_entropy` parent：
`ev_9c2ddd8c7abe660ea04b3bf191fbab25ba5d9d8f6fc9d0df749bd4ead24e6dd0`。
文件整体熵6.3343；所选区域按65536字节测量893块，未形成阈值high/low runs。
熵不能证明加密、压缩或保护算法，本轮没有解密、修补magic或重写DEX头。

在此区域精确字节搜索得到8176次目标命名空间描述符样式字符串、7936个不同字面量。
Manifest目标命名空间263项中262项有字面量匹配；余项为activity-alias。
**字面量不是class_def、方法体、调用边或运行记录。**

| 组件声明 | Manifest行 | root.dex字面量文件字节偏移 |
| --- | --- | --- |
| activities.main.MainActivity | 521 | 2,152,173 |
| activities.rungradehome.RunGradeRecordHomeActivity | 563 | 2,182,700 |
| activities.school_run.MapRunActivity | 712 | 2,190,228 |
| activities.school_run_record.RunRecordActivity | 744 | 2,195,296 |
| services.RunService | 763 | 2,441,592 |
| activities.WebViewActivity | 800 | 2,062,632 |
| activities.school_run_record_detail.RunRecordDetailActivity | 923 | 2,197,059 |

上表省略共同前缀com.lptiyu.tanke；偏移是**文件字节偏移**，不是此前DEX invoke的code-unit offset。
组件之间的业务迁移仍未观察，没有直接启动内部组件、调用桥或请求成绩写接口。

同一区域有459处HTTP(S)样式字面量，其中项目域名21处，分布于api2、dev、test、
pre-api、pre-lerun、lerun和vod子域。只保存脱敏路径模板/偏移，丢弃查询/fragment。
环境名称存在不等于当前选中该环境；候选也不是已调用接口，未向任何候选地址发请求。

## 5. 对最终一次验收的意义

已从“只有6个反编译节点”推进到明确的业务组件/记录详情入口候选和其静态来源，
但未恢复业务方法、真实接口协议或得出有效记录。
最终验收转为正常App记录与有效次数闭环，具体条件见 [14-final-acceptance.md](14-final-acceptance.md)。
不会使用本机Mock计数、桥返回或改UI数字替代原App验收。

本轮原件：`.local/research/web-assets-20261103/`，含清单、选择性提取、HTML/JSON结构、
DEX校验/头/map/字面量投影和导出REA Evidence。分析目标已close，investigation已reset。
