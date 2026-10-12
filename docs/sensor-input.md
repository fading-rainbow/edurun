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

公开真实运动片段的持续传输、丢帧/时钟停顿保护、原始通道与位置/观察的组合、
乐跑实际步频与最终成绩均尚未验收。小幅信号接收成功只证明原始输入通道可用，
不是自然动作、标准计步硬件存在或整个自动跑步产品已经通过。
