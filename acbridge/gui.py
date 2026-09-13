from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from .runtime import Bridge, load_config, save_config, DEFAULTS


class App:
    def __init__(self, root, base):
        self.root, self.base = root, base
        self.path = base / "config.json"
        self.bridge = None
        self.closing = False
        root.title("神力科莎 → Forza · 实时遥测适配器（8094）")
        root.geometry("760x670")
        root.minsize(700, 640)
        root.configure(bg="#101723")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#101723")
        style.configure("TLabel", background="#101723", foreground="#e6edf7", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"), foreground="#62ded0")
        style.configure("Sub.TLabel", foreground="#9cacbf", font=("Microsoft YaHei UI", 9))
        style.configure("Value.TLabel", font=("Consolas", 19, "bold"), foreground="#62ded0")
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 8))
        style.configure("TCheckbutton", background="#101723", foreground="#e6edf7", font=("Microsoft YaHei UI", 9))
        style.map("TCheckbutton", background=[("active", "#26384c")])
        style.configure("TEntry", padding=5)
        box = ttk.Frame(root, padding=22)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="ASSETTO CORSA  →  DASHBOARD", style="Title.TLabel").pack(anchor="w")
        ttk.Label(box, text="AC1 共享内存  /  Forza Horizon UDP  /  本机独立配置", style="Sub.TLabel").pack(anchor="w", pady=(5, 16))
        try:
            self.config = load_config(self.path)
        except (ValueError, OSError, TypeError) as exc:
            self.config = dict(DEFAULTS)
            messagebox.showwarning("配置读取失败", f"{exc}\n已显示默认值；点击开始时才会保存。", parent=root)
        fields = ttk.Frame(box)
        fields.pack(fill="x")
        self.host = tk.StringVar(value=self.config["host"])
        self.port = tk.StringVar(value=str(self.config["port"]))
        self.hz = tk.StringVar(value=str(self.config["hz"]))
        self.fmt = tk.StringVar(value=self.config["packet_format"])
        self.inputs = []
        for col, (label, var, width) in enumerate((("目标 IPv4", self.host, 17), ("UDP 端口", self.port, 8), ("频率 Hz", self.hz, 7))):
            ttk.Label(fields, text=label).grid(row=0, column=col, sticky="w", padx=(0, 14))
            entry = ttk.Entry(fields, textvariable=var, width=width)
            entry.grid(row=1, column=col, sticky="w", padx=(0, 14), pady=(6, 0))
            self.inputs.append(entry)
        ttk.Label(fields, text="Horizon 格式").grid(row=0, column=3, sticky="w")
        combo = ttk.Combobox(fields, textvariable=self.fmt, values=("fh4", "fh5"), width=8, state="readonly")
        combo.grid(row=1, column=3, pady=(6, 0))
        self.inputs.append(combo)
        options = ttk.Frame(box)
        options.pack(fill="x", pady=14)
        self.sim = tk.BooleanVar(value=False)
        self.clutch = tk.BooleanVar(value=self.config["invert_clutch"])
        self.steer = tk.BooleanVar(value=self.config["invert_steer"])
        for text, var in (("模拟测试（非实车）", self.sim), ("离合反向（AC 默认）", self.clutch), ("转向反向", self.steer)):
            cb = ttk.Checkbutton(options, text=text, variable=var)
            cb.pack(side="left", padx=(0, 15))
            self.inputs.append(cb)
        buttons = ttk.Frame(box)
        buttons.pack(fill="x", pady=(0, 14))
        self.start_button = ttk.Button(buttons, text="开始适配", command=self.start)
        self.start_button.pack(side="left", padx=(0, 10))
        self.stop_button = ttk.Button(buttons, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 10))
        ttk.Button(buttons, text="使用说明", command=self.help).pack(side="left")
        ttk.Button(box, text="游戏目录…", command=self.choose_game).pack(anchor="w", pady=(0, 6))
        self.status = tk.StringVar(value="就绪 · 配置接收端地址后开始适配")
        ttk.Label(box, textvariable=self.status, wraplength=690).pack(anchor="w", pady=(0, 7))
        self.car = tk.StringVar(value="车辆 / 赛道：—")
        ttk.Label(box, textvariable=self.car, style="Sub.TLabel", wraplength=690).pack(anchor="w")
        values = ttk.Frame(box)
        values.pack(fill="x", pady=18)
        self.value_vars = {}
        for col, (key, label) in enumerate((("speed", "速度 km/h"), ("rpm", "发动机 RPM"), ("gear", "挡位"), ("fuel", "燃油 L"))):
            values.columnconfigure(col, weight=1)
            ttk.Label(values, text=label, style="Sub.TLabel").grid(row=0, column=col, sticky="w")
            var = tk.StringVar(value="—")
            ttk.Label(values, textvariable=var, style="Value.TLabel").grid(row=1, column=col, sticky="w", pady=5)
            self.value_vars[key] = var
        self.pedals = tk.StringVar(value="油门 —    刹车 —    离合 —    转向 —")
        ttk.Label(box, textvariable=self.pedals).pack(anchor="w")
        self.stats = tk.StringVar(value="已发送 0 包 · UDP 发送成功不等于仪表盘已接收")
        ttk.Label(box, textvariable=self.stats, style="Sub.TLabel").pack(anchor="w", pady=9)
        self.error = tk.StringVar()
        ttk.Label(box, textvariable=self.error, foreground="#ffbd86", wraplength=690).pack(anchor="w")
        ttk.Separator(box).pack(fill="x", pady=13)
        ttk.Label(box, text="接收端选择 Forza Horizon，IP / 端口与此处一致。\n游戏未启动时自动等待；暂停、退出或数据停更后自动停止驾驶数据。\n车辆切换时自动解包悬挂、转向、怠速、驱动形式和动力曲线。", style="Sub.TLabel", wraplength=690).pack(anchor="w")
        # Tk geometry uses pixels while fonts follow Windows DPI. Fit requested content.
        root.update_idletasks()
        width = max(760, box.winfo_reqwidth())
        height = max(670, box.winfo_reqheight())
        root.minsize(width, height)
        root.geometry(f"{width}x{height}")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.refresh)

    def start(self):
        try:
            cfg = {**self.config, "host": self.host.get().strip(), "port": int(self.port.get()), "hz": int(self.hz.get()),
                   "packet_format": self.fmt.get(), "invert_clutch": self.clutch.get(), "invert_steer": self.steer.get()}
            bridge = Bridge(cfg, self.sim.get(), base=self.base)
            save_config(self.path, bridge.config)
            self.config, self.bridge = bridge.config, bridge
            self.error.set("")
            for widget in self.inputs:
                widget.configure(state="disabled")
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            bridge.start()
        except (ValueError, OSError, TypeError) as exc:
            messagebox.showerror("无法启动", str(exc), parent=self.root)

    def stop(self):
        if self.bridge:
            self.bridge.stop()
            self.stop_button.configure(state="disabled")
            self.status.set("正在停止…")

    def choose_game(self):
        directory=filedialog.askdirectory(title="选择 Assetto Corsa 游戏根目录", parent=self.root)
        if directory:
            if not (Path(directory)/"content/cars").is_dir():
                messagebox.showerror("目录无效", "目录中应包含 content/cars。", parent=self.root)
                return
            self.config["game_directory"]=directory
            save_config(self.path,self.config)
            if self.bridge:self.bridge.config["game_directory"]=directory

    def help(self):
        import os
        import sys
        resource_base = Path(getattr(sys, "_MEIPASS", self.base))
        path = resource_base / "help.html"
        try:
            os.startfile(path)
        except OSError:
            messagebox.showinfo("使用说明", "无法打开程序目录内的 help.html。", parent=self.root)

    def refresh(self):
        if self.bridge:
            snap = self.bridge.get_snapshot()
            f = snap["frame"]
            self.status.set(snap["status"])
            self.car.set(f"车辆：{f['car'] or '—'}    赛道：{f['track'] or '—'}")
            active = f["active"] and snap["running"]
            for key, value in (("speed", f"{f['speed_kmh']:.1f}"), ("rpm", f"{f['rpm']:.0f}"),
                               ("gear", "R" if f["gear"] == 0 else "N" if f["gear"] == 11 else str(f["gear"])),
                               ("fuel", f"{f['fuel_litres']:.1f}")):
                self.value_vars[key].set(value if active else "—")
            self.pedals.set("    ".join(f"{name} {f[key]*100:.0f}%" for name, key in
                            (("油门", "throttle"), ("刹车", "brake"), ("离合", "clutch"), ("转向", "steer"))) if active else "油门 —    刹车 —    离合 —    转向 —")
            self.stats.set(f"已发送 {snap['sent']:,} 包 · {snap['actual_hz']:.1f} Hz · {self.config['host']}:{self.config['port']} · UDP 无接收确认")
            self.error.set(snap["error"] or "；".join(f["notes"]))
            if self.bridge.thread and not self.bridge.thread.is_alive():
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                for widget in self.inputs:
                    widget.configure(state="readonly" if isinstance(widget, ttk.Combobox) else "normal")
                if self.closing:
                    self.root.destroy()
                    return
        self.root.after(100, self.refresh)

    def close(self):
        if self.bridge and self.bridge.thread and self.bridge.thread.is_alive():
            self.closing = True
            self.stop()
        else:
            self.root.destroy()
