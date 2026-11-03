# 研究图表

已有 [loader-boundary.mmd](loader-boundary.mmd)，仅覆盖当前 APK 的最小静态 Java/native 加载链；
证据、边的性质和未知项见 [加载边界报告](../docs/10-loader-boundary.md)。
后续正常业务时序、业务模块和客户端/服务器信任边界图仍待证据；不能用该 Loader 图替代。

- [startup-observed.mmd](startup-observed.mmd)：实际启动、焦点、受限读取时序，无业务动作。
- [mock-state-machine.mmd](mock-state-machine.mmd)：独立假数据状态机，不是原App协议。
- [mock-trust-boundary.mmd](mock-trust-boundary.mmd)：本机模型边界，不是真实服务器边界。
- [web-assets-components.mmd](web-assets-components.mmd)：离线文件、普通索引及字面量关联，虚线不是实际调用。
