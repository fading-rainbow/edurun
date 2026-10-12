# MuMu原始传感器通道：现场诊断，不等于目标步频

## 已经实际验证的范围

只操作本项目独立MuMu Android12/SDK32，root关闭、ADB uid2000。乐跑进程保持停止，
没有新跑步、位置输出或记录提交。诊断APK保留原有签名和数据，仅更新自有代码。

- 官方`gyro`的四次有界对照改变了加速度方向，但模长始终约9.80254m/s²。
  它不能据此当成任意加速度幅度控制。当前实测角速度也随命令变化；命令名/帮助文案
  不足以建立完整物理映射。测试后输入三轴0，实际恢复原加速度和三轴0角速度。
- 普通目标SDK34的自有诊断App能访问`initDataInjection`和`injectSensorData`。
  没有放宽hidden API策略、setAccessible、root、修改乐跑APK或来源标志。
- Shell的DUMP入口进入了实际顶层`DATA_INJECTION : com.edurun.diagnostic`模式。
  不能把命令退出码、设备声明的data-injection标志当成接收证明。
- 一秒25Hz的小幅合成测试分别发送25个加速度、25个角速度样本。
  **两类各25个样本都在独立回调中匹配到对应时间戳及三轴值**，容许实测加速度
  量化误差0.0001m/s²。这里的测试信号不是自然跑步或HAR记录。
- 退出后读取顶层`Mode : NORMAL`，再次启动普通诊断并看到两类事件计数、时间戳继续
  增长。标准StepCounter/StepDetector依然不存在。历史正式记录步数0没有因此被修复。

接口来源是固定版本的[AOSP SensorService](https://android.googlesource.com/platform/frameworks/native/+/android-12.1.0_r1/services/sensorservice/SensorService.cpp)
和[SensorManager](https://android.googlesource.com/platform/frameworks/base/+/android-12.1.0_r1/core/java/android/hardware/SensorManager.java)，
厂商实现是否支持以实际模式和回调为准。传感器物理单位参见
[Android运动传感器文档](https://developer.android.com/develop/sensors-and-location/sensors/sensors_motion)。

## 清理问题与实际修复

首次测试关闭注入API再执行`enable`后，顶层仍显示`DATA_INJECTION`空白名单，而普通
客户端显示NORMAL。这不是恢复成功：**不得用客户端Operating Mode替代顶层Mode**。
先停止自有诊断客户端，再`enable`，随后顶层模式和实际普通事件流均恢复。
现在探测器的finally始终按此顺序清理；失败/恢复未通过会返回`passed:false`，不继续驱动。

## 开发诊断入口

先正常启动独立项目MuMu，构建、在该实例更新和打开自有诊断App，确保乐跑进程已停止：

```powershell
pwsh -NoProfile -File scripts/Build-Diagnostic.ps1
# 用经归属守卫的MuMuBridge安装本机构建，并调用diagnostic_start()
uv run budao probe-mumu-sensors --output .local/runs/raw-sensor-probe-new.json
```

输出只能写在本项目`.local/`，旧证据不覆盖；终端不输出原始传感器回调。
已运行乐跑、未知/已有注入模式或不可访问API时，拒绝开始实验。探测只使用自有客户端
和固定小幅信号，不支持任意包名、任意数据或设备。该入口也不启动乐跑、QQ或微信。

异常中断的检查/回退仅限该独立实例：用MuMuBridge核实归属，确认乐跑未运行，停止
`com.edurun.diagnostic`，执行`dumpsys sensorservice enable`，读取**顶层NORMAL**并重开
普通诊断检查事件恢复。不要直接用USB序列号或全局ADB操作，不覆盖其他客户端实验。

## 下一步，不能提前认定通过

### 有来源的公开跑步片段实测

新增`replay-mumu-motion`：先验证两个官方源压缩包的固定SHA256，再按原转换器重建
全CSV并逐字节比较；不能只改来源JSON标签/摘要就冒充公开实采数据。
仍然是匿名公开参与者的腰部片段，不是本用户、本手机或完整十分钟真实动作。

- 5秒片段实际收到加速度、角速度**各251个**对应样本，匹配时间戳与全部三轴值，
  有效接收频率约48.90Hz。10秒片段两类**各501个**全部匹配，约48.80Hz。
  50Hz是目标，接收达到目标90%且无超过100ms的间断才通过。
- 首次原型只匹配248/251，未判通过：快照及大文本布局挤压了主线程接收队列。
  工作线程发送、运行中只输出计数、结束后输出完整证据，并缩短界面显示后，
  两类样本均全量匹配；没有降低匹配数量要求来掩盖丢帧。
- 每次调用绑定新随机请求号与实际输入文件摘要，旧快照不能作为本次接收证据。
  所有发送必须对应CSV连续编号/三轴值；不改轴、缩放、循环或补发积压。
- 固定250ms时钟中断实验在各25个样本后终止，匹配终止前所有样本，
  `clock_gap_stop_verified:true`，`clip_received_complete:false`。保护实验通过只代表
  停顿后没有追赶输出且清理恢复成功，不是整段回放、步频或完整跑步通过。
- 所有上述实验结束后均恢复顶层NORMAL，普通事件计数及时间戳重新增长；乐跑
  进程保持停止，没有位置输出、新正式跑步或提交记录。

```powershell
# 要求自有诊断APK已更新并运行，乐跑已停止；每次使用新的私有证据文件。
uv run budao replay-mumu-motion --seconds 5 --output .local/runs/real-imu-new.json
uv run budao replay-mumu-motion --seconds 10 --output .local/runs/real-imu-10s-new.json
uv run budao replay-mumu-motion --stall-test --output .local/runs/real-imu-stall-new.json
```

原始证据只保存在本机，原始源文件、APK或事件数组不会同步到公开仓库。
尚未验收：长时运动租约、原始通道与位置/目标前台观察的组合、乐跑实际步频、
随机点完整无人值守流程与新的最终成绩。短公开片段实际接收通过不能替代这些关卡，
也不会增加缺失的标准计步传感器。
