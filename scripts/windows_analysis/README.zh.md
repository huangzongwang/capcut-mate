# Windows 原生分析（不修改框架）

模型配置已升级为线上只读目录。数据库连接直接读取项目根目录 `env.db.dev`，
API 环境需要 `requirements-api.txt`，模型能力与解密密钥要求见
`docs/online-model-migration.zh.md`。旧 model_config_id 不再用于新任务。

## 修改边界

本功能只修改 `src/service/video_analysis/`，新增本目录的部署工具和分析测试。
不修改 `main.py`、公共中间件、公共配置、框架依赖或原有剪映接口。
现有检测、切割、逐镜字幕、模型分析请求和结果结构保持不变。
Linux 默认分析解释器、媒体目录和 Whisper 模型目录保持不变。

## 安装与启动

在项目根目录的 Anaconda Prompt 中运行：

```bat
scripts\windows_analysis\install.bat
scripts\windows_analysis\start.bat --check-only --public-host 192.168.0.191
scripts\windows_analysis\start.bat --public-host 192.168.0.191
```

安装脚本只创建/更新 `capcut-analysis`，不重装现有 `capcut-mate` API 环境。
需要现有 API 环境已经安装项目、FastAPI/Uvicorn 和 pywin32。
运行时需要支持 HTTP Range 的 Starlette；本机已验证版本为 1.7.0，
旧环境请运行媒体回归测试检查，不自动升级框架依赖。
支持通过 `API_ENV_NAME` 和 `ANALYSIS_ENV_NAME` 覆盖 Conda 环境名。
API 默认使用 30000，独立分析素材服务使用 30001（`--media-port` 可覆盖）。
局域网预览需要允许素材端口入站；脚本不会擅自修改防火墙。
启动前必须停止旧 API/Docker 分析实例，不要同时消费同一 SQLite 任务库。
脚本检测端口占用后会退出，不会杀掉占用者，也不会自动重启 Docker。
仅环境检查不会启动 API、执行历史排队任务或调用云端模型。

## 为什么独立素材服务

公共响应中间件会包装非 200 响应，影响视频 Range/206。
为了不改框架，分析模块使用独立素材服务，只映射原视频、分镜和草稿目录。
支持 Range 播放、禁止越界/符号链接逃逸，不暴露数据库、模型和日志。
不依赖剪映是否安装，纯分析和草稿创建无需桌面自动化。
草稿文件/分镜可访问，不代表已经生成完整视频预览成片。

## 路径与环境变量

| 变量 | Windows 默认/含义 |
| --- | --- |
| `SCENE_PYTHON` | start.bat 查找 capcut-analysis 的 python.exe |
| `SCENE_MEDIA_ROOT` | 项目 media 目录 |
| `SCENE_SPLIT_ROOT` | output/scene-clips |
| `SCENE_DRAFT_CLIP_ROOT` | html/output/analysis-clips |
| `SCENE_WHISPER_ROOT` | models/whisper-windows，独立 Windows 缓存 |
| `SCENE_WHISPER_MODEL_PATH` | 默认 models/faster-whisper-large-v3，本地 CTranslate2 模型目录，可覆盖 |
| `SCENE_WHISPER_DEVICE` | auto/cpu/cuda；也可用启动参数 --device |
| `SCENE_FFMPEG` / `SCENE_FFPROBE` | 可选：指定可执行文件完整路径 |
| `SCRIPT_VIDEO_MEDIA_URL_BASE` | 脚本生成草稿中的媒体公开地址；start.bat 自动设为 `http://<--public-host>:<--media-port>/media`，可在启动前显式覆盖 |

环境变量优先于脚本默认值；已有变量指向 Docker 路径时需要清除或修正。
新安装环境的 Library/bin 优先于系统 PATH，避免误用老 FFmpeg。
自检会检查 `-fps_mode` 支持，不能只以“找到 ffmpeg.exe”判断环境可用。
启动器默认直接加载 models/faster-whisper-large-v3，必须提前准备完整模型文件。
Docker 缓存可能包含 Windows 无法读取的 Linux 链接，不要直接指向这类快照。
可导出普通文件目录，或下载完整模型后设置 `SCENE_WHISPER_MODEL_PATH`。
本机已从旧缓存清单还原五个模型文件并校验 model.bin 的 SHA256，旧缓存未改动。

## GPU 与模型验收

脚本不自动安装显卡驱动、CUDA/cuDNN、OCR，也不将 CPU 回退伪装成 GPU 成功。
基础自检只检查导入、工具、目录和 GPU 枚举，不等于模型推理通过。
使用短样本执行真实转写检查：

```bat
scripts\windows_analysis\start.bat --check-only --device cuda --transcribe-sample "C:\samples\short.mp4"
```

CPU 可用 `--device cpu`。默认 auto 根据设备枚举选择，GPU 库缺失会明确报错，
不会捕获后悄悄切成 CPU。模型路径、CUDA/cuDNN 不完整时需先修复后再提交任务。
OCR 原接口保留，默认分析不启用；需要 OCR 时另行安装验证 PaddleOCR 环境。

## 测试与迁移

`tests/test_analysis_windows_runtime.py` 覆盖目录、上传、Range、安全边界、
Windows 超时/取消对子进程树的清理。设置 `SCENE_TEST_NATIVE=1` 和
`SCENE_PYTHON` 可增加真实 FFmpeg 两镜切割与总帧数校验。
保留现有 SQLite；不会自动迁移、修改历史任务或重写旧报告地址。
迁移前备份 db、media、reports、html/output、output/draft。
旧报告中的 Linux 绝对路径和旧草稿地址不保证自动可用，需单独迁移核对。

## 本次验证记录（2026-10-12）

- 已在本机创建 `capcut-analysis`，安装分析依赖和环境内 FFmpeg；pip check 通过。
- BAT `--check-only` 通过。未切换或重启当前生产 API，未迁移数据库。
- 定向测试 28 项，24 项通过、4 项 Linux 专用测试跳过。
- Windows 中文/空格路径下，真实两镜样片的检测、切割、质量校验通过，总计 100 帧。
- 超时及取消清理子进程树、媒体 Range/206、目录越界测试通过。
- 本地 large-v3 已在 HF_HUB_OFFLINE=1 下完成 6 秒样本 GPU 转写，输出 6 条字幕；
  完整云端分析任务仍需重启服务后测试。
- 额外执行旧 `test_video_file_analysis.py` 时，3 项测试因 mock 指向已不存在的
  `analyze_global_report_with_responses` 而失败；本次未修改这些旧测试和语义分析业务。
