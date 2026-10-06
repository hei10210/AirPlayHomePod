# AirPlay HomePod 推流工具（免费替代 TuneBlade）

把 Windows 的**全部系统声音**（浏览器、视频、游戏、音乐软件）推送到 HomePod mini 播放。
零成本：无需购买 TuneBlade，无需任何订阅。

- **默认协议：AirPlay 2**（低延迟 ~1-2 秒，音质好，会话稳定）
- 保留 **AirPlay 1** 完整备份随时可切换

> ⚠️ 本项目为个人学习研究产物，非苹果官方软件，未与 Apple 有任何关联。

## 原理

```
Windows 系统声音 ──(VB-CABLE 虚拟扬声器)──> ffmpeg ──(PCM s16le 管道)──> airplay_send ──(AirPlay 2 加密流)──> HomePod mini
```

- **VB-CABLE（免费虚拟声卡）**：把电脑默认输出设为 CABLE Input，系统声音全部进虚拟线缆
- **ffmpeg**：DirectShow 捕获 CABLE Output → 原始 PCM（44.1kHz/双声道）走管道
- **airplay_send**：C++ 实现的 AirPlay 2 客户端（基于 airplay2-sender-cpp，Apache-2.0），
  transient 配对免 PIN，加密控制通道 + RTP 流，**会话持续稳定**

## 快速开始（3 分钟）

> 完整安装顺序（从零环境）见 **[环境安装说明.md](环境安装说明.md)**

1. **确认驱动**：VB-CABLE 已安装，默认播放设备 = CABLE Input（未装见安装说明第 1 步）
2. **双击 `AirPlayHomePod.exe`** 启动 GUI
3. **扫描局域网设备** → 选择你的 HomePod（端口 7000）
4. **枚举设备** → 选中 `CABLE Output (VB-Audio Virtual Cable)`
5. **▶ 开始推流** → 电脑上任何声音从 HomePod 播出

> 首次推流如被 Windows 防火墙拦截：放行 `airplay_send.exe` 的**入站 UDP+TCP**（按 exe 路径加规则）。
> 复制/移动 exe 后需重新放行。

## 文件结构

| 文件/目录 | 作用 |
|---|---|
| `AirPlayHomePod.exe` | 主程序（GUI，双击运行） |
| `airplay_homepod_gui.py` | 主程序源码（`python airplay_homepod_gui.py` 运行） |
| `airplay_send.exe` | AirPlay 2 推流引擎（需与 3 个 DLL 同目录） |
| `libstdc++-6.dll` 等 3 个 DLL | 引擎运行库 |
| `ffmpeg/` | ffmpeg 引擎（DirectShow 捕获）— *仓库未携带（~500MB），按环境安装说明第 2 步下载* |
| `ap2src/` | airplay2-sender-cpp 源码 + 本地增益/管道改造（Apache-2.0） |
| `AirPlay1方案-备份/` | **AirPlay 1 完整备份**（独立可用，备用切换）— *仓库未携带，本地发布包有* |
| `安装包/` | VB-CABLE 官方驱动包（免费） |
| `环境安装说明.md` | 从零安装顺序（驱动 → 防火墙 → 运行 → 编译） |
| `逆向研究笔记.md` | 苹果 AirPlay 2 加密协议逆向研究思路与结论 |

## 功能

### 测试音
不依赖录音设备，ffmpeg 合成 440Hz 持续音推流，点「■ 停止」才结束——先验证链路通不通（音量极低，晚上不吵）。

### 音量（无极调音）
- 滑杆 **0~100**，拖动过程**实时跟手**，100 = 原始音量
- 机制：音量由 **airplay_send 内部 C++ 本地增益**实现（不动 HomePod 侧音量），Python 只发一条 UDP 指令，**任何拖动频率都不会导致推流中断**
- **推荐用法**：音乐软件音量 100%，HomePod 硬件音量（手机控制中心）拉满，**只用本工具滑杆控制**——滑杆 20 ≈ 20% 音量

### 自动重连
HomePod 会话偶发释放时，GUI 自动退避重连（最多 6 次）。

## 常见问题

| 现象 | 处理 |
|---|---|
| 扫描不到 HomePod | 确认同一 WiFi、HomePod 已通电；临时关闭防火墙；路由器 AP 隔离关掉 |
| 连接失败/无声音 | 确认 HomePod 未正被 iPhone 等其他设备播放占用 |
| 推流中声音卡顿 | WiFi 用 5G 频段；路由器靠近 HomePod |
| 有延迟 | AirPlay 2 有 ~1-2 秒缓冲，属协议正常；听歌/看视频可用 |
| 音量不跟手/偏大 | HomePod 硬件音量可能被锁定：HomePod顶部可调原始HomePod音量 |

## 技术备注

- ffmpeg 低延迟参数：`-fflags nobuffer -flags low_delay -probesize 32 -analyzeduration 0 -flush_packets 1`
- AirPlay 2 免 PIN（transient pairing，HomePod 无 PIN 弹窗）
- 延迟实测 ≤ 3 秒，PCM 无压缩流 + 低延迟 ffmpeg 参数

## 致谢

- **airplay2-sender-cpp**（Apache-2.0）——AirPlay 2 推流引擎基础：https://github.com/akustikrausch/airplay2-sender-cpp
- **ffmpeg**——音视频采集/转码
- **VB-Audio Virtual Cable**——免费虚拟声卡：https://vb-audio.com/Cable/
- **pyatv**——AirPlay 1 备用方案依赖

## License / 免责声明

- 本项目仅供学习研究用途，请勿用于商业或侵权场景
- 本工具与 Apple Inc. 无任何关联；Apple、HomePod、AirPlay 为 Apple 商标
- 第三方组件遵循各自许可证（airplay2-sender-cpp: Apache-2.0；ffmpeg: LGPL/GPL；VB-CABLE: 免费软件）
