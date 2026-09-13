# Assetto Corsa → Forza UDP Bridge

Windows 上的独立遥测转接器：读取原版 Assetto Corsa 1 的共享内存，以 Forza Horizon Dash UDP 格式发送给兼容仪表盘。

本仓库只包含转接器源码，不包含任何第三方仪表盘、游戏文件或预编译 EXE。

## 功能

- 读取 `acpmf_physics`、`acpmf_graphics` 和 `acpmf_static`，默认以 60 Hz 发送 FH4/FH5 Dash 数据。
- 转发速度、转速、挡位、踏板、转向、燃油、圈速、位置、姿态、轮速、悬挂及胎芯温度等可映射字段。
- 自动等待 AC、处理暂停和退出、车辆切换、共享内存停更及断线重连。
- 自动识别当前车型，并从车辆的 `data` 或 `data.acd` 读取自然吸气全负荷扭矩曲线。
- “完整动力学习（模拟）”会暂停真实转发，模拟一次从怠速到断油的连续拉转，再恢复真实转发。它不检测接收端，也不保证接收端已经保存。
- 使用稳定的合成车辆编号隔离不同 AC 车型；Forza PI 等无对应信息的字段使用明确的兼容占位值。

## 使用

建议使用 Conda：

```powershell
conda env create -f environment.yml
conda activate ac-forza-bridge
python main.py
```

接收端选择 Forza Horizon Dash 格式，并配置为与窗口中相同的 IPv4 和 UDP 端口。默认是 `127.0.0.1:8094`。

进入 AC 赛道后点击“开始适配”。需要建立动力曲线时点击“完整动力学习（模拟）”。程序会自动扫描 Steam 库；如果无法唯一找到安装位置，可用“游戏目录…”选择包含 `content/cars` 的 AC 根目录。

无界面模式示例：

```powershell
python main.py --headless --config config.example.json
python main.py --headless --simulate --seconds 5
```

## 动力学习的含义

模拟扭矩来自车辆文件的静态全负荷 LUT，功率按 `P = T × RPM × π / 30` 计算。它不是实时实测功率，也不是轮胎传到地面的净功率。当前版本会拒绝涡轮、混动和脚本扩展动力模型，避免把基础 LUT 错当成完整输出。

学习序列满足已分析接收器的典型采样条件：固定前进挡、全油门、有效悬挂、连续递增 RPM、功率与扭矩一致，并在最高转后发送回落、收油和非比赛状态。接收端是否启用学习、怎样保存以及怎样处理旧曲线，由接收端自行决定。

## 已知边界

AC 标准共享内存没有 Forza 的车辆编号、性能等级、实时功率/扭矩等直接对应字段。路肩、积水、表面震动和行车线等字段为零。轮胎滑移定义不同，目前没有冒充为同一种量。

## 测试与构建

```powershell
python -m unittest discover -s tests -v
.\build.ps1 -PythonExe "C:\path\to\conda-env\python.exe"
```

`build.ps1` 会先运行测试，再在本地生成 PyInstaller onedir 产物；构建产物由 `.gitignore` 排除。

## 许可

项目采用 GPL-2.0。`acbridge/vendor/acd.py` 来自 [philippkosarev/acd](https://github.com/philippkosarev/acd)，其原许可证保存在 `acbridge/vendor/ACD-LICENSE.txt`。
