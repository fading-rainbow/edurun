# BudaoRun Lab

**当前是实验环境、诊断工具和控制核心，不是一键生成有效乐跑记录的成品。**

完整方案及阶段状态见 [BudaoRun.md](BudaoRun.md)。QitRun静态原理见
[docs/QitRun-analysis.md](docs/QitRun-analysis.md)。真实路线、APK、配置、截图及密钥均留在 `.local/`，不入库。

## 已可执行

```powershell
uv sync --locked --group dev
uv run pytest -q
uv run ruff check .
uv run budao simulate --runs 30
# 合成地图，不是实际校园路线
uv run budao demo-config --output .local/routes/demo.json
uv run budao validate-route .local/routes/demo.json
```

开始菜单 **BudaoRun** 下有 Android Studio、实验安卓模拟器、停止实验模拟器入口。
也可直接运行：

```powershell
pwsh -NoProfile -File scripts/Start-ExperimentalAVD.ps1 -Visible
uv run budao device --serial emulator-5580
pwsh -NoProfile -File scripts/Build-Diagnostic.ps1
$build = Get-Content .local/setup/diagnostic-build.json -Raw | ConvertFrom-Json
uv run budao probe --serial emulator-5580 --apk $build.apk --seconds 15
pwsh -NoProfile -File scripts/Stop-ExperimentalAVD.ps1
```

API34可见启动现在只在运行时使用4GB（原AVD配置不重写），无窗口诊断默认仍为2GB；
其他变体默认内存不变，可显式传`-MemoryMiB 4096`。停止入口会等待本项目进程/端口退出，
避免刚发出停止请求便重新启动时碰到端口占用；不会强制结束任何用户进程。

诊断只启动本项目诊断App，写操作同时验证 QEMU 和 AVD 名称。不会操作 USB 真机。
控制台认证令牌只在内存中用于 localhost，不记录或上传。

### Android 16 兼容性对照设备

默认入口仍是 API34，不自动切换。另建的 `BudaoRun_API36_Play` 使用官方
API36 Google Play x86_64 镜像、3GB内存、端口5582；保留API34及旧设备的数据。
须先通过SDK Manager安装 `system-images;android-36;google_apis_playstore;x86_64`，
再显式选择设备（不要同时启动两台）：

```powershell
pwsh -NoProfile -File scripts/New-ExperimentalAVD.ps1 -Variant api36
pwsh -NoProfile -File scripts/Start-ExperimentalAVD.ps1 -Variant api36 -Visible
uv run budao device --serial emulator-5582 --avd BudaoRun_API36_Play
uv run budao probe --serial emulator-5582 --avd BudaoRun_API36_Play --apk $build.apk --seconds 15
pwsh -NoProfile -File scripts/Stop-ExperimentalAVD.ps1 -Variant api36
```

创建脚本遇到已有AVD会拒绝重建；CLI必须同时匹配指定AVD名，不会凭端口自动选择其他设备。
API36实际诊断通过（375轮、加速度/陀螺仪约25Hz），仍没有标准计步传感器。
最新版微信8.0.79在该对照设备进入了欢迎页；这不等于账号登录或乐跑认证成功。
但乐跑4.3.0在API36两次启动均被信号终止（exit-info reason2/status24），未到首页，
因此不能把该对照设备当作可用主环境；已切回保留数据的API34继续认证检查。

### 当前 App 的只读观察

```powershell
uv run budao inspect-target --serial emulator-5580
```

仅核对4.3.0/code230的真实MapRunActivity控件：公里→米、用时、已打卡计数及GPS文字。
缺失/重复/隐藏控件、热量模式、版本不同或读取中切换前台时拒绝猜测。
QQ登录/密码页不读取界面树或截图；该命令不点击、不移动、不提交、也不调用模型。
输出的`run_started_proven`及`record_valid_proven`仍为false：读到数字不等于跑步已开始或有效。

API34现已进入过MapRunActivity并实际渲染高德底图，但定位仍显示“搜索中”，
初始化日志有RunHelper空指针，尚未取得可用开始/结束控件或真实记录ID。
因此地图/运行初始化关卡仍未通过；不能把底图渲染当成自由跑完成。

### Android 11 / ARM32 兼容性对照

新增独立 `BudaoRun_API30_Play`（Google Play x86_64 revision10，3GB，端口5584）。
默认仍为API34，原设备及其QQ登录数据未修改、复制或重置。不要同时启动多台：

```powershell
pwsh -NoProfile -File scripts/New-ExperimentalAVD.ps1 -Variant api30
pwsh -NoProfile -File scripts/Start-ExperimentalAVD.ps1 -Variant api30 -Visible
uv run budao device --serial emulator-5584 --avd BudaoRun_API30_Play
pwsh -NoProfile -File scripts/Stop-ExperimentalAVD.ps1 -Variant api30
```

创建前需安装官方 `system-images;android-30;google_apis_playstore;x86_64`。
该设备实际ABI包含ARM32/ARM64，shell为uid2000；未经修改的乐跑4.3.0/code230
指定ARM32安装后已通过协议、引导并进入MainActivity。OS与ABI同时变化，
不能据此断言此前故障的唯一原因，也不代表地图、账号或有效记录已通过。
API34的QQ登录在电脑重启后保留，但乐跑官方QQ授权曾使openSdk进程SIGSEGV；
API34冷启动无焦点的现象在临时3GB内存对照中仍存在，未把增加内存标为修复。
API30上的公开腰部running片段已完成60秒独立诊断：加速度/陀螺仪各3014个事件、
均50Hz，约150.14m诊断位移，最终GPS误差0.043m；标准计步传感器仍缺失。
这仅验证数据接收，不是乐跑里程、打卡或有效记录。

## 路线与模型

### 启动故障诊断状态（2026-10-11）

WHPX和RTX4060实际启用；安卓设置页约1.3s可用，而乐跑出现无焦点窗口ANR。
另有一次RenderThread的fdsan/SIGABRT，不能仅凭该栈认定GPU是唯一原因。
同数据的host/SwiftShader对照均进入过首页，但重复启动仍有失败。
系统还记录过WebView后台更新主动停止目标进程，这一次退出不是目标自身崩溃。

已提供显式、可回滚的[官方开发设备非SDK接口策略](https://developer.android.com/guide/app-compatibility/restrictions-non-sdk-interfaces)：

```powershell
uv run budao startup-compat --serial emulator-5580 --apply
# 如不再测试，恢复首次保存的原策略
uv run budao startup-compat --serial emulator-5580 --restore
```

仅本项目AVD，原策略保存在忽略的`.local/setup/startup-compat/`；不改APK、账号、模拟来源标志，
不重启App、不点击跑步。policy1保留警告，不使用关闭全部检测/日志的policy0。
暖启动对照曾连续三次约6s进入首页，切回默认时40s无焦点；但独立冷启动及重应用仍失败，
因此**这只是诊断开关，不是已验收的修复**；现场已恢复原策略。完整断网对照也仍有启动失败。
真实账号授权、地图初始化、随机三点及有效跑步均未通过，不自动提交记录。

安卓11/ARM32对照使用运行时4GB、暂时停止该设备QQ/Play商店后台后，三次App启动
分别约10.5/6.2/6.2s进入可操作首页；整台模拟器重启后约47s进入界面，首次页面请求失败，
待网络可用点击观察到的“重新加载”后正常显示首页。未改变原APK或复制API34登录数据。
这是当前可打开的对照环境，不证明唯一故障原因、长期稳定、账号授权或地图/跑步有效。
启动入口（先停止API34，勿同时运行两台）：

```powershell
pwsh -NoProfile -File scripts/Start-ExperimentalAVD.ps1 -Variant api30 -Visible -MemoryMiB 4096
```

```powershell
uv run budao editor --port 8765
# 打开 http://127.0.0.1:8765/，绘制区域/通道/基线或导入单条KML/GPX/GeoJSON
# 校验后只生成新的.local/routes/edited-*.json，不覆盖现有路线。
```

编辑器采用本地 Leaflet 1.9.4，默认不请求任何底图。可选OSM底图会请求其瓦片。
导入轨迹只作为基线；需明确添加允许区域及已知通道，不自动认定校园道路或坐标来源。

持续位置/运动回放已独立实现，慢速界面/模型不能阻塞运动线程；移动租约默认2秒，
观察停止后驻留、不追赶。以下命令只启动本项目诊断App，不启动乐跑：

```powershell
uv run budao replay-diagnostic --serial emulator-5580 --config .local/routes/replay-demo.json --seconds 30
# 实采CSV可用 --motion-csv .local/motion/sample.csv
```

CSV格式为 `t_s,ax,ay,az,gx,gy,gz`；加速度含重力、单位m/s²，陀螺仪rad/s。
可执行 `uv run budao prepare-public-motion` 准备有官方来源的公开running腰部片段；
来源、采样时钟、佩戴位置和循环限制见[运动来源说明](docs/motion-provenance.md)。
新参数`--motion-hz 50`只增加受限诊断采样目标，默认25Hz不变；实际接收及目标App接受仍需单独实测。
诊断App按配置请求20–40ms采样；已修复API30不存在`Location.isMock()`导致的崩溃，
旧系统读取原有`isFromMockProvider()`，不改变位置来源标志。
Windows本机实测`Event.wait(20ms)`约30ms、`time.sleep(20ms)`约20ms，
运动线程改用高精度sleep，仍不追赶旧采样，停止响应最多一个20–40ms周期。
默认波形仅证明数据链路，不是人体跑步或计步。诊断App离开前台时自动停止，不抢回界面。
30秒设备回放已通过：约75.08m、最终GPS误差0.123m，实际加速度/陀螺仪事件约25Hz。
这不是乐跑里程、打卡成功或有效记录。

```powershell
uv run budao inspect-track .local/routes/qitrun/huancaochang.kml
uv run budao export-qit-routes .local/apks/candidate-qitrun.apk
```

- KML/GPX/GeoJSON轨迹可读取；完整配置包含WGS84区域、通道、基线路线及速度。
- 使用 `RouteConfig` / `RoutePlanner`，只沿已知通道重规划，越界/不连通时拒绝猜路线。
- `RunController` 实现逐点、两帧确认、里程、广告暂停、提交与有效状态分离。
- `MappedObserver` 组合裁剪截图、地标变化检测、仿射标定和视觉识别。模型不能覆盖App控件确认的里程、打卡数或记录有效性。
- `VisionClient` 使用环境变量 `VISION_BASE_URL`、`VISION_MODEL`、`VISION_API_KEY`。不会读取Codex认证或自动调用付费模型。
- 视觉接口只做HTTP模拟测试，尚未配置或验证真实模型；实际地图锚点和控件选择器尚未采集。
- 暂未提供真实跑步自动提交入口。登录、地图标定、打卡控件和有效性关卡通过后，再接入完整现场执行循环。
- 正式乐跑曾在初次检查时不在开放时段；当前时段与账号规则需登录后重新核对。
  用户后来已允许一次正式乐跑，但实际账号时段及初始化仍需核对；该授权尚未使用。
  自由乐跑通过不替代正式三点记录验收。

## 重建/回滚

官方下载：`python scripts/prepare_android.py all`（固定版本和SHA256，拒绝覆盖不同文件）。
首次创建实验设备：`pwsh -NoProfile -File scripts/New-ExperimentalAVD.ps1`。
已有设备不会强制重建；停止只针对本项目AVD。未改变全局PATH/JAVA_HOME，未重置旧设备或真机App。
临时镜像、签名、APK和结果不会进入GitHub或Cloud测试环境。
