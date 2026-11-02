# 本机假数据协议与状态机验收

更新：2026-11-03。**独立研究模型，不是步道乐跑协议、接口或有效成绩实现。**

用标准库 HTTPServer、HMAC/hashlib 和项目已有 httpx 组装，没有新增依赖。
只绑定 127.0.0.1 临时端口，结束自动关闭；不接触 Android、真实服务或 VPS。

```powershell
uv run budao research-mock-demo
```

该命令已实际走本机 HTTP，而非仅直接调用函数：

| 场景 | HTTP / 返回 | 模型有效计数 |
| --- | --- | --- |
| 缺少假身份状态就开始 | 412 / IDENTITY_REQUIRED | 0 |
| identity fixture 后正常开始 | 200 / RUNNING | 0 |
| 复用签名请求 nonce | 409 / REPLAY | 0 |
| 提前提交 valid=true | 412 / INCOMPLETE | 0 |
| 三个假点/进度后结束 | 200 / PENDING_REVIEW | **仍为0** |
| review fixture 接受 | 200 / ACCEPTED | 1，仅本机假模型 |
| 重复审核 | 409 / WRONG_STATE | 保持1，不重复计数 |

真实 App 创建/提交记录为 **0**。摘要存本机 mock-http-demo.json，不输出 key、签名、nonce 或正文。

## 设计与边界

- 路由固定 /mock/ 前缀，不复制或猜测真实服务路径。
- bootstrap 只生成假会话及内存随机演示 key，不读取磁盘凭据。
- HMAC 绑定 method/path/time/nonce/body SHA-256；时间窗±120秒，nonce 重放拒绝，数量/正文有限制。
- 进度必须有限、范围内、不倒退；假点按 mock-a/b/c 顺序。
- 1000m、三点、120–900s/km 是**人为模型参数，不是恢复的学校规则**。
- identity-fixture/review-fixture 是假数据入口，不是人脸识别或真实服务端审核。
- 客户端有效声明不直接计数，pending_review/accepted 分离，审核只计一次。
- 无访问日志/密钥/真实位置保存；HTTP 客户端不使用环境代理，始终 loopback。

实现：src/budao/research_mock.py；测试：tests/test_research_mock.py。
客户端为 Python/httpx 实际 HTTP 演示，**Android Demo 尚未构建**，不能冒称 Android 验收。
原 App 的状态机仍须正常取证，不由 Mock 补全。

图：[假状态机](../diagrams/mock-state-machine.mmd)、[模型边界](../diagrams/mock-trust-boundary.mmd)。
已用现有Mermaid CLI实际渲染SVG/PNG，调整重叠布局后完成视觉检查。
本模型只复现“客户端声称成功但服务端未接受”的区别，不推定目标签名、防重放、本人验证或审核实现。
