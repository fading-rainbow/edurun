# 公开跑步 IMU 来源与限制

使用[曼海姆大学 RealWorld HAR2016](https://www.uni-mannheim.de/dws/research/projects/activity-recognition/dataset/dataset-realworld/)
及[官方 s1 下载页](https://www.uni-mannheim.de/dws/research/projects/activity-recognition/dataset/dataset-realworld/s1/)
提供的 running 标签数据，仅下载加速度/陀螺仪两个约4MB压缩包。
不下载GPS、声音、视频或参与者照片；原始文件和生成片段均只保存在 `.local/motion/realworld/`。

研究引用：Timo Sztyler、Heiner Stuckenschmidt，*On-body Localization of Wearable Devices:
An Investigation of Position-Aware Activity Recognition*，PerCom2016。
未把数据或采集器源码复制进本仓库，只保留转换器、官方链接与本地固定摘要。

## 单位/时钟依据

- 官方链接的[Android 采集器](https://github.com/sztyler/sensordatacollector/tree/b25336559eabef70ee28e8a40c11fff7a022981d)
  使用TYPE1和TYPE4，写入 `attr_x/y/z/time`；时间取软件接收时钟毫秒，并非硬件事件纳秒。
- [Android 官方传感器单位](https://developer.android.com/develop/sensors-and-location/sensors/sensors_motion)：
  TYPE1含重力加速度，m/s²；TYPE4角速度，rad/s。转换不旋转轴、不改符号、不缩放数值。
- 采集器支持可选去重力滤波，但发布文件没有完整采集配置。
  实际s1腰部开头2秒平均向量模约9.861m/s²，60–65秒约9.796m/s²，支持按含重力通道使用。
  这是基于源码/文件的工作依据，不冒称持有原设备校准证明。
- 压缩包readMe报告约50.02Hz；文件存在软件时钟抖动、突发及约13个大于0.2秒的缺口。
  只选双传感器共同无大缺口区间、跳过初始60秒，以NumPy线性插值对齐为50Hz。
  不跨缺口填补、不外推，不做25Hz抗混叠降采样。

## 重建与检查

```powershell
uv run budao prepare-public-motion
# 默认60秒、3001个采样；下载/缓存必须匹配固定SHA256，已有不同数据拒绝覆盖。
```

本机已生成 `s1-running-waist-60s-50hz.csv` 和来源/质量JSON，MotionClip格式检查通过。
选择的是匿名公开参与者的腰部数据，不是用户本人或当前手机实采；活动标签来自发布者。
MotionClip会循环片段，来源JSON记录接缝六轴差值；这不是完整十分钟连续真人轨迹。
未将该片段注入当前设备：QQ密码交接期间不抢前台、不启动诊断App。
待交接完成才可用独立诊断验证实际接收频率，再核对目标App行为。

```powershell
# 仅示例，执行前必须结束认证交接并确认诊断前台；不是乐跑启动/提交命令。
uv run budao replay-diagnostic --serial emulator-5580 --config .local/routes/replay-demo.json --motion-csv .local/motion/realworld/s1-running-waist-60s-50hz.csv --motion-hz 50 --seconds 30
```

默认25Hz行为保留。50Hz是新可选目标，必须读取实际 `received_hz`；新模式不足目标90%不会判诊断通过。
公开实采片段不会增加缺失的标准计步/步伐检测传感器，不证明里程/打卡/最终有效记录。
在速度、佩戴姿态、时钟、接缝及业务判定实测之前，不能声称“真实跑步模拟已验收”。
