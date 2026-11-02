# 网络与接口清单

更新：2026-11-03。状态：已有网络配置及静态资源候选；尚无确认的业务请求/响应。

本轮没有配置代理、安装 CA 或捕获任何 App 请求。
静态候选与动态已观察接口将分表记录，不把域名/路径字符串直接当作实际接口。

待记录字段：相对时间、正常 UI 操作、HTTP 方法、脱敏路径模板、字段类型、
认证类别、状态码、状态迁移、证据位置及置信度。

2026-11-03 加载边界静态分析只识别系统属性、本地文件头读取和库加载。
这些调用不是 HTTP 接口；本轮没有新增已观察网络请求，不从 Loader 类名推定业务网络栈。

## 当前证据清单

详见 [11-runtime-and-network-policy.md](11-runtime-and-network-policy.md)。

| 层次 | 发现 | 不可扩大为 |
| --- | --- | --- |
| Manifest | usesCleartextTraffic=true；networkSecurityConfig=@0x7f14000b | 实际明文传输 |
| XML | res/4uC.xml；base仅system CA；domain文本带scheme | 用户CA已被真实连接接受 |
| XML 域文本 | `https://api2.lptiyu.com` | 已观察API endpoint、请求或有效hostname规则 |
| 带值资源 | auth.snapchat.com 1处；www.mob.com 7处 | 跑步API或SDK实际调用 |
| 脱敏PID日志 | 454行有效格式；API2候选0，其他主机出现2次但值省略 | 无API通信，或已确认HTTP方法/路径/响应 |

**已观察业务请求表仍为空。**未设置代理/CA，未保存请求原件、认证头或真实轨迹。
