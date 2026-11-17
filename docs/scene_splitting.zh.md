# 第二版镜头切割接口

2026-09-17 播放兼容性修正：默认改为 `encoding=compatible`，使用常规 H.264 High。
旧的 `lossless` 输出是 High 4:4:4 Predictive，软件解码一致不代表所有播放器都能正常播放。
旧输出文件不会被覆盖，必须重新请求生成兼容版。

## 目录与职责

沿用已有 `video_analysis` 模块，没有放进剪映草稿生成库，也没有恢复面积比例检测。

| 文件 | 改动与职责 |
|---|---|
| `src/router/video_analysis.py` | 增加 POST `/v1/split_scenes` 和 GET `/v1/scene_clips/{job_id}/{filename}` |
| `src/schemas/video_analysis.py` | 增加切割请求、帧区间、片段和结果模型；复用原检测参数 |
| `src/service/video_analysis/scene_splitting.py` | API 侧输出目录管理、失败清理、原子发布和下载路径校验 |
| `src/service/video_analysis/splitting.py` | Worker 内边界复核、时间戳检查、FFmpeg 按帧重编码、逐帧校验 |
| `src/service/video_analysis/scene_detection.py` | 复用原 Worker 调度，增加切割专用超时与 Linux 进程组终止 |
| `src/service/video_analysis/worker.py` | 复用原输入下载和跨进程锁；仅切割请求将增强检测信号传给切割模块 |
| `Dockerfile.local` | 增加系统 FFmpeg/FFprobe，不引入 OCR、ONNX、PyAV |
| `docker-compose.yaml` | 独立持久化片段目录、超时、输出大小配置，以及回收孤儿子进程的 init |
| `config/nginx.local.conf` | 代理读取超时改为 960 秒，覆盖最长 900 秒切割请求 |
| `tests/test_scene_splitting.py` | 参数、路径、失败清理、原子发布与进程组取消测试 |
| `tests/test_scene_split_worker.py` | 真 FFmpeg 非关键帧切割、音频、时间戳、复核、逐帧一致性及大小限制测试 |
| `tests/test_scene_detection_api.py` | 在原真实 HTTP 测试中增加切割和下载覆盖 |

没有修改 `enhanced.py` 的第二版检测算法，也没有修改已有剪映 API。
原 `detect_scenes` 仍只返回镜头时间表，不产生片段。

## 调用

```http
POST /openapi/capcut-mate/v1/split_scenes
Content-Type: application/json
```

```json
{
  "video_path": "desktop4-20260917-baseline/08.mp4",
  "boundary_policy": "strict"
}
```

输入路径、可信视频地址及文件限制与原检测接口相同。必须且只能传
`video_path` 或 `video_url`；不能传任意磁盘绝对路径。只支持 `enhanced`。

| 新字段 | 默认值 | 含义 |
|---|---|---|
| `encoding` | `compatible` | 常规 H.264 High、YUV420P、CRF 16，VBV 最大码率 20 Mbps、缓冲 40 Mb；不缩小分辨率 |
| `encoding=lossless` | 非默认 | H.264 无损；需要支持 High 4:4:4 Predictive 的播放器，文件较大 |
| `encoder` | `auto` | 兼容模式优先使用 NVIDIA NVENC，不可用时回退 `libx264`；也可指定 `cpu` 或 `nvenc` |
| `boundary_policy` | `strict` | 边界有歧义时返回 `needs_review`，不生成或发布任何片段 |
| `boundary_policy=allow_review` | 非默认 | 明确允许导出待复核边界；仍执行帧校验，不能视为纯净镜头 |
| `confirmed_ranges` | 不传 | 人工确认的帧区间，最多 100 段；替代自动区间，必须升序、不重叠且不越界 |

`confirmed_ranges` 采用零基帧号，左闭右开。例如：

```json
{
  "video_path": "demo.mp4",
  "confirmed_ranges": [
    {"start_frame": 0, "end_frame": 100},
    {"start_frame": 105, "end_frame": 200}
  ]
}
```

该请求主动丢弃 `[100,105)` 五帧，可用于排除人工确认的叠化过渡区。
不传该字段时不硬裁剪所有镜头头尾，不默认丢帧。
该字段是调用方确认，不表示系统已经判断这些区间都是纯净镜头。

## 处理与保证

1. 复用第二版检测和已有下载、大小、时长、分辨率检查。
2. 在候选边界约前后 0.2 秒内逐帧检查变化峰，结合前后画面持续性复核。
   只有显著更强的峰才移动切点；窗口限制在相邻镜头中点内。
   这是启发式，不是语义模型，分数不是概率。
3. FFprobe 读取逐帧时间戳；目前只接受稳定 CFR，拒绝缺失、非递增或明显变帧率时间戳。
   拒绝未规范化的旋转、奇数宽高和 HDR 输入，避免静默改变显示效果。
4. FFmpeg 解码原视频，以 `trim` 帧区间选取画面并重编码，不使用关键帧 `-c copy`。
   视频时间从零开始，保留第一条音轨并按源时间裁剪、补齐必要静音、编码为 AAC。
   导出按小批次共享一次源视频解码：NVENC 默认每批 4 段，CPU 回退最多每批 2 段；
   没有使用会将非关键帧边界推迟的 segment muxer。
5. 两种模式都检查解码帧数、音视频时长和时间起点。
   兼容模式将输出与对应源区间逐帧计算 PSNR，要求对齐的帧数完整且每帧不低于 35 dB；
   同时检查编码 Profile 和像素格式。无损模式则对统一为 8-bit YUV420 的全部帧做 MD5 顺序比对。
   任一检查失败，整批不发布。PSNR 是质量门槛，不是镜头检测准确率或像素同一性证明。
6. 整批通过后先写临时完成清单，再原子重命名为 `manifest.json`，才开放该批下载。
   不移动包含大量视频的目录，避免 Windows Docker 挂载的大目录重命名问题。
   普通失败、超时、请求任务取消均会终止进程组并清理临时文件。
   Windows 挂载目录的文件若仍被占用，复用项目现有 `deferred_delete` 后台队列重试，
   不让清理异常遮盖原始错误；没有完成清单的目录始终不开放下载。

默认输出使用高质量有损 H.264 High，保持源分辨率和帧率，避免将高码率无损格式作为普通播放文件。
显式选择无损模式时，像素一致性仅相对于统一后的 YUV420 帧，
不表示原始高位深或其他色度格式零损失。

**实际保证**：输出准确包含指定源帧区间，没有编码/关键帧定位额外带入的相邻帧。

**不保证**：算法从不漏镜头，或源帧不含叠化、运动模糊、字幕及其他混合内容。
画面复核通过只记为 `hard_cut_candidate`，不表示语义纯净已认证。
需要严格纯净素材时，应人工确认不确定区间，必要时主动舍弃转场帧。

## 返回与下载

与现有项目一致，先检查 JSON `code`，不要只看 HTTP 状态：

- `code=0,status=completed`：整批切割和帧校验成功，`clips` 包含每段下载路径。
- `code=0,status=needs_review`：边界有歧义，`clips=[]`、`job_id=null`；没有发布文件。
- `review_required=true`：仍有复核未通过的边界；仅 `allow_review` 允许带此标记导出。
- `boundary_reviews`：原切点、选定切点、移动帧数、峰值、持续性与复核原因。
- `detection`：原第二版结果；最终实际输出区间以 `clips` 为准。
- `clips[].encoding`：实际使用的编码模式。
- `requested_encoder/actual_encoder`：请求的编码器策略和实际编码器。
- `clips[].actual_encoder`：该片段实际使用 `libx264` 或 `h264_nvenc`。
- `clips[].verification=frame_count_and_quality_checked`：兼容模式帧数和逐帧质量检查通过，不表示逐像素相同。
- `clips[].min_frame_psnr_db`：兼容模式最低单帧 PSNR；所有帧均为无穷大时返回 null，无损模式不计算。
- `clips[].verification=all_decoded_frames_match`：仅无损模式使用，全部帧匹配源区间，不是语义识别通过。
- `clips[].start_us/end_us`：基于实际 PTS、相对首帧的整数微秒；精度受实际帧间隔限制。
- `clips[].download_path`：同源下载路径，不使用调用方的 Host 构造绝对地址。

```http
GET /openapi/capcut-mate/v1/scene_clips/{job_id}/scene-001.mp4
```

下载仅支持完整 HTTP 200 文件流，暂不支持 Range 断点/拖动。
项目现有响应中间件会重写非 200 状态；本次没有扩大范围修改全站中间件。
只能下载格式合法的已发布 MP4，不能读取 manifest、临时目录或任意文件。

## 输出与限制

本地：`E:\project\capcut-mate\output\scene-clips\<job_id>\`。
Docker：`/app/scene-clips/<job_id>/`。
输出不放进 nginx 静态目录，未完成文件不开放下载。
`output` 是项目已有的生成文件目录且已被 Git 忽略。

| 配置 | 默认 |
|---|---|
| `SCENE_SPLIT_ROOT` | compose 为 `/app/scene-clips`；非 compose 为项目 `output/scene-clips` |
| `SCENE_SPLIT_TIMEOUT_SECONDS` | 900 秒，包含检测、复核、编码、校验 |
| `SCENE_SPLIT_MAX_BYTES` | 1 GiB，单请求输出片段总大小限制 |
| `SCENE_EXPORT_BATCH_SIZE` | 4；CPU 回退内部上限为 2 |
| `SCENE_FFMPEG_THREADS` | 2；防止多个 CPU 编码器自动扩张导致内存峰值过高 |
| 最大片段数 | 100 |
| 单次媒体子命令超时 | 300 秒 |
| 同容器并发 | 检测和切割共用一个跨进程锁，忙时返回 `code=409` |

该版本仍是同步请求，不是可重启恢复的后台任务队列。
输入沿用 200 MiB、600 秒和分辨率限制。总大小限制基于编码器阈值及落盘校验，
不是操作系统硬磁盘配额；容器强制断电可能留下 `.staging` 或无完成清单的片段目录，
它们不会作为完成结果发布。
已完成片段不自动过期，请按业务保留需求定期清理 `output/scene-clips`。

导出或校验失败返回 `code=400`，资源限制返回 `413`，超时返回 `504`，
媒体运行时缺失返回 `503`；参数校验失败沿用 `1001`。
本地接口没有新增用户鉴权；公网使用必须先配置鉴权、配额和输出生命周期。

## 验证命令

```powershell
docker run --rm --mount "type=bind,source=E:\project\capcut-mate\tests,target=/app/tests,readonly" `
  capcut-mate-local:scenedetect-0.7.1 /app/.venv/bin/python `
  -m unittest discover -s tests -p test_scene_splitting.py -v

docker run --rm --mount "type=bind,source=E:\project\capcut-mate,target=/workspace,readonly" `
  capcut-mate-local:scenedetect-0.7.1 /opt/scenedetect/bin/python `
  -m unittest discover -s /workspace/tests -p test_scene_split_worker.py -v
```

真实 HTTP 测试在原检测测试挂载基础上额外挂载
`output/scene-clips` 为 `/split-fixtures`，用于删除该次测试自己创建的输出。
测试不删除用户素材或其他任务的输出。

## GPU 与批处理优化验收（2026-09-21）

本地 RTX 4060 Ti、Docker Desktop NVIDIA runtime，容器分配 4 CPU / 4 GiB：

- Compose 已请求全部 GPU，并开放 `compute,video,utility` 驱动能力。
- `encoder=auto` 实际选择 `h264_nvenc`；不可用时自动回退 `libx264`。
- 兼容模式不再为整段源视频生成未使用的帧 MD5；无损模式仍保留全帧 MD5 比对。
- 编码与 PSNR 校验均按小批次共享源视频解码，仍逐段检查帧数、时间、音频和每帧 PSNR。
- 单次 segment muxer 原型不能稳定落在非关键帧边界，已放弃，没有进入生产代码。

真实样片 `media/desktop4-20260921-dual-api/08.mp4`（555.mp4）：

| 项目 | 优化前 | 优化后 |
|---|---:|---:|
| 总耗时 | 243.72 秒 | 71.58 秒 |
| 实际编码器 | libx264 | h264_nvenc |
| 分镜数 | 8 | 8 |
| 总帧数 | 1803 | 1803 |
| 最低单帧 PSNR | 45.51 dB | 50.01 dB |

优化后帧区间仍为 `[0,219)、[219,511)、[511,545)、[545,873)、[873,917)、
[917,1580)、[1580,1621)、[1621,1803)`，与优化前完全一致。
本次 NVENC 输出约 99.56 MiB，文件大小高于旧版约 58.15 MiB；这是 CQ 16 的质量/速度取舍，
不是边界变化。任务目录为 `output/scene-clips/c0d7b977428e4c099e0f41641e7f72d9`。

## 首次无损版验收（2026-09-17，历史记录）

自动测试共 37 项通过：

| 测试文件 | 数量 | 覆盖 |
|---|---:|---|
| `test_scene_detection.py` | 9 | 原输入限制、安全校验、锁、超时回归 |
| `test_scene_enhanced.py` | 5 | 第二版检测与边界选择回归 |
| `test_scene_splitting.py` | 7 | 新参数、下载路径、清理、发布、真实子进程超时终止 |
| `test_scene_split_worker.py` | 8 | 全帧一致性、单帧/非关键帧、29.97 fps、音频延迟、非零时间戳、严格模式 |
| `test_scene_detection_api.py` | 8 | 原 HTTP 接口、新切割接口、本地/URL 输入、下载、错误请求 |

真实样片为已复制进素材目录的 `555.mp4`：
`media/desktop4-20260917-baseline/08.mp4`，1080×1920、25 fps、72.12 秒。

- 请求：默认第二版参数，`boundary_policy=strict`。
- 返回：`code=0`、`status=completed`、`review_required=false`。
- 输出：8 个 MP4，共 1803 帧，全部通过源区间与输出的逐帧顺序校验。
- 所有切点与此前保存的第二版检测结果一致，本样片没有移动切点。
- 耗时：257.07 秒；当时容器限制为 1 CPU / 1 GiB。
- 总大小：716,993,632 字节，约 684 MiB；这是无损中间素材，不是压缩交付成片。
- 运行中验证：未完成片段返回 `code=404`，并发检测返回 `code=409`。
- 重启容器后，经 nginx 下载第 3 段，字节数及 SHA-256 与落盘文件一致。

输出目录：

```text
E:\project\capcut-mate\output\scene-clips\51c6045542f74e0b8ef7e010b0ca9996\
```

内有 8 个片段及完整 `manifest.json`：

| 文件 | 起始秒 | 结束秒（不含） | 帧范围（左闭右开） | 帧数 |
|---|---:|---:|---|---:|
| scene-001.mp4 | 0.00 | 8.76 | [0,219) | 219 |
| scene-002.mp4 | 8.76 | 20.44 | [219,511) | 292 |
| scene-003.mp4 | 20.44 | 21.80 | [511,545) | 34 |
| scene-004.mp4 | 21.80 | 34.92 | [545,873) | 328 |
| scene-005.mp4 | 34.92 | 36.68 | [873,917) | 44 |
| scene-006.mp4 | 36.68 | 63.20 | [917,1580) | 663 |
| scene-007.mp4 | 63.20 | 64.84 | [1580,1621) | 41 |
| scene-008.mp4 | 64.84 | 72.12 | [1621,1803) | 182 |

这次实测证明输出区间与第二版边界一致且没有切割额外带帧，**不是新的语义准确率评估**。

### 测试残留说明

第一次真实样片试跑在大目录发布/清理阶段遇到 Windows 挂载权限错误。
已改为完成清单原子发布，并为被占用文件接入项目现有延迟删除队列。
该次旧试跑的 8 个文件仍留在以下目录，约 684 MiB：

```text
E:\project\capcut-mate\output\scene-clips\.staging\d8308f50ee0343f8921834394f5bc47d-ha65n43a\
```

本轮清理操作被执行策略阻止，未删除该旧目录；它没有完成清单，不提供下载，
不参与后续检测或切割。不要将此失败试跑目录与上面的成功输出目录混淆。

## 播放兼容性修正验收（2026-09-17）

用户反馈旧片段播放时画面模糊。检查旧第 1 段：

- 编码 Profile 为 `High 4:4:4 Predictive`，视频平均码率约 71.14 Mbps。
- 原片第 25 帧与旧片段对应帧经 FFmpeg 软件解码转成 PNG 后，最大像素差为 0，
  画面清晰。未发现这次抽帧存在内容模糊；实际用户播放器尚未确认，播放兼容性是优先怀疑项，
  不把软件解码通过等同于所有播放器都支持。

修正默认输出为 `compatible`，重新通过 API 从原片生成 8 段：

```text
E:\project\capcut-mate\output\scene-clips\a9b8d2c2698143e6a35d8cd3c6c9f830\
```

- 返回 `completed`，8 段共 1803 帧，所有区间与之前一致。
- 第 1 段确认是 H.264 `High`、YUV420P、1080×1920，视频平均码率约 5.73 Mbps。
- 全部片段通过帧数、音视频时间和逐帧 PSNR 检查，最低单帧 PSNR 为 45.51 dB。
- 总大小 60,972,357 字节，约 58.1 MiB；耗时 241.77 秒。
- 对应回归测试共 38 项通过，新增兼容编码 Profile/质量校验及错帧拒绝测试。
- `encoding=lossless` 仍可显式选择，不再作为普通播放的默认输出。
- 未覆盖或删除旧片段；播放修正后的文件必须使用上述新目录。

抽帧对比保存在：
`reports/scene-split-playback-20260917/compatible-comparison.png`，
左侧为原片，右侧为新兼容版。两侧采用相同尺寸展示，没有用模糊或缩小输出视频掩盖问题。
