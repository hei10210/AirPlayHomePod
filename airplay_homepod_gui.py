# -*- coding: utf-8 -*-
"""
AirPlay HomePod 推流工具（Windows · pyatv 版）
================================================
原理：
  Windows 系统声音 → ffmpeg 捕获(立体声混音) → MP3 流(管道) → pyatv(RAOP) → HomePod mini

  全程免费、无需编译：ffmpeg 采集系统音频，pyatv（纯 Python 的 AirPlay 客户端）
  通过 RAOP 协议（AirPlay 1，无需配对）推送到 HomePod。

用法：python airplay_homepod_gui.py
依赖：ffmpeg（本目录 ffmpeg 子目录或 PATH 中）、Python 3.9+、pyatv（pip install pyatv）
"""

import asyncio
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import pyatv
from pyatv.conf import AppleTV, RaopService
from pyatv.interface import MediaMetadata

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)          # exe 模式：exe 所在目录
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))  # 脚本模式
SAMPLE_RATE = 44100
AP2_CTRL_PORT = 55999   # airplay_send --control：本地 UDP 实时音量口


def find_ffmpeg():
    """依次探测：本目录 ffmpeg 子目录 → PATH。找不到返回 None"""
    for root, _, files in os.walk(os.path.join(APP_DIR, "ffmpeg")):
        for f in files:
            if f.lower() == "ffmpeg.exe":
                return os.path.join(root, f)
    hit = shutil.which("ffmpeg")
    return hit if hit else None


def find_airplay_send():
    """AirPlay 2 推流引擎：本目录 airplay_send.exe（与 ffmpeg 同目录部署）"""
    p = os.path.join(APP_DIR, "airplay_send.exe")
    if os.path.exists(p):
        return p
    return shutil.which("airplay_send")


# ───────────── 默认播放设备（虚拟扬声器模式，pycaw 延迟导入） ─────────────

def audio_default_id():
    """当前默认播放设备 id"""
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().id


def audio_default_name():
    """当前默认播放设备名"""
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().FriendlyName


def audio_find_device(keyword):
    """按名称关键词查找设备（返回 AudioDevice 或 None）"""
    from pycaw.pycaw import AudioUtilities
    for d in AudioUtilities.GetAllDevices():
        if keyword in d.FriendlyName:
            return d
    return None


def audio_set_default(device_id):
    """把默认播放设备切到指定设备（IPolicyConfig.SetDefaultEndpoint, eConsole）"""
    from comtypes import CoCreateInstance, CLSCTX_ALL, GUID
    from pycaw.api.policyconfig import IPolicyConfig
    pc = CoCreateInstance(GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}"),
                          interface=IPolicyConfig, clsctx=CLSCTX_ALL)
    pc.SetDefaultEndpoint(device_id, 0)


def ffmpeg_version(exe):
    try:
        out = subprocess.run([exe, "-version"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, timeout=10)
        return out.stdout.splitlines()[0].strip()
    except Exception:
        return ""


def parse_dshow_devices(ffmpeg_exe):
    """运行 ffmpeg -f dshow -list_devices true -i dummy，解析音频设备名列表。
    兼容新旧输出格式：
    - 旧版：有 "DirectShow audio devices:" 标题行分段
    - 新版（N-127203+）：每行直接带 "(audio)" 类型标注
    """
    try:
        # PyInstaller --windowed 环境下 subprocess 的 PIPE 捕获（尤其 stderr）会失效，
        # 因此枚举输出改走文件重定向（stderr 合并进 stdout 一起写入文件）。
        tmp = os.path.join(APP_DIR, "ffmpeg_devlist.txt")
        with open(tmp, "w", encoding="utf-8", errors="replace") as f:
            subprocess.run(
                [ffmpeg_exe, "-hide_banner", "-f", "dshow", "-list_devices", "true", "-i", "dummy"],
                stdout=f, stderr=subprocess.STDOUT, timeout=25,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        with open(tmp, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        return [], "枚举命令执行异常[%s]: %s（ffmpeg=%s）" % (type(e).__name__, e, ffmpeg_exe)
    devs, in_audio = [], False
    for ln in text.splitlines():
        # 旧版：标题行定位音频段
        if "DirectShow audio devices" in ln:
            in_audio = True
            continue
        if "DirectShow video devices" in ln:
            in_audio = False
            continue
        # 新版：行内带类型标注 → 只收 audio
        m = re.search(r'"([^"]+)"\s*\((\w+)\)', ln)
        if m and "Alternative name" not in ln:
            name, typ = m.group(1), m.group(2)
            if typ == "audio":
                devs.append(name)
            continue
        # 旧版：处于音频段且无类型标注的普通行
        if in_audio and "Alternative name" not in ln:
            m2 = re.search(r'"([^"]+)"', ln)
            if m2:
                devs.append(m2.group(1))
    return devs, ""


def make_conf(dev):
    """由扫描结果构造仅含 RAOP 服务的配置"""
    raop_svc = next((s for s in dev.services if s.protocol.name == "RAOP"), None)
    if raop_svc is None:
        return None
    conf = AppleTV(dev.address, dev.name)
    conf.add_service(RaopService(raop_svc.identifier, raop_svc.port,
                                 None, None, raop_svc.properties))
    return conf


class Worker(threading.Thread):
    """常驻 asyncio 事件循环线程：连接 / 推流 / 音量 / 停止都在同一 loop 内串行执行"""

    def __init__(self, log_q):
        super().__init__(daemon=True)
        self.log_q = log_q
        self.loop = asyncio.new_event_loop()
        self.atv = None
        self.ffmpeg_proc = None
        self.send_proc = None   # AirPlay 2 引擎：airplay_send 子进程
        self._pump = None       # ffmpeg stdout → airplay_send stdin 泵线程
        self._gain = 0.5        # AirPlay 2 本地音量衰减（泵线程应用，HomePod 侧不动）
        self.stream_task = None
        self._closing = False

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro):
        """向 worker loop 提交协程（线程安全）"""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def log(self, msg):
        self.log_q.put(("log", msg))

    # ── 协程任务 ──
    async def scan(self):
        self.log("正在扫描局域网 AirPlay 设备…")
        loop = asyncio.get_running_loop()
        try:
            devices = await pyatv.scan(loop, timeout=6)
        except Exception as e:
            self.log("扫描出错：%s" % e)
            return []
        result = []
        for d in devices:
            raop_svc = next((s for s in d.services if s.protocol.name == "RAOP"), None)
            if raop_svc:
                result.append({
                    "name": d.name, "ip": str(d.address), "port": raop_svc.port,
                    "identifier": raop_svc.identifier or "",
                    "properties": dict(raop_svc.properties or {}),
                })
        return result

    async def play(self, ffmpeg_exe, source, host, port, volume, test_tone=False,
                   identifier="", properties=None, engine="ap2"):
        """启动推流，直到流结束（EOF）返回。engine: ap2=AirPlay 2（低延迟）, ap1=RAOP"""
        self._closing = False
        if engine == "ap2":
            await self._play_ap2(ffmpeg_exe, source, host, volume, test_tone)
            return
        loop = asyncio.get_running_loop()
        # 1) 连接 RAOP
        conf = AppleTV(host, host)
        conf.add_service(RaopService(identifier or "", port, None, None, properties))
        self.log("连接 %s:%s（RAOP）…" % (host, port))
        try:
            self.atv = await pyatv.connect(conf, loop, protocol=pyatv.Protocol.RAOP)
        except Exception as e:
            self.log("连接失败：%s" % e)
            self.log_q.put(("status", "连接失败"))
            return
        self.log("连接成功，开始推流")
        self.log_q.put(("status", "推流中…"))

        # 2) 启动 ffmpeg（捕获 → MP3 → 管道）；低延迟参数：跳过探测、nobuffer、管道立即刷出
        lowlat = ["-fflags", "nobuffer", "-flags", "low_delay",
                  "-probesize", "32", "-analyzeduration", "0", "-flush_packets", "1"]
        if test_tone:
            args = [ffmpeg_exe, "-hide_banner", "-loglevel", "error"] + lowlat + [
                    "-f", "lavfi", "-i", "sine=frequency=440",
                    "-af", "volume=0.01",
                    "-ar", str(SAMPLE_RATE), "-ac", "2",
                    "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", "-"]
            self.log("测试音：440Hz 持续播放（超低音量），点「停止」结束")
        else:
            args = [ffmpeg_exe, "-hide_banner", "-loglevel", "error"] + lowlat + [
                    "-sample_rate", str(SAMPLE_RATE), "-channels", "2",
                    "-rtbufsize", "256K",
                    "-f", "dshow", "-i", "audio=%s" % source,
                    "-ar", str(SAMPLE_RATE), "-ac", "2",
                    "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", "-"]
            self.log("音频源：%s" % source)
        self.log("已启动：%s" % ("测试音" if test_tone else source))
        try:
            self.ffmpeg_proc = subprocess.Popen(
                args, stdout=subprocess.PIPE,
                stderr=open(os.path.join(APP_DIR, "gui_ffmpeg.log"), "ab"),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            self.log("启动 ffmpeg 失败：%s" % e)
            await self._cleanup()
            self.log_q.put(("status", "启动失败"))
            return

        # 3) 初始音量
        try:
            await self.atv.audio.set_volume(max(0, min(100, volume)))
        except Exception as e:
            self.log("设置音量失败（忽略）：%s" % e)

        # 4) 推流（阻塞直到管道 EOF = ffmpeg 结束）；显式给 metadata 跳过管道标签探测
        self.stream_task = asyncio.ensure_future(
            self.atv.stream.stream_file(
                self.ffmpeg_proc.stdout,
                metadata=MediaMetadata(title="Windows Audio", artist="AirPlay 推流"),
            )
        )
        try:
            await self.stream_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log("推流异常：%s" % e)
        finally:
            await self._cleanup()
            if not self._closing:
                self.log_q.put(("status", "推流已结束"))

    async def _play_ap2(self, ffmpeg_exe, source, host, volume, test_tone=False):
        """AirPlay 2 引擎：ffmpeg 捕获 → raw s16le 管道 → airplay_send --homepod --stdin"""
        send_exe = find_airplay_send()
        if not send_exe:
            self.log("未找到 airplay_send.exe（AirPlay 2 引擎），请把它放在程序同目录")
            self.log_q.put(("status", "缺少 airplay_send"))
            return
        lowlat = ["-fflags", "nobuffer", "-flags", "low_delay",
                  "-probesize", "32", "-analyzeduration", "0", "-flush_packets", "1"]
        if test_tone:
            args = [ffmpeg_exe, "-hide_banner", "-loglevel", "error"] + lowlat + [
                    "-f", "lavfi", "-i", "sine=frequency=440",
                    "-af", "volume=0.01", "-ar", "44100", "-ac", "2",
                    "-c:a", "pcm_s16le", "-f", "s16le", "-"]
            self.log("测试音：440Hz 持续播放（超低音量），点「停止」结束")
        else:
            args = [ffmpeg_exe, "-hide_banner", "-loglevel", "error"] + lowlat + [
                    "-sample_rate", "44100", "-channels", "2",
                    "-rtbufsize", "256K",
                    "-f", "dshow", "-i", "audio=%s" % source,
                    "-ar", "44100", "-ac", "2",
                    "-c:a", "pcm_s16le", "-f", "s16le", "-"]
            self.log("音频源：%s" % source)
        gain0 = (max(0.0, min(100.0, float(volume))) / 100.0) * 1.0
        send_args = [send_exe, host, "--homepod", "--stdin",
                     "--volume", "100",      # HomePod 侧 0dB；实际音量由本地增益控制
                     "--gain", "%.4f" % gain0,   # 初始增益（滑杆 100 → 1.0 = 原始音量）
                     "--control", str(AP2_CTRL_PORT),
                     "--name", "HomePod-推流"]
        # 本地增益映射：滑杆 0~100 → 0 ~ 1.0（100 = 原始音量；HomePod 侧恒 0dB）
        self._gain = gain0
        # ffmpeg 只启动一次（持续捕获）；airplay_send 连接失败自动重试
        # 诊断：ffmpeg/send 的 stderr 写日志文件（不丢弃），断联/卡顿时可查
        ff_err = open(os.path.join(APP_DIR, "gui_ffmpeg.log"), "ab")
        self._ff_err = ff_err
        try:
            self.ffmpeg_proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=ff_err,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            self.log("启动 ffmpeg 失败：%s" % e)
            await self._cleanup()
            self.log_q.put(("status", "启动失败"))
            return

        # HomePod 会话释放较慢，失败后静默退避重连（最多 6 次，覆盖 ~90 秒释放期）
        for attempt in range(1, 7):
            if self._closing:
                break
            self.log("AirPlay 2 连接 %s:7000（免配对，第 %d 次尝试）…" % (host, attempt))
            self.log_q.put(("status", "连接中…（等待 HomePod 释放）"))
            try:
                self.send_proc = subprocess.Popen(
                    send_args, stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=open(os.path.join(APP_DIR, "gui_send.log"), "ab"),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as e:
                self.log("启动推流器失败：%s" % e)
                await self._cleanup()
                self.log_q.put(("status", "启动失败"))
                return
            send = self.send_proc

            # 泵线程（绑定本次 send 实例，失败重试时旧泵自然退出）
            # 纯转发：音量衰减在 airplay_send 的 C++ producer 里做（GAIN 命令），
            # Python 侧零处理开销，杜绝拖杆/高负载下的断流
            def pump(s=send):
                try:
                    while True:
                        data = self.ffmpeg_proc.stdout.read(65536)
                        if not data:
                            break
                        s.stdin.write(data)
                        s.stdin.flush()
                except Exception:
                    pass
                finally:
                    try:
                        s.stdin.close()
                    except Exception:
                        pass
            self._pump = threading.Thread(target=pump, daemon=True)
            self._pump.start()

            # 等待本次尝试的结果：检测到推流开始（成功）或进程退出（失败）
            try:
                fsize = os.path.getsize(os.path.join(APP_DIR, "gui_send.log"))
            except Exception:
                fsize = 0
            success = False
            for _ in range(60):
                if self._closing:
                    break
                if send.poll() is not None:
                    break
                try:
                    with open(os.path.join(APP_DIR, "gui_send.log"), "rb") as f:
                        f.seek(fsize)
                        new = f.read().decode("utf8", "replace")
                    if "streaming to" in new:
                        self.log("第 %d 次尝试：连接成功，推流中" % attempt)
                        self.log_q.put(("status", "推流中"))
                        success = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(1)
            if self._closing:
                break
            if not success and send.poll() is not None:
                # 连接阶段就失败（未进入推流）
                self.send_proc = None
                rc = send.returncode
                if attempt < 6:
                    wait_s = min(15 + (attempt - 1) * 10, 40)   # 15/25/35/40/40s 退避
                    self.log("第 %d 次尝试：连接失败（退出码 %d），%d 秒后自动重试…" %
                             (attempt, rc, wait_s))
                    await asyncio.sleep(wait_s)
                else:
                    self.log("第 %d 次尝试：连接失败（退出码 %d），重试次数用尽" %
                             (attempt, rc))
                continue
            if not success:
                # 60 秒轮询未检测到 streaming 但进程仍存活：视为已连上（继续等会话）
                self.log("第 %d 次尝试：连接成功，推流中" % attempt)
                self.log_q.put(("status", "推流中"))
            try:
                # 无限等待：推流器正常持续运行（C++ 增益 + 纯转发泵不会再卡死），
                # 只有用户点「停止」或推流器自身退出时才返回。早期 45 秒超时
                # 会把正常运行的会话误杀，导致"断了又重连"。
                rc = await asyncio.to_thread(send.wait)
            except Exception:
                rc = -1
            self.send_proc = None
            if self._closing:
                break
            if rc == 0:
                self.log("第 %d 次尝试：连接成功，会话正常结束" % attempt)
                break
            if attempt < 6:
                wait_s = min(15 + (attempt - 1) * 10, 40)   # 15/25/35/40/40s 退避
                self.log("第 %d 次尝试：会话中断（退出码 %d），%d 秒后自动重试…" %
                         (attempt, rc, wait_s))
                await asyncio.sleep(wait_s)
            else:
                self.log("第 %d 次尝试：会话中断（退出码 %d），重试次数用尽" %
                         (attempt, rc))
        await self._cleanup()
        if not self._closing:
            self.log_q.put(("status", "推流已结束"))

    async def set_volume(self, v):
        if self.atv:
            try:
                await self.atv.audio.set_volume(max(0, min(100, v)))
                self.log_q.put(("log", "音量 → %d" % v))
            except Exception:
                pass
        elif self.send_proc:
            # AirPlay 2：本地增益（C++ producer 原子应用，不碰 HomePod 会话）
            g = (max(0.0, min(100.0, float(v))) / 100.0) * 1.0
            try:
                import socket as _s
                s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
                s.sendto(("GAIN %.4f" % g).encode(), ("127.0.0.1", AP2_CTRL_PORT))
                s.close()
                self.log_q.put(("log", "音量 → %d（增益 %.2f）" % (int(v), g)))
            except Exception:
                pass

    async def stop(self):
        self._closing = True
        self.log("正在停止…")
        if self.ffmpeg_proc:
            try:
                self.ffmpeg_proc.terminate()   # EOF → airplay_send 自动结束
            except Exception:
                pass
        if self.send_proc:
            try:
                self.send_proc.terminate()
            except Exception:
                pass
        await self._cleanup()
        self.log_q.put(("status", "已停止"))
        self.log("已停止")

    async def _cleanup(self):
        if self.ffmpeg_proc:
            try:
                self.ffmpeg_proc.terminate()
            except Exception:
                pass
            self.ffmpeg_proc = None
        if getattr(self, "_ff_err", None):
            try:
                self._ff_err.close()
            except Exception:
                pass
            self._ff_err = None
        if self.send_proc:
            try:
                self.send_proc.terminate()
            except Exception:
                pass
            self.send_proc = None
        if self.atv:
            try:
                self.atv.close()
            except Exception:
                pass
            self.atv = None


class AirPlayGui:
    def __init__(self, root):
        self.root = root
        root.title("AirPlay HomePod 推流工具")
        root.geometry("780x700")
        root.minsize(740, 640)

        # ffmpeg 加入 PATH（pyatv/子进程可能用到）
        self.ffmpeg_exe = find_ffmpeg()
        if self.ffmpeg_exe:
            os.environ["PATH"] = os.path.dirname(self.ffmpeg_exe) + os.pathsep + os.environ.get("PATH", "")

        self.log_q = queue.Queue()
        self.worker = Worker(self.log_q)
        self.worker.start()

        style = ttk.Style(root)
        try:
            style.theme_use("vista")
        except Exception:
            pass

        pad = dict(padx=10, pady=5)
        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)

        # ① 引擎
        top = ttk.LabelFrame(frm, text="① 推流引擎（ffmpeg + airplay_send / pyatv）", padding=8)
        top.pack(fill="x", **pad)
        eng_row = ttk.Frame(top)
        eng_row.pack(fill="x")
        ttk.Label(eng_row, text="协议：").pack(side="left")
        self.engine_combo = ttk.Combobox(eng_row, width=32, state="readonly")
        self.engine_combo["values"] = ("AirPlay 2（低延迟，推荐）",
                                       "AirPlay 1（RAOP 兼容）")
        self.engine_combo.current(0)
        self.engine_combo.pack(side="left", padx=4)
        self.engine_status = tk.StringVar(value=self._fmt_engine())
        ttk.Label(eng_row, textvariable=self.engine_status, wraplength=430).pack(side="left", padx=8)
        ttk.Button(eng_row, text="选择 ffmpeg.exe", command=self.choose_ffmpeg).pack(side="right", padx=4)
        ttk.Button(eng_row, text="刷新", command=self.refresh_engine).pack(side="right", padx=4)

        # ② 设备
        dev = ttk.LabelFrame(frm, text="② 目标设备（HomePod）", padding=8)
        dev.pack(fill="x", **pad)
        row1 = ttk.Frame(dev)
        row1.pack(fill="x")
        ttk.Label(row1, text="发现列表：").pack(side="left")
        self.device_combo = ttk.Combobox(row1, width=46, state="readonly")
        self.device_combo.pack(side="left", padx=4)
        self.scan_btn = ttk.Button(row1, text="扫描局域网设备", command=self.start_scan)
        self.scan_btn.pack(side="left", padx=4)
        row2 = ttk.Frame(dev)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="或手动输入 IP：").pack(side="left")
        self.ip_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.ip_var, width=18).pack(side="left", padx=4)
        ttk.Label(row2, text="端口：").pack(side="left")
        self.port_var = tk.StringVar(value="7000")
        ttk.Entry(row2, textvariable=self.port_var, width=7).pack(side="left", padx=4)

        # ③ 虚拟扬声器（声音只去 HomePod）
        vs = ttk.LabelFrame(frm, text="③ 虚拟扬声器（声音只去 HomePod，扬声器静默）", padding=8)
        vs.pack(fill="x", **pad)
        vs_row = ttk.Frame(vs)
        vs_row.pack(fill="x")
        self.vs_state = tk.StringVar(value="读取默认播放设备…")
        ttk.Label(vs_row, textvariable=self.vs_state, wraplength=430).pack(side="left")
        self.vs_on_btn = ttk.Button(vs_row, text="设为虚拟扬声器", command=self.use_virtual_speaker)
        self.vs_on_btn.pack(side="left", padx=6)
        self.vs_off_btn = ttk.Button(vs_row, text="恢复原扬声器", command=self.restore_speaker)
        self.vs_off_btn.pack(side="left", padx=6)
        self.vs_default_id = None  # 切换前记住的原默认设备
        self.vs_hint = tk.StringVar()
        ttk.Label(vs, textvariable=self.vs_hint, foreground="#8e44ad", wraplength=700,
                  justify="left").pack(anchor="w", pady=(6, 0))

        # ④ 音频源
        src = ttk.LabelFrame(frm, text="④ 音频源（系统声音来源）", padding=8)
        src.pack(fill="x", **pad)
        row3 = ttk.Frame(src)
        row3.pack(fill="x")
        ttk.Label(row3, text="录音设备：").pack(side="left")
        self.src_combo = ttk.Combobox(row3, width=50, state="readonly")
        self.src_combo.pack(side="left", padx=4)
        self.enum_btn = ttk.Button(row3, text="枚举设备", command=self.enum_devices)
        self.enum_btn.pack(side="left", padx=4)
        self.stereo_hint = tk.StringVar()
        ttk.Label(src, textvariable=self.stereo_hint, foreground="#8e44ad", wraplength=700,
                  justify="left").pack(anchor="w", pady=(6, 0))

        # ⑤ 控制
        ctl = ttk.LabelFrame(frm, text="⑤ 播放控制", padding=8)
        ctl.pack(fill="x", **pad)
        ctl_row = ttk.Frame(ctl)
        ctl_row.pack(fill="x")
        ttk.Label(ctl_row, text="音量：").pack(side="left")
        self.vol = tk.IntVar(value=30)   # 默认音量 30（gain 0.3，约 -10dB，温和）
        # tk.Scale（经典控件）：拖动过程实时触发回调，无极跟手
        self.vol_scale = tk.Scale(ctl_row, from_=0, to=100, orient="horizontal",
                                  variable=self.vol, length=180, command=self._vol_changed)
        self.vol_scale.pack(side="left", padx=4)
        ttk.Label(ctl_row, textvariable=self.vol, width=4).pack(side="left")
        self.test_btn = ttk.Button(ctl_row, text="♪ 测试音", command=self.play_test_tone)
        self.test_btn.pack(side="left", padx=(14, 4))
        self.start_btn = ttk.Button(ctl_row, text="▶ 开始推流", command=self.start_stream)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(ctl_row, text="■ 停止", command=self.stop_stream, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.state_var = tk.StringVar(value="未连接")
        ttk.Label(ctl_row, textvariable=self.state_var, foreground="#16a085").pack(side="left", padx=10)

        # ⑥ 日志
        logf = ttk.LabelFrame(frm, text="⑥ 运行日志", padding=8)
        logf.pack(fill="both", expand=True, **pad)
        self.log = scrolledtext.ScrolledText(logf, height=8, state="disabled",
                                             font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

        note = ("使用步骤：① 先点「测试音」验证 HomePod 链路 → ② 扫描/选择 HomePod → "
                "③ 点「设为虚拟扬声器」（声音只进虚拟声卡）→ ④ 枚举录音设备选 CABLE Output → ⑤ 开始推流。"
                "停止推流后点「恢复原扬声器」切回。默认 AirPlay 2 协议（低延迟、免配对），"
                "旧设备可切回 AirPlay 1（RAOP）。")
        ttk.Label(frm, text=note, foreground="#7f8c8d", wraplength=720, justify="left").pack(**pad)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._refresh_default_device()
        self._poll()
        self.root.after(400, self._startup_init)

    # ───────────── 启动自动初始化 ─────────────
    def _startup_init(self):
        """启动即用：清理残留进程 → 自动虚拟扬声器 → 自动音频源 → 自动扫描设备"""
        self._post_log("—— 启动初始化 ——")
        # 1) 清理残留的 airplay_send（上次异常退出遗留，会占用 HomePod 会话/控制口）
        try:
            subprocess.run(["taskkill", "/F", "/IM", "airplay_send.exe"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._post_log("已清理残留推流进程")
        except Exception:
            pass
        # 2) 虚拟扬声器自动就绪（默认设备不是 CABLE → 自动切换并记住原设备）
        try:
            name = audio_default_name()
            if "CABLE" in name:
                self._post_log("虚拟扬声器已就绪：%s" % name)
            else:
                cable_in = audio_find_device("CABLE Input")
                if cable_in is not None:
                    if self.vs_default_id is None:
                        self.vs_default_id = audio_default_id()
                    audio_set_default(cable_in.id)
                    self._post_log("已自动切换虚拟扬声器：系统声音 → CABLE（原设备已记住，可随时恢复）")
                else:
                    self._post_log("未检测到 VB-CABLE 虚拟声卡，请安装后重启程序")
            self._refresh_default_device()
        except Exception as e:
            self._post_log("虚拟扬声器初始化失败：%s" % e)
        # 3) 音频源自动枚举并选中（优先 CABLE Output）
        if self.ffmpeg_exe:
            self.enum_devices()
        # 4) 自动扫描 HomePod 并选中
        self.root.after(1200, self.start_scan)

    # ───────────── 虚拟扬声器 ─────────────
    def _refresh_default_device(self):
        """读取当前默认播放设备并刷新显示"""
        try:
            name = audio_default_name()
            self.vs_state.set("当前默认播放设备：%s" % name)
            if "CABLE" in name:
                self.vs_hint.set("已处于虚拟扬声器模式：系统声音全部进入虚拟声卡，扬声器静默。"
                                 "直接枚举音频源选 CABLE Output 后开始推流即可。")
            else:
                self.vs_hint.set("点「设为虚拟扬声器」后，系统所有声音将只进虚拟声卡 → 推流到 HomePod，"
                                 "电脑扬声器不再出声。")
        except Exception as e:
            self.vs_state.set("无法读取默认播放设备（%s）" % e)

    def use_virtual_speaker(self):
        """把默认播放设备切到 CABLE Input（虚拟声卡），并自动选 CABLE Output 为音频源"""
        try:
            cable_in = audio_find_device("CABLE Input")
            if cable_in is None:
                self._post_log("未找到 CABLE Input：请先安装 VB-CABLE（免费虚拟声卡）")
                messagebox.showwarning("未安装虚拟声卡",
                                       "未找到 CABLE Input (VB-Audio Virtual Cable)。\n"
                                       "请到 vb-audio.com/Cable 下载安装 VB-CABLE（免费）后重试。")
                return
            if self.vs_default_id is None:
                self.vs_default_id = audio_default_id()
            audio_set_default(cable_in.id)
            self._refresh_default_device()
            self._post_log("已切换到虚拟扬声器：系统声音 → CABLE → HomePod（扬声器静默）")
            # 自动枚举并选中 CABLE Output
            if self.ffmpeg_exe and not self.src_combo.get():
                self.enum_devices()
        except Exception as e:
            self._post_log("切换失败：%s" % e)

    def restore_speaker(self):
        """恢复切换前的默认播放设备"""
        try:
            if self.vs_default_id:
                audio_set_default(self.vs_default_id)
                self._post_log("已恢复原默认播放设备")
            else:
                self._post_log("没有记录到切换前的设备（本次会话未切换过）")
            self._refresh_default_device()
        except Exception as e:
            self._post_log("恢复失败：%s" % e)

    # ───────────── 引擎 ─────────────
    def _fmt_engine(self):
        lines = []
        if self.ffmpeg_exe:
            lines.append("ffmpeg：OK " + ffmpeg_version(self.ffmpeg_exe)[:40])
        else:
            lines.append("ffmpeg：未找到 —— 点击「选择 ffmpeg.exe」")
        lines.append("pyatv：OK")
        return "  |  ".join(lines)

    def refresh_engine(self):
        self.ffmpeg_exe = find_ffmpeg()
        if self.ffmpeg_exe:
            os.environ["PATH"] = os.path.dirname(self.ffmpeg_exe) + os.pathsep + os.environ.get("PATH", "")
        self.engine_status.set(self._fmt_engine())

    def choose_ffmpeg(self):
        path = filedialog.askopenfilename(title="选择 ffmpeg.exe",
                                          filetypes=[("ffmpeg", "ffmpeg.exe")])
        if path:
            self.ffmpeg_exe = path
            self.engine_status.set(self._fmt_engine())

    # ───────────── 设备 ─────────────
    def start_scan(self):
        self.scan_btn.config(state="disabled", text="扫描中…")
        self.state_var.set("正在扫描局域网…")
        fut = self.worker.call(self.worker.scan())
        fut.add_done_callback(lambda f: self.root.after(0, self._scan_done, f))

    def _scan_done(self, fut):
        self.scan_btn.config(state="normal", text="扫描局域网设备")
        try:
            found = fut.result()
        except Exception as e:
            self.state_var.set("扫描出错")
            self._post_log("扫描出错：%s" % e)
            return
        if not found:
            self.state_var.set("未发现 AirPlay 设备，请手动输入 IP")
            self._post_log("未发现 RAOP 设备（请确认 PC 与 HomePod 在同一 WiFi，且 HomePod 已通电）")
            return
        self._devices = found
        labels = ["%s  @ %s:%s" % (d["name"], d["ip"], d["port"]) for d in found]
        self.device_combo["values"] = labels
        self.device_combo.current(0)
        self.state_var.set("发现 %d 台设备" % len(found))
        for d in found:
            self._post_log("发现设备：%s  %s:%s" % (d["name"], d["ip"], d["port"]))

    # ───────────── 音频源 ─────────────
    def enum_devices(self):
        if not self.ffmpeg_exe:
            messagebox.showwarning("缺少 ffmpeg", "请先选择 ffmpeg.exe")
            return
        self.enum_btn.config(state="disabled", text="枚举中…")
        threading.Thread(target=self._enum_worker, daemon=True).start()

    def _enum_worker(self):
        devs, err = parse_dshow_devices(self.ffmpeg_exe)
        self.root.after(0, self._enum_done, devs, err)

    def _enum_done(self, devs, err):
        self.enum_btn.config(state="normal", text="枚举设备")
        if err:
            self._post_log("枚举失败：%s" % err)
            return
        if not devs:
            self.stereo_hint.set("未枚举到录音设备：请打开 控制面板→声音→录制，右键空白处勾选“显示禁用的设备”，"
                                 "右键“立体声混音”→启用。启用后重新枚举。")
            self._post_log("未枚举到录音设备")
            return
        self.src_combo["values"] = devs
        pre = None
        for d in devs:
            if "CABLE Output" in d:
                pre = d
                break
        if pre is None:
            for d in devs:
                if "立体声混音" in d or "Stereo Mix" in d:
                    pre = d
                    break
        self.src_combo.current(devs.index(pre) if pre else 0)
        if pre:
            self.stereo_hint.set("")
        else:
            self.stereo_hint.set("未找到“立体声混音”：建议启用它来捕获全局系统声音（见提示），否则只能录制麦克风。")
        self._post_log("音频设备：%s" % "、".join(devs))

    # ───────────── 推流控制 ─────────────
    def _resolve_target(self):
        sel = self.device_combo.get()
        if sel:
            for d in getattr(self, "_devices", []) or []:
                label = "%s  @ %s:%s" % (d["name"], d["ip"], d["port"])
                if label == sel:
                    return d["ip"], d["port"], d.get("identifier", ""), d.get("properties")
            tail = sel.split("@")[-1].strip().rsplit(":", 1)
            return tail[0], int(tail[1]), "", None
        ip = self.ip_var.get().strip()
        if not ip:
            return None, None, "", None
        try:
            port = int(self.port_var.get().strip() or 7000)
        except ValueError:
            port = 7000
        return ip, port, "", None

    def _do_start(self, test_tone):
        if not self.ffmpeg_exe:
            messagebox.showwarning("缺少 ffmpeg", "请先选择 ffmpeg.exe")
            return
        engine = "ap2" if str(self.engine_combo.get()).startswith("AirPlay 2") else "ap1"
        if engine == "ap2" and not find_airplay_send():
            messagebox.showwarning(
                "缺少 AirPlay 2 引擎",
                "未找到 airplay_send.exe（AirPlay 2 推流引擎）。\n"
                "请把 airplay_send.exe 放到本程序同目录后重试，\n"
                "或改用「AirPlay 1（RAOP 兼容）」协议。")
            return
        ip, port, identifier, properties = self._resolve_target()
        if not ip:
            messagebox.showwarning("未填设备", "请选择扫描到的设备，或手动填写 IP")
            return
        source = None
        if not test_tone:
            source = self.src_combo.get()
            if not source:
                messagebox.showwarning("未选音频源", "请先枚举并选择录音设备（推荐立体声混音）")
                return
        self.start_btn.config(state="disabled")
        self.test_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.state_var.set("连接中…")
        self.worker.call(self.worker.play(self.ffmpeg_exe, source, ip, port,
                                          self.vol.get(), test_tone,
                                          identifier, properties, engine))

    def start_stream(self):
        self._do_start(False)

    def play_test_tone(self):
        self._do_start(True)

    def _vol_changed(self, _=None):
        # 本地衰减：每次拖动立即生效（无极调音），无需防抖
        self.worker.call(self.worker.set_volume(self.vol.get()))

    def stop_stream(self):
        self.worker.call(self.worker.stop())
        self.start_btn.config(state="normal")
        self.test_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    # ───────────── 消息轮询 ─────────────
    def _poll(self):
        try:
            while True:
                item = self.log_q.get_nowait()
                if isinstance(item, tuple) and item[0] == "status":
                    self.state_var.set(item[1])
                    if item[1] == "推流已结束" or item[1] == "已停止":
                        self.start_btn.config(state="normal")
                        self.test_btn.config(state="normal")
                        self.stop_btn.config(state="disabled")
                elif isinstance(item, tuple) and item[0] == "log":
                    self._post_log(item[1])
                else:
                    self._post_log(str(item))
        except queue.Empty:
            pass
        self._poll_job = self.root.after(120, self._poll)

    def _post_log(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def on_close(self):
        try:
            self.worker.call(self.worker.stop())
        except Exception:
            pass
        if self._poll_job:
            self.root.after_cancel(self._poll_job)
        self.root.destroy()


def main():
    root = tk.Tk()
    AirPlayGui(root)
    root.mainloop()


def _selftest_enum():
    """自检模式（--selftest-enum）：枚举音频设备并把结果写入 exe 旁的 selftest_enum.txt，
    不弹 GUI。用于打包后自动验证 exe 内枚举链路。"""
    exe = find_ffmpeg()
    out = ["ffmpeg=%s" % exe]
    if exe:
        devs, err = parse_dshow_devices(exe)
        out.append("ERR=%s" % err)
        out.append("DEVS=%s" % devs)
        # 验证推流用的 stdout PIPE 是否可用（生成 1 秒 MP3 读前 4KB）
        try:
            proc = subprocess.Popen(
                [exe, "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                 "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-q:a", "2", "-f", "mp3", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            chunk = proc.stdout.read(4096)
            proc.kill()
            out.append("PIPE_STDOUT_LEN=%d" % len(chunk))
        except Exception as e:
            out.append("PIPE_EXC=%s: %s" % (type(e).__name__, e))
        # 验证 pycaw（默认设备读取）打包正常
        try:
            from pycaw.pycaw import AudioUtilities
            out.append("DEFAULT_DEV=%s" % AudioUtilities.GetSpeakers().FriendlyName)
        except Exception as e:
            out.append("PYCAW_ERR=%s: %s" % (type(e).__name__, e))
    else:
        out.append("ERR=ffmpeg not found")
    try:
        with open(os.path.join(APP_DIR, "selftest_enum.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(out))
    except Exception as e:
        out.append("write_fail=%s" % e)
    return 0


if __name__ == "__main__":
    if "--selftest-enum" in sys.argv:
        sys.exit(_selftest_enum())
    main()
