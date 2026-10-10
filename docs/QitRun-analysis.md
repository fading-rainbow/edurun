# QitRun 1.0 静态分析

证据：2026-10-10 从用户手机只读复制的安装包，`aapt`、DEX字符串及SDK `dexdump` 检查。
没有运行旧脚本、没有注入真机位置、没有读取其私有账号数据。

- 包名：`com.langqi.fakegps`，versionCode=1，minSdk=26，targetSdk=34。
- APK SHA256：`edf7a5c7e60390aae838ef01227ad5dfa437ffc2318622a696a9b72c69b82bef`。
- 主业务位于 `classes3.dex` 的 `FakeGPSActivity`、`LocationMockService`。
- APK没有assets和原生库；内置三条KML位于res/raw。真实坐标文件只保存在.local。

## 已确认链路

KML坐标 → 逐段累计距离 → `getCoordinateByDisplacement` 插值 → 按速度/时间推进
→ 广播 → 前台服务 → `LocationManager` 的GPS及Network测试提供器。

界面支持KML导入、重复次数、0.2秒默认广播间隔、开始/暂停/恢复/终止。
`startProcessing`和`startLocationUpdates`会将速度设为2.7–3.3m/s，每约3秒改一次，覆盖手动速度框。
业务还添加约米级位置扰动；这不是传感器输入，也不能证明绕过任何判定。

## 误标与缺失能力

界面“加速度值”对应`inputAcceleration`和`acc`字段，最终传给`Location.setAccuracy`。
因此该项是GPS精度参数，**不是加速度、步伐或系统步数**。
界面“海拔”是位置海拔；速度单位m/s，间隔单位秒。

在检查的业务代码中没有发现SensorManager/SensorEvent、无障碍服务、目标App控制或动态打卡识别。
不能仅用依赖库里出现的SensorManager字符串推断其具有运动模拟能力。

## 未做运行证明

服务动态注册定位广播接收器，且静态代码使用exported标志；可能存在外部控制接口。
尚未验证电脑端广播驱动、Android16生命周期兼容性、当前乐跑接受程度。
GPS和Network测试提供器不等于真实GNSS硬件。去年成功不能替代当前版本验收。
本实现优先使用模拟器官方控制接口，不修改或重新打包旧APK。
