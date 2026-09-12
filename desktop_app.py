#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CSU Library 图书馆座位预约 - 桌面版
功能：修改座位、备选三座位依次预约、一键跳转网页、修改账号密码、后台自动运行
"""

import os
import sys
import json
import threading
import time
import webbrowser
import logging
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import pandas as pd
from bs4 import BeautifulSoup
from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import pad
import base64
import random
import configparser

# 系统托盘支持
try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

# ================= 常量与路径 =================
APP_NAME = "CSU Library 座位预约"
CONFIG_DIR = Path(os.getenv("APPDATA", Path.home())) / "CSULibrary"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "app.log"
SEAT_CSV_DIR = Path(__file__).parent

# 默认配置
DEFAULT_CONFIG = {
    "userid": "",
    "password": "",
    "campus": "湘雅新校区",
    "main_seat": "3208",
    "backup_seats": ["", "", ""],
    "reserve_time": "06:01",  # 北京时间
    "auto_start": False,
    "minimize_to_tray": True,
    "run_on_startup": False,
}

CAMPUS_OPTIONS = ["湘雅新校区", "新校区", "本部校区", "铁道校区"]
CAMPUS_ID_MAP = {
    "湘雅新校区": 71,
    "新校区": 1,
    "铁道校区": 28,
    "本部校区": 94,
}

# ================= 工具函数 =================
def setup_logging():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger(__name__)

logger = setup_logging()

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                # 合并默认值
                for k, v in DEFAULT_CONFIG.items():
                    cfg.setdefault(k, v)
                return cfg
        except Exception as e:
            logger.error(f"读取配置失败: {e}")
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        logger.info("配置已保存")
        return True
    except Exception as e:
        logger.error(f"保存配置失败: {e}")
        return False

def get_seat_csv_path(campus):
    return SEAT_CSV_DIR / f"{campus}座位表.csv"

def lookup_seat_id(campus, seat_no):
    """根据校区和座位号查找 seat_id 和 area"""
    csv_path = get_seat_csv_path(campus)
    if not csv_path.exists():
        return None, None
    try:
        df = pd.read_csv(csv_path)
        row = df[df["NO"] == int(seat_no)]
        if not row.empty:
            return int(row.iloc[0]["ID"]), int(row.iloc[0]["AREA"])
    except Exception as e:
        logger.error(f"查找座位失败: {e}")
    return None, None

# ================= 核心预约逻辑（移植自 helper.py） =================
def randomString(length):
    aes_chars = 'ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678'
    return ''.join(random.choice(aes_chars) for _ in range(length))

def getAesString(data, key, iv):
    data = str.encode(data)
    data = pad(data, AES.block_size)
    key = str.encode(key)
    iv = str.encode(iv)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    cipher_text = cipher.encrypt(data)
    return str(base64.b64encode(cipher_text), encoding='utf-8')

class CSULibrary:
    def __init__(self, userid, password):
        self.userid = userid
        self.password = password
        self.client = requests.Session()
        self.seat_infos = []  # list of (seat_no, seat_id, area)

    def login(self):
        # 尝试多个可能的登录入口
        login_urls = [
            ("http://libzw.csu.edu.cn/cas/index.php", {"callback": "http://libzw.csu.edu.cn/home/web/f_second"}),
            ("http://libzw.csu.edu.cn/cas/login", {"service": "http://libzw.csu.edu.cn/home/web/f_second"}),
            ("https://libzw.csu.edu.cn/cas/login", {"service": "https://libzw.csu.edu.cn/home/web/f_second"}),
            ("http://libzw.csu.edu.cn/cas/index.php", {}),
        ]
        
        for url1, params1 in login_urls:
            try:
                logger.info(f"尝试登录入口: {url1} params={params1}")
                r1 = self.client.get(url1, params=params1, timeout=15, allow_redirects=False)
                logger.info(f"登录页状态码: {r1.status_code}, URL: {r1.url}, 长度: {len(r1.text)}")
                
                if r1.status_code == 404:
                    logger.warning(f"入口 {url1} 返回 404，尝试下一个")
                    continue
                    
                # 如果是重定向，跟随重定向
                if r1.status_code in (301, 302, 303, 307, 308):
                    redirect_url = r1.headers.get('Location')
                    logger.info(f"跟随重定向: {redirect_url}")
                    r1 = self.client.get(redirect_url, timeout=15)
                    logger.info(f"重定向后状态码: {r1.status_code}, URL: {r1.url}, 长度: {len(r1.text)}")
                
                soup = BeautifulSoup(r1.text, 'html.parser')
                salt_input = soup.find('input', id="pwdEncryptSalt")
                exec_input = soup.find('input', id="execution")
                if not salt_input or not exec_input:
                    # 尝试备选 id/name
                    salt_input = (soup.find('input', id="salt") or 
                                 soup.find('input', attrs={"name": "pwdEncryptSalt"}) or
                                 soup.find('input', attrs={"name": "salt"}))
                    exec_input = (soup.find('input', id="execution") or
                                 soup.find('input', attrs={"name": "execution"}))
                if not salt_input or not exec_input:
                    logger.warning(f"入口 {url1} 找不到 salt/execution，HTML片段: {r1.text[:500]}")
                    continue
                    
                salt = salt_input['value']
                execution = exec_input['value']
                logger.info(f"成功获取 salt/execution: salt长度={len(salt)}, execution={execution}")
                
                # 登录提交地址：优先用 form action，否则用当前 URL
                form = soup.find('form')
                url2 = form['action'] if form and form.get('action') else r1.url
                if not url2.startswith('http'):
                    from urllib.parse import urljoin
                    url2 = urljoin(r1.url, url2)
                
                data2 = {
                    'username': self.userid,
                    'password': getAesString(randomString(64)+self.password, salt, randomString(16)),
                    'captcha': '',
                    '_eventId': 'submit',
                    'cllt': 'userNameLogin',
                    'dllt': 'generalLogin',
                    'lt': '',
                    'execution': execution
                }
                r2 = self.client.post(url2, data=data2, timeout=15, allow_redirects=True)
                logger.info(f"登录提交状态码: {r2.status_code}, 最终URL: {r2.url}, Cookie: {dict(self.client.cookies)}")
                
                if "access_token" in self.client.cookies:
                    logger.info("登录成功，获取到 access_token")
                    return True
                else:
                    logger.warning(f"登录提交未获取 access_token, 响应: {r2.text[:500]}")
                    
            except Exception as e:
                logger.warning(f"入口 {url1} 尝试失败: {e}")
                continue
                
        raise Exception("所有登录入口均失败，可能需要更新登录逻辑或检查网络")

    def get_book_time_ids(self, area):
        url = f"http://libzw.csu.edu.cn/api.php/v3areadays/{area}"
        headers = {'Referer': 'http://libzw.csu.edu.cn/home/web/seat/area/1'}
        r = self.client.get(url, headers=headers, timeout=15)
        data = r.json()["data"]["list"]
        return data[0]["id"], data[1]["id"]  # today, tomorrow

    def reserve_seat(self, seat_id, segment):
        url = f"http://libzw.csu.edu.cn/api.php/spaces/{seat_id}/book"
        headers = {'Referer': 'http://libzw.csu.edu.cn/home/web/seat/area/1'}
        access_token = self.client.cookies.get('access_token')
        data = {
            'access_token': access_token,
            'userid': self.userid,
            'segment': segment,
            'type': '1',
            'operateChannel': '2'
        }
        r = self.client.post(url, headers=headers, data=data, timeout=15)
        return r.json()

    def try_reserve_sequence(self, seat_infos):
        """依次尝试预约多个座位，返回 (success, message, seat_no)"""
        self.login()
        for seat_no, seat_id, area in seat_infos:
            _, tomorrow_id = self.get_book_time_ids(area)
            result = self.reserve_seat(seat_id, tomorrow_id)
            msg = result.get('msg', '未知错误')
            if result.get('status') == 1:
                return True, f"预约成功：{msg}", seat_no
            logger.warning(f"座位 {seat_no} 预约失败: {msg}")
        return False, f"所有座位均预约失败，最后错误：{msg}", None

    def get_future_reservations(self):
        """查询已预约的未来座位（含明天）"""
        self.login()
        headers = {'Referer': 'http://libzw.csu.edu.cn/home/web/seat/area/1'}
        # 尝试常见的未来预约接口
        endpoints = [
            "http://libzw.csu.edu.cn/api.php/futureuse",
            "http://libzw.csu.edu.cn/api.php/reservations",
            "http://libzw.csu.edu.cn/api.php/bookings",
        ]
        for url in endpoints:
            try:
                r = self.client.get(url, headers=headers, params={"user": self.userid}, timeout=15)
                data = r.json()
                logger.info(f"API {url} 返回: {data}")
                if isinstance(data, dict) and data.get('status') == 1:
                    d = data.get('data')
                    if isinstance(d, list) and d:
                        return d
                    elif isinstance(d, dict):
                        return [d]  # 单对象包成列表
            except Exception as e:
                logger.warning(f"接口 {url} 失败: {e}")
                continue
        # 兜底：尝试 currentuse 里是否包含未来预约
        try:
            r = self.client.get("http://libzw.csu.edu.cn/api.php/currentuse", headers=headers, params={"user": self.userid}, timeout=15)
            data = r.json()
            logger.info(f"API currentuse 返回: {data}")
            if isinstance(data, dict) and data.get('status') == 1:
                d = data.get('data')
                if isinstance(d, list) and d:
                    return d
                elif isinstance(d, dict):
                    return [d]
        except Exception as e:
            logger.warning(f"currentuse 失败: {e}")
        return []


# ================= 系统托盘 =================
def create_tray_image():
    """生成简单托盘图标"""
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([8, 8, 56, 56], fill=(0, 102, 204))
    draw.text((20, 20), "CSU", fill=(255, 255, 255))
    return img

class TrayManager:
    def __init__(self, app):
        self.app = app
        self.icon = None
        if HAS_TRAY:
            self.icon = pystray.Icon(
                "CSULibrary",
                create_tray_image(),
                menu=pystray.Menu(
                    pystray.MenuItem("显示主界面", self.show_window, default=True),
                    pystray.MenuItem("立即预约", self.app.manual_reserve),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("退出", self.quit_app)
                )
            )

    def run(self):
        if self.icon:
            self.icon.run()

    def show_window(self):
        self.app.root.after(0, self.app.deiconify)

    def quit_app(self):
        self.app.root.after(0, self.app.on_closing)

# ================= 定时任务 =================
class Scheduler:
    def __init__(self, app):
        self.app = app
        self.running = False
        self.thread = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        logger.info("定时任务已启动")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2)
        logger.info("定时任务已停止")

    def _run(self):
        while self.running:
            cfg = self.app.config
            if not cfg.get("userid") or not cfg.get("password"):
                time.sleep(60)
                continue
            # 计算下一次运行时间（北京时间）
            now = datetime.now(timezone(timedelta(hours=8)))
            target_h, target_m = map(int, cfg["reserve_time"].split(":"))
            target = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait_sec = (target - now).total_seconds()
            logger.info(f"下次预约时间: {target.strftime('%Y-%m-%d %H:%M:%S')} (等待 {int(wait_sec)} 秒)")
            # 分段睡眠，响应停止信号
            while wait_sec > 0 and self.running:
                sleep_t = min(30, wait_sec)
                time.sleep(sleep_t)
                wait_sec -= sleep_t
            if not self.running:
                break
            # 执行预约
            self.app.root.after(0, self.app.auto_reserve)

# ================= 主界面 =================
class CSULibraryApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("600x520")
        self.root.minsize(580, 500)
        self.config = load_config()
        self.tray = TrayManager(self) if HAS_TRAY else None
        self.scheduler = Scheduler(self)
        self.reserve_thread = None
        self._build_ui()
        self._load_config_to_ui()
        self._start_tray()
        self._check_autostart()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def _build_ui(self):
        # 顶部标签页
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # --- 配置页 ---
        tab_cfg = ttk.Frame(nb)
        nb.add(tab_cfg, text="⚙️ 配置")

        row = 0
        def add_label_entry(parent, label, var, show=None, readonly=False, width=30):
            nonlocal row
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, pady=4, padx=5)
            ent = ttk.Entry(parent, textvariable=var, show=show, width=width, state='readonly' if readonly else 'normal')
            ent.grid(row=row, column=1, sticky=tk.EW, pady=4, padx=5)
            parent.columnconfigure(1, weight=1)
            row += 1
            return ent

        self.var_user = tk.StringVar()
        self.var_pwd = tk.StringVar()
        self.var_campus = tk.StringVar()
        self.var_main_seat = tk.StringVar()
        self.var_backup1 = tk.StringVar()
        self.var_backup2 = tk.StringVar()
        self.var_backup3 = tk.StringVar()
        self.var_time = tk.StringVar()
        self.var_tray = tk.BooleanVar()
        self.var_autostart = tk.BooleanVar()

        add_label_entry(tab_cfg, "学号/账号:", self.var_user)
        add_label_entry(tab_cfg, "密码:", self.var_pwd, show="●")
        ttk.Label(tab_cfg, text="校区:").grid(row=row, column=0, sticky=tk.W, pady=4, padx=5)
        cb_campus = ttk.Combobox(tab_cfg, textvariable=self.var_campus, values=CAMPUS_OPTIONS, state="readonly", width=28)
        cb_campus.grid(row=row, column=1, sticky=tk.EW, pady=4, padx=5)
        cb_campus.bind("<<ComboboxSelected>>", lambda e: self._on_campus_change())
        row += 1

        add_label_entry(tab_cfg, "主座位号:", self.var_main_seat)
        ttk.Label(tab_cfg, text="备选座位 1:").grid(row=row, column=0, sticky=tk.W, pady=4, padx=5)
        ttk.Entry(tab_cfg, textvariable=self.var_backup1, width=30).grid(row=row, column=1, sticky=tk.EW, pady=4, padx=5)
        row += 1
        ttk.Label(tab_cfg, text="备选座位 2:").grid(row=row, column=0, sticky=tk.W, pady=4, padx=5)
        ttk.Entry(tab_cfg, textvariable=self.var_backup2, width=30).grid(row=row, column=1, sticky=tk.EW, pady=4, padx=5)
        row += 1
        ttk.Label(tab_cfg, text="备选座位 3:").grid(row=row, column=0, sticky=tk.W, pady=4, padx=5)
        ttk.Entry(tab_cfg, textvariable=self.var_backup3, width=30).grid(row=row, column=1, sticky=tk.EW, pady=4, padx=5)
        row += 1

        add_label_entry(tab_cfg, "每日预约时间(北京):", self.var_time)
        ttk.Label(tab_cfg, text="格式 HH:MM，如 06:01").grid(row=row, column=1, sticky=tk.W, padx=5)
        row += 1

        ttk.Checkbutton(tab_cfg, text="最小化到托盘", variable=self.var_tray).grid(row=row, column=1, sticky=tk.W, pady=4, padx=5)
        row += 1
        ttk.Checkbutton(tab_cfg, text="开机自启", variable=self.var_autostart, command=self._toggle_autostart).grid(row=row, column=1, sticky=tk.W, pady=4, padx=5)
        row += 1

        btn_frame = ttk.Frame(tab_cfg)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text="💾 保存配置", command=self.save_config_ui).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🔍 验证座位", command=self.verify_seats).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🌐 打开预约网页", command=self.open_web).pack(side=tk.LEFT, padx=5)

        # --- 预约页 ---
        tab_res = ttk.Frame(nb)
        nb.add(tab_res, text="📋 预约")

        self.log_text = tk.Text(tab_res, height=18, state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        scroll = ttk.Scrollbar(self.log_text, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        btn_frame2 = ttk.Frame(tab_res)
        btn_frame2.pack(fill=tk.X, padx=5, pady=5)
        ttk.Button(btn_frame2, text="▶️ 立即预约(含备选)", command=self.manual_reserve).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame2, text="🔄 刷新状态", command=self.refresh_status).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame2, text="📅 查询明天预约", command=self.query_tomorrow_reservation).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame2, text="🧹 清空日志", command=lambda: self._set_log("")).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame2, text="⏱️ 启动/停止定时", command=self.toggle_scheduler).pack(side=tk.RIGHT, padx=5)

        # 底部状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W).pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=2)

    def _load_config_to_ui(self):
        cfg = self.config
        self.var_user.set(cfg.get("userid", ""))
        self.var_pwd.set(cfg.get("password", ""))
        self.var_campus.set(cfg.get("campus", "湘雅新校区"))
        self.var_main_seat.set(cfg.get("main_seat", "3208"))
        bks = cfg.get("backup_seats", ["", "", ""])
        self.var_backup1.set(bks[0] if len(bks) > 0 else "")
        self.var_backup2.set(bks[1] if len(bks) > 1 else "")
        self.var_backup3.set(bks[2] if len(bks) > 2 else "")
        self.var_time.set(cfg.get("reserve_time", "06:01"))
        self.var_tray.set(cfg.get("minimize_to_tray", True))
        self.var_autostart.set(cfg.get("run_on_startup", False))

    def _on_campus_change(self):
        # 校区变更时提示座位号可能需要重新填写
        pass

    def save_config_ui(self):
        cfg = {
            "userid": self.var_user.get().strip(),
            "password": self.var_pwd.get().strip(),
            "campus": self.var_campus.get().strip(),
            "main_seat": self.var_main_seat.get().strip(),
            "backup_seats": [
                self.var_backup1.get().strip(),
                self.var_backup2.get().strip(),
                self.var_backup3.get().strip(),
            ],
            "reserve_time": self.var_time.get().strip(),
            "minimize_to_tray": self.var_tray.get(),
            "run_on_startup": self.var_autostart.get(),
        }
        if not cfg["userid"] or not cfg["password"]:
            messagebox.showwarning("提示", "请填写账号和密码")
            return
        if not cfg["main_seat"]:
            messagebox.showwarning("提示", "请填写主座位号")
            return
        # 简单时间格式校验
        try:
            h, m = map(int, cfg["reserve_time"].split(":"))
            assert 0 <= h < 24 and 0 <= m < 60
        except:
            messagebox.showwarning("提示", "时间格式错误，应为 HH:MM")
            return
        if save_config(cfg):
            self.config = cfg
            self._log("配置已保存")
            messagebox.showinfo("成功", "配置已保存")
        else:
            messagebox.showerror("错误", "保存失败，查看日志")

    def verify_seats(self):
        campus = self.var_campus.get().strip()
        seats = [self.var_main_seat.get().strip()] + [self.var_backup1.get().strip(), self.var_backup2.get().strip(), self.var_backup3.get().strip()]
        seats = [s for s in seats if s]
        if not seats:
            messagebox.showwarning("提示", "请先填写座位号")
            return
        self._log(f"正在验证 {campus} 座位: {', '.join(seats)} ...")
        results = []
        for s in seats:
            seat_id, area = lookup_seat_id(campus, s)
            if seat_id:
                results.append(f"✅ 座位 {s} -> seat_id={seat_id}, area={area}")
            else:
                results.append(f"❌ 座位 {s} 未找到")
        self._log("\n".join(results))
        messagebox.showinfo("验证结果", "\n".join(results))

    def open_web(self):
        webbrowser.open("http://libzw.csu.edu.cn/home/web/seat/area/1")
        self._log("已打开图书馆预约网页")

    def _collect_seat_infos(self):
        """收集主座位+备选座位的 (seat_no, seat_id, area) 列表"""
        campus = self.config.get("campus", self.var_campus.get().strip())
        seat_nos = [self.config.get("main_seat", self.var_main_seat.get().strip())]
        for b in self.config.get("backup_seats", []):
            if b:
                seat_nos.append(b)
        infos = []
        for s in seat_nos:
            seat_id, area = lookup_seat_id(campus, s)
            if seat_id:
                infos.append((s, seat_id, area))
            else:
                self._log(f"⚠️ 跳过无效座位: {s}")
        return infos

    def manual_reserve(self):
        if self.reserve_thread and self.reserve_thread.is_alive():
            messagebox.showinfo("提示", "预约任务正在运行中")
            return
        self.reserve_thread = threading.Thread(target=self._do_reserve, daemon=True)
        self.reserve_thread.start()

    def _do_reserve(self):
        self._set_status("正在预约...")
        self._log("=== 开始预约 ===")
        infos = self._collect_seat_infos()
        if not infos:
            self._log("❌ 没有有效座位")
            self._set_status("就绪")
            return
        self._log(f"尝试序列: {', '.join([i[0] for i in infos])}")
        try:
            lib = CSULibrary(self.config["userid"], self.config["password"])
            ok, msg, seat_no = lib.try_reserve_sequence(infos)
            if ok:
                self._log(f"✅ {msg}")
                messagebox.showinfo("成功", msg)
            else:
                self._log(f"❌ {msg}")
                messagebox.showerror("失败", msg)
        except Exception as e:
            self._log(f"❌ 异常: {e}")
            messagebox.showerror("错误", f"预约异常: {e}")
        self._set_status("就绪")

    def auto_reserve(self):
        """定时任务调用的自动预约（静默，不弹窗）"""
        if self.reserve_thread and self.reserve_thread.is_alive():
            return
        self.reserve_thread = threading.Thread(target=self._do_auto_reserve, daemon=True)
        self.reserve_thread.start()

    def _do_auto_reserve(self):
        self._set_status("自动预约中...")
        self._log("=== 定时自动预约 ===")
        infos = self._collect_seat_infos()
        if not infos:
            self._log("❌ 没有有效座位")
            self._set_status("就绪")
            return
        self._log(f"尝试序列: {', '.join([i[0] for i in infos])}")
        try:
            lib = CSULibrary(self.config["userid"], self.config["password"])
            ok, msg, seat_no = lib.try_reserve_sequence(infos)
            if ok:
                self._log(f"✅ 自动预约成功: {msg}")
            else:
                self._log(f"❌ 自动预约失败: {msg}")
        except Exception as e:
            self._log(f"❌ 自动预约异常: {e}")
        self._set_status("就绪")

    def refresh_status(self):
        self._log("正在查询当前占座状态...")
        try:
            lib = CSULibrary(self.config["userid"], self.config["password"])
            lib.login()
            url = "http://libzw.csu.edu.cn/api.php/currentuse"
            headers = {"Referer": "http://libzw.csu.edu.cn/home/web/seat/area/1"}
            params = {"user": self.config["userid"]}
            r = lib.client.get(url, headers=headers, params=params, timeout=15)
            data = r.json().get('data', [])
            if data:
                d = data[0]
                self._log(f"当前占座: {d.get('seatName', '')} {d.get('startTime', '')}-{d.get('endTime', '')}")
            else:
                self._log("当前无占座记录")
        except Exception as e:
            self._log(f"查询失败: {e}")

    def query_tomorrow_reservation(self):
        """查询并显示明天（及未来）已预约的座位"""
        self._log("正在查询已预约座位（含明天）...")
        def _do():
            try:
                lib = CSULibrary(self.config["userid"], self.config["password"])
                reservations = lib.get_future_reservations()
                logger.info(f"get_future_reservations 返回类型: {type(reservations)}, 值: {reservations}")
                if not reservations:
                    self._log("暂无已预约座位")
                    return
                if not isinstance(reservations, list):
                    self._log(f"响应结构异常，非列表: {reservations}")
                    return
                # 过滤明天及之后的预约
                tomorrow = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)).date()
                self._log(f"=== 已预约座位列表 ===")
                for r in reservations:
                    if not isinstance(r, dict):
                        continue
                    # 兼容不同字段名
                    seat_name = r.get('seatName') or r.get('name') or r.get('seat_name') or '未知座位'
                    start = r.get('startTime') or r.get('start_time') or r.get('beginTime') or ''
                    end = r.get('endTime') or r.get('end_time') or r.get('endTime') or ''
                    status = r.get('status') or r.get('state') or ''
                    area_name = r.get('areaName') or r.get('area_name') or ''
                    self._log(f"  📍 {seat_name}  {area_name}  {start}~{end}  状态:{status}")
                self._log("========================")
            except Exception as e:
                import traceback
                logger.exception("查询异常")
                self._log(f"查询失败: {e}")
        threading.Thread(target=_do, daemon=True).start()

    def toggle_scheduler(self):
        if self.scheduler.running:
            self.scheduler.stop()
            self._log("⏸️ 定时任务已停止")
            self._set_status("就绪")
        else:
            self.scheduler.start()
            self._log("▶️ 定时任务已启动")
            self._set_status("定时运行中...")

    def _set_status(self, txt):
        self.root.after(0, lambda: self.status_var.set(txt))

    def _log(self, msg):
        def _append():
            self.log_text.configure(state=tk.NORMAL)
            self.log_text.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
            self.log_text.see(tk.END)
            self.log_text.configure(state=tk.DISABLED)
        self.root.after(0, _append)

    def _set_log(self, txt):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _start_tray(self):
        if self.tray:
            threading.Thread(target=self.tray.run, daemon=True).start()

    def _check_autostart(self):
        if self.config.get("run_on_startup"):
            self.scheduler.start()
            self._log("开机自启：定时任务已自动启动")

    def _toggle_autostart(self):
        # 这里只是记录配置，实际开机自启需写注册表/快捷方式，简化处理
        pass

    def deiconify(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def on_closing(self):
        if self.config.get("minimize_to_tray") and HAS_TRAY:
            self.root.withdraw()
            self._log("最小化到托盘，右键托盘图标可恢复")
        else:
            self.scheduler.stop()
            self.root.destroy()


def main():
    # 单实例检查（简单版）
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 47291))
    except OSError:
        messagebox.showinfo(APP_NAME, "程序已在运行（托盘区）")
        sys.exit(0)

    root = tk.Tk()
    # 设置 DPI 感知（Windows 高分屏）
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except:
        pass
    app = CSULibraryApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()