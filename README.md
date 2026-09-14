# Assetto Corsa → Forza UDP Bridge

Windows 上的独立遥测转接器：读取原版 Assetto Corsa 1 的共享内存，以 Forza Horizon Dash UDP 格式发送给兼容仪表盘。

本仓库只包含转接器源码，不包含任何第三方仪表盘、游戏文件或预编译 EXE。

## 功能

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

进入 AC 赛道后点击“开始适配”。程序会自动扫描 Steam 库并解包当前车辆；如果无法唯一找到安装位置，可用“游戏目录…”选择包含 `content/cars` 的 AC 根目录。

无界面模式示例：

```powershell
python main.py --headless --config config.example.json
python main.py --headless --simulate --seconds 5
```

## 实时动力估算的含义

扭矩来自车辆文件的静态全负荷 LUT，并按实时油门作线性缩放；功率按 `P = T × RPM × π / 30` 计算。它不是游戏直接输出的瞬时功率，也不是轮胎传到地面的净功率。涡轮、混动和脚本扩展车辆会继续发送基础曲线估算值，同时在界面提示模型限制。

界面的“使用说明”会打开随程序打包的 `help.html`，其中列出完整字段映射、转换公式和无映射字段。

## 已知边界

AC 标准共享内存没有 Forza 的车辆编号、性能等级、实时功率/扭矩等直接对应字段。路肩、积水、表面震动和行车线等字段为零。轮胎滑移定义不同，目前没有冒充为同一种量。

## 测试与构建

```powershell
python -m unittest discover -s tests -v
.\build.ps1 -PythonExe "C:\path\to\conda-env\python.exe"
```

`build.ps1` 会先运行测试，再在本地生成 PyInstaller onedir 产物；构建产物由 `.gitignore` 排除。
