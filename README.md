# Assetto Corsa / ACC → Forza UDP Bridge

Windows 上的独立遥测转接器：读取原版 Assetto Corsa 1 或 Assetto Corsa Competizione（ACC）的共享内存，以 Forza Horizon Dash UDP 格式发送给兼容仪表盘。

本仓库只包含转接器源码，不包含任何第三方仪表盘、游戏文件或预编译 EXE。

## 功能

- 在界面或 `--game ac|acc` 中明确选择游戏；旧配置继续使用 AC1。两款游戏使用同名共享内存，适配器会检查实际驾驶进程；游戏选错、未启动或两款同时运行时停止驾驶数据。
- ACC 基础转发支持速度、转速、挡位、油门、刹车、离合、归一化转向、圈速、燃油比例、轮速、胎芯温度及原始悬挂行程。ACC 不使用 AC1 的车辆解包逻辑，车辆编号与 AC1 隔离。

以下车辆解包、动力估算及运动坐标转换功能针对 **AC1**：

- 读取 `acpmf_physics`、`acpmf_graphics` 和 `acpmf_static`，默认以 60 Hz 发送 324 字节的 FH4/FH5 Horizon Dash 数据。
- 转发速度、转速、挡位、踏板、转向、燃油、圈速、位置、姿态、轮速、悬挂及胎芯温度等可映射字段。
- 实时增压值由 AC 的 bar 转为 Forza 的 PSI；怠速转速、FWD/RWD/AWD 驱动形式和单侧转向锁角从当前车辆的 `data` 或 `data.acd` 读取。AC 的转向弧度据此自动归一化到 Forza 的 -1～1；离合踏板按 AC 的“1=接合/松开”反向为 Forza 输入值。
- 根据 AC 实车受控动作记录转换运动坐标：局部 X 轴和世界位置 X 轴反向，Y/Z 保持；三个局部角速度分量反向，使其与发送的 Yaw/Pitch/Roll 变化方向一致。
- 自动等待 AC、处理暂停和退出、车辆切换、共享内存停更及断线重连。
- 自动识别当前车型，并从车辆的 `data` 或 `data.acd` 读取悬挂机械行程、转向锁、怠速、驱动形式和全负荷扭矩曲线，不再依赖适配器本地车型档案。
- 行驶时按实时转速在 `power.lut` 中插值，以实时油门缩放扭矩，并按 `P = T × RPM × π / 30` 连续发送估算功率。涡轮、混动、发动机制动、TC 和损伤等动态修正不在该静态曲线模型内。
- 使用稳定的合成车辆编号隔离不同 AC 车型；Forza PI 等无对应信息的字段使用明确的兼容占位值。

## 使用

建议使用 Conda：

```powershell
conda env create -f environment.yml
conda activate ac-forza-bridge
python main.py
```

接收端选择 Forza Horizon Dash 格式，并配置为与窗口中相同的 IPv4 和 UDP 端口。默认是 `127.0.0.1:8094`。

在窗口选择 **AC1** 或 **ACC**，进入所选游戏赛道后点击“开始适配”。程序自动扫描 Steam 库；无法唯一匹配时，选择所选游戏的根目录。AC1 目录应包含 `content/cars`，ACC 目录应包含 `AC2/Content/Paks/AC2-WindowsNoEditor.pak`。

无界面模式示例：

```powershell
python main.py --headless --config config.example.json
python main.py --headless --game acc --snapshot tmp/acc-status.json
python main.py --headless --simulate --seconds 5
```

## 实时动力估算的含义

扭矩来自车辆文件的静态全负荷 LUT，并按实时油门作线性缩放；功率按 `P = T × RPM × π / 30` 计算。它不是游戏直接输出的瞬时功率，也不是轮胎传到地面的净功率。涡轮、混动和脚本扩展车辆会继续发送基础曲线估算值，同时在界面提示模型限制。

界面的“使用说明”会打开随程序打包的 `help.html`，其中列出完整字段映射、转换公式和无映射字段。

## 已知边界

AC 标准共享内存没有 Forza 的车辆编号、性能等级、实时功率/扭矩等直接对应字段。路肩、积水、表面震动和行车线等字段为零。轮胎滑移定义不同，目前没有冒充为同一种量。

ACC 适配依据 [Kunos Shared Memory v1.8.12 文档转载页](https://www.assettocorsamods.net/threads/doc-shared-memory-reference-acc.3061/)，使用已核对的结构体前缀。已通过合成数据、真实 Windows 共享内存及本机 UDP 验证；在 ACC 静止车辆场景中，确认读取车型、转速和持续变化的数据计数器，并在约 2 秒内接收 119 个有效转发包。**尚未完成动态驾驶动作的逐项标定及真实仪表盘验收**，不能保证所有字段的物理语义已验证。

- ACC 的转向字段按文档直接视为 -1～1；离合默认直接映射，使用独立配置 `acc_invert_clutch`（默认 `false`），不继承 AC1 的 `invert_clutch`。实际离合方向仍需实机确认。
- ACC 燃油显示为“原值”，比例按 `fuel / maxFuel` 计算。文档对单位的说明不足以确认其与 AC1 升数语义一致，界面不标作 L。
- ACC 自动从当前车型的 `CarData.uasset` / `.uexp` 读取怠速、断油转速和悬挂限位参考区间。切换车型后按车型 ID 重新匹配，不维护单车硬编码表。共享内存最高转速优先，缺失时才使用文件断油转速。
- 当前资源读取支持已验证的 UE4 PAK v11、Zlib 和 tagged property 结构；仅读取所需车辆资源，不修改、解包整个游戏或保存密钥。索引及资源内容经过完整性校验；未知格式和无法唯一匹配的车型保持缺失提示。可通过 `acc_game_directory` 指定非 Steam 的 ACC 根目录。
- 悬挂使用 `abs(hubFront/hubRear.bumpStop.up.Range - down.Range)` 作为参考分母，发送 `clamp(当前悬挂行程 / 参考区间, 0, 1)`。参考区间的两端不必跨过零点。这是**基于车辆文件的兼容归一化，不是已验证的全机械行程百分比**；默认调校和实际调校可能不同，缓冲块还可继续压缩。无有效参考的轮子仍为零。
- 某些资源省略了等于原生默认值的 `Range`。本机游戏的 `SuspBumpStopDef`、`SuspHubData` 和 `SuspData` 默认构造逻辑已静态核对，默认 Range 为零；仅当游戏 EXE 的 SHA-256 与已核对的版本一致时才补回该值。未知版本不会猜测缺省参数；没有保存密钥或加入单车参数表。
- 本机资源离线检查：56 个 CarData 均可解析断油转速，55 个非 `dummy_car` 车型资源均取得完整参考区间；占位车保持缺失。接收端原版解析器和红线算法验证中，55 组模拟断油序列学到各自的文件断油转速，55 组低悬挂对照没有学习。该结果不等于全车型实机验收或当前接收端版本的认证。
- 接收端红线学习须启用自动监测、解锁当前车型红线，并满足稳定前进挡、油门至少 90%、刹车/离合不超过 5%、四轮参考行程均不低于 10% 等条件，再观察断油回落。怠速、空挡轰油不等于完成学习；仍需在实际接收端完成一次有效的驾驶序列。缺少悬挂参考的未知车型/版本仍会被接收端筛选条件阻止，不用虚构常量绕过。
- ACC 功率、扭矩保持为零；驱动形式使用协议零值占位（会被 Forza 接收端解释为 FWD，不代表真实驱动形式）。不把 `maxPower`/`maxTorque` 当实时动力；动力曲线学习仍不支持。
- ACC 的加速度、局部速度、角速度、姿态、位置及增压缺少实机方向/单位标定，相应 Forza 字段暂为零。原始数据保留在诊断快照 `frame.source_data` 中；玩家位置通过 `carID` 匹配 `playerCarID`，不采用固定数组槽位。找不到唯一匹配时原始位置为 `null`。
- ACC 在暂停、回放、调校菜单、未启动及数据停更时发送非比赛状态。两款游戏不能同时运行；不支持主机版 ACC、EVO 或未知重命名游戏进程。

实机标定时可记录原始数据（不会修改游戏文件）：

```powershell
python -m tools.record_motion --game acc --seconds 60 --output tmp/acc-motion.csv
```

依次保持直行加速、制动、左右转向、换挡及离合动作，结合真实接收端核对数据。记录完成前不要将未标定的字段视为可用的运动平台输入。

## 测试与构建

```powershell
python -m unittest discover -s tests -v
.\build.ps1 -PythonExe "C:\path\to\conda-env\python.exe"
```

`build.ps1` 会先运行测试，再在本地生成 PyInstaller onedir 产物；构建产物由 `.gitignore` 排除。
