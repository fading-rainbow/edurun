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
2026-10-11在独立API30对照设备完成60秒诊断回放：两传感器各3014个事件、均50Hz，
驱动3022次，诊断位移150.139m，GPS最终误差0.043m；没有提交记录。
原API34的QQ登录数据保持原位，未迁移；本次没有在用户输入密码时抢前台。
转换器来源JSON仍代表准备阶段；设备接收结果另存`.local/runs/`，不修改来源证明或上传原始数据。
实际乐跑接受度、计步和有效性依然未证明。

2026-10-13在独立MuMu实际完成有来源片段的原始通道诊断：5秒两类各251个样本、
10秒两类各501个样本全量时间戳/三轴匹配，约48.90/48.80Hz；250ms停顿会终止而不追赶。
清理后顶层NORMAL及普通事件增长均已复核，期间乐跑未运行，没有位置输出或记录提交。
该新结果仍只说明公开片段接收，不证明用户自身运动、目标步频或完整跑步。
详见[原始传感器实测](sensor-input.md)。

```powershell
# 仅示例，执行前必须结束认证交接并确认诊断前台；不是乐跑启动/提交命令。
uv run budao replay-diagnostic --serial emulator-5580 --config .local/routes/replay-demo.json --motion-csv .local/motion/realworld/s1-running-waist-60s-50hz.csv --motion-hz 50 --seconds 30
# 已实测的API30对照需显式指定名称与端口（先停止API34）：
uv run budao replay-diagnostic --serial emulator-5584 --avd BudaoRun_API30_Play --config .local/routes/replay-demo.json --motion-csv .local/motion/realworld/s1-running-waist-60s-50hz.csv --motion-hz 50 --seconds 60
```

默认25Hz行为保留。50Hz是新可选目标，必须读取实际 `received_hz`；新模式不足目标90%不会判诊断通过。
公开实采片段不会增加缺失的标准计步/步伐检测传感器，不证明里程/打卡/最终有效记录。
在速度、佩戴姿态、时钟、接缝及业务判定实测之前，不能声称“真实跑步模拟已验收”。
