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

CAMPUS_OPTIONS = ["杏林校区馆", "新校区", "本部校区", "铁道校区"]
# 新版校区名映射（UI 显示名 -> CSV 文件名关键词）
CAMPUS_CSV_MAP = {
    "杏林校区馆": "湘雅新校区",  # CSV 文件仍叫 "湘雅新校区座位表.csv"
    "新校区": "新校区",
    "本部校区": "本部校区",
    "铁道校区": "铁道校区",
}
# 新版 API 区域 ID 映射（需抓包确认，暂用旧 ID 占位）
CAMPUS_AREA_ID = {
    "杏林校区馆": 71,
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
    """campus 为 UI 显示名（如 '杏林校区馆'），映射到 CSV 文件名"""
    csv_name = CAMPUS_CSV_MAP.get(campus, campus)
    return SEAT_CSV_DIR / f"{csv_name}座位表.csv"

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

# ================= 核心预约逻辑（适配新版 h5 + 统一身份认证） =================
class CSULibrary:
    """新版 API：统一身份认证 + h5 座位系统"""
    
    # 新版校区映射（湘雅新校区 -> 杏林校区馆）
    CAMPUS_NAME_MAP = {
        "湘雅新校区": "杏林校区馆",
        "新校区": "新校区",
        "本部校区": "本部校区", 
        "铁道校区": "铁道校区",
    }
    
    def __init__(self, userid, password):
        self.userid = userid
        self.password = password
        self.client = requests.Session()
        self.client.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        })
        self.access_token = None
        self.refresh_token = None
        
    def login(self):
        """新版登录流程：CAS 统一身份认证 -> 获取 token"""
        logger.info("开始新版登录流程...")
        
        # 1. 先访问 h5 首页建立会话
        try:
            r = self.client.get("https://libzw.csu.edu.cn/h5/index.html", timeout=15)
            logger.info(f"访问 h5 首页: {r.status_code}")
        except Exception as e:
            logger.warning(f"访问 h5 首页失败: {e}")
        
        # 2. 访问 CAS 登录页
        cas_login_url = "https://ca.csu.edu.cn/authserver/login"
        service_url = "https://libzw.csu.edu.cn/v4/login/cas"
        params = {"service": service_url}
        
        try:
            r1 = self.client.get(cas_login_url, params=params, timeout=15)
            logger.info(f"CAS 登录页: {r1.status_code}, URL: {r1.url}")
        except Exception as e:
            logger.error(f"访问 CAS 登录页失败: {e}")
            raise Exception(f"无法访问统一身份认证平台: {e}")
        
        # 3. 解析登录表单
        soup = BeautifulSoup(r1.text, 'html.parser')
        form = soup.find('form', id='casLoginForm') or soup.find('form')
        if not form:
            logger.error(f"CAS 登录页无表单: {r1.text[:2000]}")
            raise Exception("CAS 登录页结构异常，找不到登录表单")
        
        # 提取所有隐藏字段（lt, execution, _eventId 等）
        form_data = {}
        for inp in form.find_all('input'):
            name = inp.get('name')
            value = inp.get('value', '')
            if name:
                form_data[name] = value
        
        # 关键字段
        form_data['username'] = self.userid
        form_data['password'] = self.password  # 可能需要加密，先试明文
        form_data['cllt'] = 'userNameLogin'      # 账号密码登录
        form_data['dllt'] = 'generalLogin'       # 普通登录
        form_data['responseJson'] = 'true'       # 返回 JSON
        form_data['rememberMe'] = 'true'
        
        # 登录提交地址
        action = form.get('action', 'https://ca.csu.edu.cn/authserver/login')
        if not action.startswith('http'):
            from urllib.parse import urljoin
            action = urljoin(r1.url, action)
        
        logger.info(f"提交登录表单到: {action}")
        logger.info(f"表单字段: {list(form_data.keys())}")
        
        # 4. 提交登录
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': 'https://ca.csu.edu.cn',
            'Referer': r1.url,
            'Accept': 'application/json, text/javascript, */*; q=0.01',
            'X-Requested-With': 'XMLHttpRequest',
        }
        
        try:
            r2 = self.client.post(action, data=form_data, headers=headers, timeout=20)
            logger.info(f"登录提交: {r2.status_code}, 最终URL: {r2.url}")
            logger.info(f"登录响应前200字符: {r2.text[:200]}")
            
            # 处理响应（可能含 UTF-8 BOM）
            resp_text = r2.text
            if resp_text.startswith('\ufeff'):
                resp_text = resp_text[1:]
            
            # 尝试解析 JSON
            try:
                resp_json = json.loads(resp_text)
                logger.info(f"登录 JSON 响应: {resp_json}")
                if resp_json.get('success') or resp_json.get('code') == 200 or resp_json.get('result') == 'success':
                    logger.info("CAS 登录成功 (JSON)")
                else:
                    msg = resp_json.get('message') or resp_json.get('msg') or '未知错误'
                    logger.warning(f"登录失败: {msg}")
                    raise Exception(f"登录失败: {msg}")
            except json.JSONDecodeError:
                # 非 JSON 响应，检查重定向或 HTML
                logger.warning(f"响应非 JSON: {r2.text[:300]}")
                if 'ticket=' in r2.url or r2.url.startswith('https://libzw.csu.edu.cn'):
                    logger.info("CAS 登录成功，重定向回图书馆系统")
                else:
                    logger.warning(f"登录可能失败，响应: {r2.text[:300]}")
                    raise Exception("登录失败，未获取到有效响应")
                
        except Exception as e:
            logger.error(f"登录提交异常: {e}")
            raise Exception(f"登录提交失败: {e}")
        
        # 5. 回到图书馆系统，获取 token
        # 尝试调用 v4 获取 token 的接口
        try:
            # 先访问 h5 座位页建立上下文
            self.client.get("https://libzw.csu.edu.cn/h5/index.html#/seat", timeout=15)
            
            # 尝试获取 token（新版可能用 Authorization header 或 cookie）
            token_endpoints = [
                "https://libzw.csu.edu.cn/v4/auth/token",
                "https://libzw.csu.edu.cn/v4/user/token",
                "https://libzw.csu.edu.cn/api/v1/auth/token",
            ]
            for ep in token_endpoints:
                try:
                    r = self.client.get(ep, timeout=10)
                    if r.status_code == 200:
                        data = r.json()
                        if 'access_token' in data:
                            self.access_token = data['access_token']
                            self.client.headers['Authorization'] = f"Bearer {self.access_token}"
                            logger.info(f"获取到 access_token: {self.access_token[:20]}...")
                            break
                        elif 'token' in data:
                            self.access_token = data['token']
                            self.client.headers['Authorization'] = f"Bearer {self.access_token}"
                            logger.info(f"获取到 token: {self.access_token[:20]}...")
                            break
                except:
                    continue
                    
            # 如果 cookie 里有 token
            for cookie in self.client.cookies:
                if 'token' in cookie.name.lower() or cookie.name == 'access_token':
                    self.access_token = cookie.value
                    self.client.headers['Authorization'] = f"Bearer {self.access_token}"
                    logger.info(f"从 Cookie 获取 token: {self.access_token[:20]}...")
                    break
                    
        except Exception as e:
            logger.warning(f"获取 token 过程异常: {e}")
        
        # 6. 验证登录状态
        if self._check_login():
            logger.info("登录验证通过")
            return True
        else:
            logger.error("登录验证失败")
            raise Exception("登录失败，请检查账号密码或网络")
    
    def _check_login(self):
        """验证登录状态"""
        try:
            # 尝试调用需要认证的接口
            r = self.client.get("https://libzw.csu.edu.cn/v4/user/info", timeout=10)
            if r.status_code == 200:
                return True
            # 备选：查询座位列表
            r = self.client.get("https://libzw.csu.edu.cn/v4/seat/areas", timeout=10)
            return r.status_code == 200
        except:
            return False
    
    def _get_headers(self):
        """获取请求头"""
        headers = {
            'Referer': 'https://libzw.csu.edu.cn/h5/index.html',
            'Origin': 'https://libzw.csu.edu.cn',
        }
        if self.access_token:
            headers['Authorization'] = f'Bearer {self.access_token}'
        return headers
    
    def get_areas(self):
        """获取区域列表（新版 v4 API）"""
        url = "https://libzw.csu.edu.cn/v4/seat/areas"
        r = self.client.get(url, headers=self._get_headers(), timeout=15)
        logger.info(f"获取区域列表: {r.status_code}")
        return r.json()
    
    def get_area_detail(self, area_id):
        """获取区域详情（包含子区域和座位）"""
        url = f"https://libzw.csu.edu.cn/v4/seat/areas/{area_id}"
        r = self.client.get(url, headers=self._get_headers(), timeout=15)
        return r.json()
    
    def get_seats(self, area_id, segment_id, date_str):
        """获取指定区域、时段、日期的座位列表"""
        url = f"https://libzw.csu.edu.cn/v4/seat/list"
        params = {
            'area_id': area_id,
            'segment_id': segment_id,
            'date': date_str,
        }
        r = self.client.get(url, params=params, headers=self._get_headers(), timeout=15)
        return r.json()
    
    def get_segments(self, area_id, date_str):
        """获取区域的时段列表"""
        url = f"https://libzw.csu.edu.cn/v4/seat/segments"
        params = {'area_id': area_id, 'date': date_str}
        r = self.client.get(url, params=params, headers=self._get_headers(), timeout=15)
        return r.json()
    
    def reserve(self, seat_id, segment_id, date_str):
        """预约座位"""
        url = "https://libzw.csu.edu.cn/v4/seat/reserve"
        data = {
            'seat_id': seat_id,
            'segment_id': segment_id,
            'date': date_str,
            'user_id': self.userid,
        }
        r = self.client.post(url, json=data, headers=self._get_headers(), timeout=15)
        return r.json()
    
    def get_my_reservations(self):
        """获取我的预约（含未来）"""
        url = "https://libzw.csu.edu.cn/v4/user/reservations"
        r = self.client.get(url, headers=self._get_headers(), timeout=15)
        return r.json()
    
    def cancel_reservation(self, reservation_id):
        """取消预约"""
        url = f"https://libzw.csu.edu.cn/v4/seat/cancel/{reservation_id}"
        r = self.client.post(url, headers=self._get_headers(), timeout=15)
        return r.json()
    
    # 兼容旧接口方法（供现有调用处使用）
    def get_book_time_ids(self, area):
        """兼容：获取时段 ID（新版用 segment_id）"""
        # 尝试用新版 API
        try:
            tomorrow = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)).strftime('%Y-%m-%d')
            segments = self.get_segments(area, tomorrow)
            if segments.get('data'):
                segs = segments['data']
                if len(segs) >= 2:
                    return segs[0].get('id', ''), segs[1].get('id', '')
        except Exception as e:
            logger.warning(f"新版 get_segments 失败: {e}")
        return '', ''
    
    def reserve_seat(self, seat_id, segment):
        """兼容：预约座位"""
        tomorrow = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)).strftime('%Y-%m-%d')
        return self.reserve(seat_id, segment, tomorrow)
    
    def try_reserve_sequence(self, seat_infos):
        """依次尝试预约多个座位"""
        self.login()
        tomorrow = (datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)).strftime('%Y-%m-%d')
        
        for seat_no, seat_id, area in seat_infos:
            # 获取该区域明天的时段
            try:
                segments = self.get_segments(area, tomorrow)
                seg_list = segments.get('data', [])
                if not seg_list:
                    logger.warning(f"区域 {area} 无可用时段")
                    continue
                # 取全天时段（通常最后一个或第一个）
                segment_id = seg_list[-1].get('id') or seg_list[0].get('id')
            except Exception as e:
                logger.warning(f"获取时段失败: {e}")
                continue
            
            result = self.reserve(seat_id, segment_id, tomorrow)
            msg = result.get('msg', result.get('message', '未知错误'))
            if result.get('code') == 200 or result.get('status') == 1 or result.get('success') == True:
                return True, f"预约成功：{msg}", seat_no
            logger.warning(f"座位 {seat_no} 预约失败: {msg}")
        return False, f"所有座位均预约失败", None
    
    def get_future_reservations(self):
        """查询已预约的未来座位"""
        self.login()
        try:
            data = self.get_my_reservations()
            logger.info(f"获取预约列表: {data}")
            if isinstance(data, dict) and data.get('code') == 200:
                return data.get('data', [])
            elif isinstance(data, list):
                return data
        except Exception as e:
            logger.warning(f"获取预约列表失败: {e}")
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
        ttk.Button(btn_frame2, text="📂 打开日志文件", command=self.open_log_file).pack(side=tk.LEFT, padx=5)
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
        # 新版 h5 座位系统
        webbrowser.open("https://libzw.csu.edu.cn/h5/index.html#/seat")
        self._log("已打开新版图书馆预约网页 (h5)")

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
        def _do():
            try:
                lib = CSULibrary(self.config["userid"], self.config["password"])
                lib.login()
                # 新版：获取当前使用中的座位
                r = lib.client.get("https://libzw.csu.edu.cn/v4/user/current", headers=lib._get_headers(), timeout=15)
                data = r.json()
                if data.get('code') == 200 and data.get('data'):
                    d = data['data']
                    self._log(f"当前占座: {d.get('seatName', '')} {d.get('startTime', '')}-{d.get('endTime', '')}")
                else:
                    self._log("当前无占座记录")
            except Exception as e:
                self._log(f"查询失败: {e}")
        threading.Thread(target=_do, daemon=True).start()

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
                    # 新版 v4 API 字段名兼容
                    seat_name = r.get('seat_name') or r.get('seatName') or r.get('name') or '未知座位'
                    start = r.get('start_time') or r.get('startTime') or r.get('beginTime') or ''
                    end = r.get('end_time') or r.get('endTime') or r.get('endTime') or ''
                    status = r.get('status') or r.get('state') or ''
                    area_name = r.get('area_name') or r.get('areaName') or r.get('library_name') or ''
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

    def open_log_file(self):
        """打开日志文件所在文件夹，或直接用记事本打开日志文件"""
        log_path = LOG_FILE
        if log_path.exists():
            try:
                os.startfile(str(log_path))  # 用默认关联程序打开（通常是记事本）
                self._log(f"已打开日志文件: {log_path}")
            except Exception as e:
                # 兜底：打开所在文件夹
                os.startfile(str(CONFIG_DIR))
                self._log(f"打开日志文件失败，已打开所在文件夹: {CONFIG_DIR}")
        else:
            os.startfile(str(CONFIG_DIR))
            self._log(f"日志文件不存在，已打开配置目录: {CONFIG_DIR}")

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