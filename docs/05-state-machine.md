# 业务状态机

更新：2026-11-02。状态：未建立当前版本的证据化状态机。

待依次核实：登录/刷新、运动准备/开始/暂停/结束、随机打卡、本人验证、
结果保存/查询和后续审核。未知状态不得补造迁移关系。
历史 App 成绩观察不作为本轮设备状态或后续服务端审核的证明。

## 当前可确认与隔离模型

- 原 App：焦点实际从 launcher → SplashActivity → MainActivity，见
  [正常启动时序](../diagrams/startup-observed.mmd)。没有点击跑步或身份流程。
- 本机 Mock：已演示 ready → running → pending_review → accepted/rejected；
  这是[独立假协议模型](12-local-mock-protocol.md)，不是目标 App 状态机。
- 目标登录刷新、随机打卡、人脸、保存/审核迁移仍未知，不以 Mock 或正常启动替代。
