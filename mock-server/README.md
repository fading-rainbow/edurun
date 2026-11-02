# 本地 Mock API（已实现假数据演示）

运行 `uv run budao research-mock-demo`，实际启动回环临时 HTTP 服务并完成签名、防重放、
假身份/打卡/审核场景，结束后自动关闭。实现位于 `src/budao/research_mock.py`。
不连接生产接口、不操作 Android 或真实记录，规则不是已恢复的目标协议。
详见[模型验收](../docs/12-local-mock-protocol.md)。
