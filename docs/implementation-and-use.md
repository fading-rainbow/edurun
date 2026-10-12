# 实现、启动方式与独立账号环境

## 当前结论

一次代理调度的现场正式流程已完成，目标App的成绩页明确计入有效次数、有效里程和时长。
不是只看到地图、上传提示或合成测试。原始截图、账号、照片、路线和记录只在本机忽略目录。

**这是已跑通一次的实验原型，不是独立一键无人值守成品。**
此次详情中的步数为0；MuMu自然运动/步频输入尚未解决。当前有效计数不能保证后续审核、
其他账号、其他学校或未来版本同样通过。不要把QEMU的传感器诊断成功混同于MuMu支持。

## 现场链路

1. **环境隔离。** 原Google AVD保留；另建官方签名MuMu的Android12实例。
   root关闭，不关闭Hyper-V，不改变真机。使用原始乐跑/QQ包，用户正常登录，未搬运登录数据。
2. **位置链路。** MuMu当前版本的官方位置入口经静止对照确认采用BD09输入。
   现场用现成`eviltransform`将KML规范WGS84转换，再核对接收坐标。
   没有修改模拟来源标志、GNSS元数据或App。
   实际位置输出也已封装为`MuMuBridge.location_wgs84(GeoPoint)`供开发使用；
   每次仍检查实例归属、root及实际uid/SDK，不能把这个底层方法当一键跑步入口。
3. **实时运动。** `Playback`以单调时钟沿连续路线推进，约1Hz发送位置，现场速度3m/s。
   停顿超过阈值不补发瞬移；广告、外部包、模式不明、验证中断或暂停时不继续移动。
   App显示的实际里程是验收依据，不是脚本累计值。
4. **顺序打卡。** 正式卡片先给出“再跑N米后分配”，之后给出名称、距当前点的米数及状态。
   本地`parse_checkpoint_cards`读取这些字段，完成计数须与卡片顺序一致。
   现场利用已知路线上的多点距离估计接近方向，再以App连续两帧计数增长确认打卡。
   这些估计是现场辅助，不是已交付的独立高精度定位器；不能把估计位置当打卡成功。
5. **人脸中断。** App正常提示后自动暂停。用户提供本人照片，经MuMu自带图像输入、
   App正常拍摄/裁切/识别流程返回成功；没有修改识别结果或复制认证目录。
   照片未发送到MonToken/视觉API，也没有进入源码。
   返回MapRun后App自动恢复计时，必须重新检查，不能假设仍暂停。
6. **结束及验证。** 全部三点和App最低里程满足后暂停。仅在用户明确授权本次提交后，
   通过App正常结束/保存上传，关闭已知广告，并读取正式成绩页验证计入有效次数。
   当前命令行入口没有自动上传功能，项目默认提交约束不变。

## 为什么不需要外部大模型

用户确认候选点约7–8个，并非任意无限地点。卡片提供准确名称，可将经过现场核对的
“名称→坐标→对应路网”放入本机目录，使用NetworkX/Shapely/PyProj/GeographicLib选择或拼接路线。
未知名称停止观察，不能套用旧点；不能按固定时间假定打卡出现。

当前卡片观察器及GIS/时钟核心已实现；**完整点目录、MuMu实时驱动整合和无人值守调度仍待封装**。
本次有Codex参与路线决策和界面调度，没有创建MonToken密钥，也没有付费模型API调用。

## 当前确实可用的启动命令

本机现有环境：

```powershell
cd E:\edurun
uv sync --locked --group dev
uv run budao start-mumu
uv run budao inspect-mumu-target
uv run budao editor --port 8765
# 使用结束且没有未保存运行时，停止的只是本项目实例
uv run budao stop-mumu
```

`start-mumu`只启动/显示本机所有权凭据确认的实例，不点击“开始乐跑”；
启动后接回SDK到已核对的localhost端口，实际uid/SDK核对完成才允许ADB操作。
不连接USB真机、不杀全局ADB、不清数据，不重新启动已经运行的实例。

`inspect-mumu-target`只读App4.3.0正式运行页，输出里程、用时、卡片状态或人脸中断提示。
首页、账号/摄像头页、未知版本或广告遮挡时不会当作正在跑步，也不会读取认证图片。

路线编辑器地址为`http://127.0.0.1:8765/`。它是路线准备工具，不是正式跑步开始按钮。
当前没有`budao run`一键正式执行入口；本机现场实验脚本并非可直接跨账号复用的产品。

### 后台离线预检与路线建议

游戏或其他程序在前台时，可先做以下离线工作；这些命令不连接设备、不启动模拟器、
不点击App、不移动位置、不读取照片内容，也不提交跑步：

```powershell
uv run budao preflight-profile --profile .local/profiles/current/profile.json
uv run budao plan-snapshot `
  --profile .local/profiles/current/profile.json `
  --snapshot .local/profiles/current/snapshot.xml `
  --position .local/profiles/current/position.json `
  --output .local/profiles/current/proposal-new.json
```

这些是需要本机配置的入口，不会自动生成可执行校园路线。`profile.json`结构示例：

```json
{
  "profile_id": "current",
  "site_id": "my_campus",
  "receipt_file": ".local/setup/mumu-owned-instance.json",
  "route_file": ".local/profiles/current/roads.json",
  "catalog_file": ".local/profiles/current/catalog.json",
  "upload_policy": "manual",
  "operator_roads_verified": false
}
```

`roads.json`采用路线编辑器的`RouteConfig`结构：WGS84区域、通道、闭合等待路线、速度及
里程阈值。`position.json`是`{"lon": 经度, "lat": 纬度}`；必须采用与快照对应的位置，
不是模型生成坐标。`snapshot.xml`只接受运行卡片快照，不应保存密码/认证页面。

目录字段为`coordinate_system: "WGS84"`、`site_id`、`expected_candidate_count`、
`candidate_count_verified`及`entries`；每项包含完整`name`、`approach: {lon, lat}`、
可选`confirmation_evidence`（本机历史确认JSON及同名XML）。`approach`是得到过App确认的
接近位置，**不冒充精确标记中心**。声明“预计8个”但未核实总数不会判定目录完整。

预检检查配置隔离、声明的设备创建凭据、通道连通性及匹配的两帧历史打卡证据；
不会把凭据格式通过冒充实际设备身份核验，`ready_for_unattended_run`仍为false。
离线建议按当前快照的完整名称查目录，不按时间猜测点位；未知点、缺证据、越界或
人脸提示返回`hold`，未分配返回`observe`。满足结束阈值只返回`review_finish`。
快照无法证明当前运行状态、暂停状态、会话身份或时效，因此任何建议始终
`execution_allowed: false`、`upload_allowed: false`，不能直接驱动设备。
建议中的坐标仅写入新建私有文件，控制台只输出摘要，不覆盖已有文件。

其他电脑从公开仓库开始，只能先验证源码：

```powershell
git clone https://github.com/fading-rainbow/edurun.git
cd edurun
uv sync --locked --group dev
uv run pytest -q
uv run ruff check .
```

公开仓库不含MuMu安装包、安卓镜像、已登录数据、所有权凭据、自拍或实际路线。
另一台电脑须正常安装软件并创建新的独立实例，不能把本机`.local`直接当可移植环境。

## 多个账号：软件模板，而不是账号数据克隆

建议保留当前已验证环境，另外建立**未登录的基础模板**：同版本系统、同版本原始APK、
root关闭、同样的显示/性能配置，但不包含QQ会话、乐跑账号、照片或历史。
每个账号使用独立实例、独立userdata、ADB端口、配置与照片目录，正常重新认证。

克隆后必须实际核对系统/应用设备标识是否独立；“实例名称不同”不等于安卓身份不同。
如果厂商克隆保留这些标识，采用官方新建实例后复用软件配置，不手工伪造来源标志。
只安装相同版本APK，不等于复制登录数据。

目前适配器严格只允许已核对的现场项目实例；**多人管理及未登录模板制作尚未实施**。
在给其他实例发写命令前，还须扩展并验证其创建凭据、路径、索引、端口、root及实际uid守卫。
不能改一个JSON名字便把旧实例当独立新设备。
离线配置为其他账号保留`BudaoRun_profile_<profile_id>`命名空间，但当前实时适配器
仍不支持这些实例；预检的`live_bridge_supported`会明确返回false。

在约16GB主机上优先逐台启动，不默认同时运行多个3GB实例；镜像留本机，不占用VPS。
账号密码通过正常登录流程输入，照片和配置留本机，不写源码、Git提交或Cloud任务。

## 已验证与未验证的边界

- 已验证：启动兼容、当前账号认证、位置被App累计、三点顺序确认、正常照片验证和当前有效成绩计数。
- 未完成：MuMu自然步频输入、一键运行调度、全部固定候选点目录、独立账号模板/实例管理。
- 开源的是MIT许可的源码与测试，不是可直接导入的已登录镜像；第三方许可证保留。
