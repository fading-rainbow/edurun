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

诊断只启动本项目诊断App，写操作同时验证 QEMU 和 AVD 名称。不会操作 USB 真机。
控制台认证令牌只在内存中用于 localhost，不记录或上传。

## 路线与模型

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
- 正式乐跑当前不在开放时段，后续先用自由乐跑做有限现场测试；自由乐跑通过不替代正式三点记录验收。

## 重建/回滚

官方下载：`python scripts/prepare_android.py all`（固定版本和SHA256，拒绝覆盖不同文件）。
首次创建实验设备：`pwsh -NoProfile -File scripts/New-ExperimentalAVD.ps1`。
已有设备不会强制重建；停止只针对本项目AVD。未改变全局PATH/JAVA_HOME，未重置旧设备或真机App。
临时镜像、签名、APK和结果不会进入GitHub或Cloud测试环境。
