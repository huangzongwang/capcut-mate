# 视频镜头检测 API（第二版）

切割与下载接口另见 [第二版镜头切割](scene_splitting.zh.md)；本检测接口行为不变。
跨项目分析接入流程和完整结果字段另见 [视频分析接口接入文档](video_analysis_integration.zh.md)。

2026-09-17：已回退至第二版增强检测，默认 `detector=enhanced`。

## 范围

新增 `POST /openapi/capcut-mate/v1/detect_scenes`，不更改现有剪映 API 的请求字段。
输入一个视频，返回候选镜头边界，不修改源文件、不切出新视频。
支持自适应检测 `adaptive`、画面内容变化检测 `content`、实验增强检测 `enhanced`。

这一版是请求内等待结果的接口，不是任务队列。单容器最多一个检测任务，
并发请求返回忙碌；运行在独立进程，不在 API 事件循环中执行视频解码。
未包含 OCR、语音识别、镜头语义分类、可替换判断、素材匹配、50 版裂变和成片渲染。

## 本地文件调用

将视频放到 `E:\project\capcut-mate\media` 中，例如 `demo.mp4`。
compose 已将其只读挂载到 `/app/media`，不经过 nginx 公开目录。
参数传相对路径，不能传 Windows 绝对路径或容器任意路径。

PowerShell：

```powershell
$body = @{
    video_path = "demo.mp4"
    detector = "enhanced"
    min_scene_len = 15
} | ConvertTo-Json
Invoke-RestMethod -Method Post `
    -Uri "http://localhost:30000/openapi/capcut-mate/v1/detect_scenes" `
    -ContentType "application/json" -Body $body
```

## 视频地址调用

```json
{
  "video_url": "http://nginx/output/demo.mp4",
  "detector": "content",
  "threshold": 27,
  "min_scene_len": 15
}
```

`video_path` 与 `video_url` 必须且只能提供一个。默认只允许主机 `nginx`。
如需已有素材库，管理员在 `SCENE_ALLOWED_MEDIA_HOSTS` 添加精确的主机与显式端口，
例如 `nginx,media.example.com,media.example.com:8443`，不能使用通配符。
只允许 HTTP(S)，禁止用户名/密码形式的 URL，不跟随重定向，不使用环境代理。

允许列表里的主机必须由部署者信任和控制，包括其 DNS；不要把任意用户可控制的
域名添加进去。带签名的查询参数支持，但不应把真实令牌写入公开文档。
这不是面向公网的完整鉴权服务；对外部署需在网关增加身份认证、限流与访问控制。

## 上传视频后分析

调用方可以先将本地视频上传到服务端素材目录，再将接口返回的 `video_path`
传入同步分析或异步分析任务。上传接口接收 `multipart/form-data`，字段名为 `file`。

```http
POST /openapi/capcut-mate/v1/upload_video
Content-Type: multipart/form-data
```

```bash
curl -X POST "http://localhost:30000/openapi/capcut-mate/v1/upload_video" \
  -F "file=@./demo.mp4"
```

成功响应示例：

```json
{
  "code": 0,
  "message": "成功",
  "video_path": "uploads/4b7a2f8d63ea4c8b99d603c947429fa1.mp4",
  "filename": "4b7a2f8d63ea4c8b99d603c947429fa1.mp4",
  "size_bytes": 12345678
}
```

将 `video_path` 原样用于分析任务：

```json
{
  "video_path": "uploads/4b7a2f8d63ea4c8b99d603c947429fa1.mp4",
  "detector": "enhanced",
  "use_external_model": true,
  "use_transcription": true,
  "video_fps": 2
}
```

请求地址为 `POST /openapi/capcut-mate/v1/analyze_video_tasks`；
分析完成后使用任务状态和结果接口获取结果。上传大小上限由
`SCENE_MAX_FILE_BYTES` 控制，默认 200 MiB。文件保存在服务端素材目录，
目前没有配套删除接口；调用方或部署方需负责上传文件的保留和磁盘空间管理。

## 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| video_path | 无 | 素材根目录内相对路径，支持子目录 |
| video_url | 无 | 可信源视频地址 |
| detector | enhanced | adaptive / content / enhanced；省略时使用第二版增强检测 |
| threshold | adaptive=3.0；content=27.0 | 范围 (0,255]；越低通常检测到越多切镜，两种模式数值含义不同 |
| min_scene_len | 15 | 最短镜头间隔参数，单位帧，范围 1～1500；尾镜头仍可能较短 |
| min_content_val | adaptive=15；enhanced=30 | 最低内容变化量；content 模式不可用 |
| window_width | adaptive=2；enhanced=4 | 前后分析窗口帧数，范围 1～30；content 模式不可用 |
| score_region | full | enhanced 专用；roi 对照模式使用高度 8%～80% 区域，非文字识别 |
| persistence_ratio | enhanced=1.5 | 持续变化/片段内变化比值门槛，0 关闭复核；enhanced 专用 |
| refine_seconds | 0 | 局部精修半径，0～0.5 秒；enhanced 专用；没有实测增益，默认关闭 |
| include_diagnostics | false | enhanced 专用；返回每个候选边界的去留原因与位置 |

## 增强模式（实验版）

```json
{
  "video_path": "desktop4-20260917-baseline/08.mp4",
  "detector": "enhanced",
  "include_diagnostics": true
}
```

流程：上游 ContentDetector 提取逐帧变化分数 → 自适应候选边界 → 前后多帧变化持续性复核
→ 可选局部精修 → 合并重复/过近候选 → 输出镜头区间。
复核使用低分辨率中部画面特征，适合尝试过滤闪动、字幕和瞬时变化，
不是语义理解，可能过滤只在边缘发生的真实变化；仍可显式选择 adaptive 使用第一版。

新增返回字段 `settings` 表示实际参数；`diagnostics` 默认空列表，显式开启后包含：
`candidate_frame`、`final_frame`、`reason`、`separation`、`persistence_ratio`。
被过滤的候选 `final_frame=null`。原因包括 kept、refined、weak_persistent_change、
nearby_weaker_boundary。这些分数不是置信概率。

2026-09-17 的 6 视频人工标注实验共比较 96 组基础配置与 36 组复核/精修配置。
按 ±0.5 秒一对一匹配，拟合该样本后的 F1 从 56.7% 到 60.1%；
按 ±0.2 秒无改善。逐视频留一选择基础参数的 F1 为 59.1%（±0.5 秒），
不是最终增强配置的独立泛化成绩。原算法 124 段，实验增强 116 段，人工 89 段。
部分差异属于人工业务合并或时间标注偏差，不能据此删除所有未匹配的真实切镜。
报告位于 `reports/desktop4-20260917-enhanced`。

本地重复实验：

```powershell
docker run --rm --cpus 2 --memory 2g `
  --mount "type=bind,source=$((Get-Location).Path),target=/workspace" `
  --mount "type=bind,source=C:\Users\admin\.codex\attachments\f9a19bfd-60b2-4027-9bf6-0e75008681ce,target=/annotations,readonly" `
  capcut-mate-local:scenedetect-0.7.1 /opt/scenedetect/bin/python `
  /workspace/scripts/calibrate_scene_detection.py `
  --annotations /annotations/pasted-text.txt `
  --baseline /workspace/reports/desktop4-20260917-baseline/results.json `
  --media-root /workspace/media --output /workspace/reports/desktop4-20260917-enhanced
```

实验脚本仅用于这批人工标注，555.mp4 的人工区间来自聊天文本，明确保存在脚本中；
生产检测代码不读取文件名、人工标注或评测结果。更换素材或提分特征算法后，
请使用新的 output 目录，避免复用旧缓存及截图。

## 返回示例

项目统一响应中间件会在 HTTP 200 响应体内加入 `code`、`message`。
调用方必须检查 `code == 0`，不能只检查 HTTP 状态码。

```json
{
  "code": 0,
  "message": "成功",
  "detector": "adaptive",
  "threshold": 3.0,
  "min_scene_len": 15,
  "fps": 25.0,
  "frame_count": 50,
  "duration_us": 2000000,
  "scene_count": 1,
  "scenes": [{
    "index": 1,
    "start_frame": 0,
    "end_frame": 50,
    "start_us": 0,
    "end_us": 2000000,
    "duration_us": 2000000,
    "start_timecode": "00:00:00.000",
    "end_timecode": "00:00:02.000"
  }],
  "warnings": [
    "Times use the reported frame rate. Normalize variable-frame-rate videos before frame-accurate editing.",
    "Shot boundaries are not semantic labels or permission to replace footage."
  ]
}
```

帧号从 0 开始，区间为左闭右开 `[start_frame, end_frame)`；`*_us` 是整数微秒。
时间基于输入报告帧率换算，不是逐帧读取容器 PTS。变帧率视频应先规范化，
再用于帧精确剪辑；元数据帧数与实际解码帧数差异较大的视频会被拒绝。
没有检测到切换但视频有效时返回一个覆盖完整视频的镜头。

错误码：

| code | 含义 |
|---|---|
| 1001 | 请求参数校验失败，沿用项目中间件 |
| 400 | 地址不受信任、路径越界、下载失败、不可解码等 |
| 404 | 本地视频不存在 |
| 409 | 容器正在执行另一个镜头检测，请稍后重试 |
| 413 | 文件大小、时长或分辨率超限 |
| 503 | 独立检测 Python 环境不可用 |
| 504 | 总处理时间超时，子进程已终止 |
| 500 | 检测进程返回内容异常 |

## 资源与部署

| 环境变量 | 默认值 |
|---|---|
| SCENE_PYTHON | /opt/scenedetect/bin/python |
| SCENE_MEDIA_ROOT | /app/media |
| SCENE_ALLOWED_MEDIA_HOSTS | 代码默认空；本地 compose 配置 nginx |
| SCENE_TIMEOUT_SECONDS | 180 秒，包含下载和检测 |
| SCENE_MAX_FILE_BYTES | 209715200（200 MiB） |
| SCENE_MAX_DURATION_SECONDS | 600 秒 |

另有分辨率像素数上限 `3840 * 2160`。这些上限不是运行时间或所有编码均可解码的保证；
当前 1 CPU/1GB 容器建议先测试短视频，不建议直接提高上限运行大型素材。
下载临时文件由 API 父进程管理，正常结束、错误、请求任务取消及检测超时均会清理。
整个容器被强制终止时的系统临时目录不在上述清理保证范围内。

修改后的源码通过 `Dockerfile.local` 复制进镜像；重建后才会进入服务：

```powershell
cd E:\project\capcut-mate
docker compose build capcut-mate
docker compose up -d --no-deps capcut-mate
```

如果经过反向代理调用，需要将其读取超时设置为超过检测总超时，或缩小素材范围。
本轮接口测试直接访问 30000 端口。批量异步任务及重启恢复留到下一阶段。

## 测试

请求/路径/可信源/超时/下载/进程锁单元测试：

```powershell
docker run --rm --mount "type=bind,source=$((Get-Location).Path)\tests,target=/app/tests,readonly" capcut-mate-local:scenedetect-0.7.1 /app/.venv/bin/python -m unittest discover -s tests -p test_scene_detection.py -v
```

真实 HTTP 测试自动生成三镜头和单镜头视频，不依赖用户样片；会在 media 与
html/output 下创建临时测试目录，结束后清理。仅应在本地可信网络执行：

```powershell
docker run --rm --network capcut-mate_capcut-mate-network `
  --mount "type=bind,source=$((Get-Location).Path)\media,target=/fixtures" `
  --mount "type=bind,source=$((Get-Location).Path)\html\output,target=/http-fixtures" `
  --mount "type=bind,source=$((Get-Location).Path)\output\scene-clips,target=/split-fixtures" `
  --mount "type=bind,source=$((Get-Location).Path)\tests,target=/tests,readonly" `
  -e SCENE_TEST_API_URL=http://capcut-mate:30000 `
  capcut-mate-local:scenedetect-0.7.1 /opt/scenedetect/bin/python `
  -m unittest discover -s /tests -p test_scene_detection_api.py -v
```

检测准确率仍需业务真实样片评估，合成视频测试仅验证功能正确性。
# 视频分析接口

## 第二阶段基础能力

第二阶段先完成模型配置管理和本地视觉特征抽取。模型配置使用现有
`VIDEO_GEN_TASK_DB_PATH` SQLite 数据库，新增表
`video_analysis_model_configs`，不会改变已有视频生成任务表。

### 模型配置接口

```http
GET    /openapi/capcut-mate/v1/model_configs
POST   /openapi/capcut-mate/v1/model_configs
PATCH  /openapi/capcut-mate/v1/model_configs/{id}
DELETE /openapi/capcut-mate/v1/model_configs/{id}
```

`api_key` 只允许写入，读取时只返回 `api_key_masked`。
`base_url` 按 OpenAI-compatible 接口约定保存，服务端不会绑定具体厂商。
模型配置保存在 `VIDEO_GEN_TASK_DB_PATH` 指向的 SQLite 数据库
（默认 `db/video_gen_tasks.sqlite3`）的 `video_analysis_model_configs` 表中。
这些接口通常由管理员调用；不要将 API Key 暴露给浏览器或普通调用方。

以下 `{BASE_URL}` 指服务地址，例如 `http://localhost:30000`。
项目成功响应会在业务字段外统一增加 `code` 和 `message`，调用方应检查 `code == 0`。

#### 查询配置

```http
GET {BASE_URL}/openapi/capcut-mate/v1/model_configs
```

```bash
curl "http://localhost:30000/openapi/capcut-mate/v1/model_configs"
```

返回 `configs` 数组；每个配置包含 `id`、`name`、`provider`、`api_key_masked`、
`base_url`、`model`、`enabled`、`is_default`、`created_at` 和 `updated_at`。
API Key 不会以明文返回。

#### 创建配置

```http
POST {BASE_URL}/openapi/capcut-mate/v1/model_configs
Content-Type: application/json
```

```json
{
  "name": "视频分析模型",
  "provider": "openai_compatible",
  "api_key": "YOUR_API_KEY",
  "base_url": "https://api.example.com/api/v3",
  "model": "your-video-model",
  "enabled": true,
  "is_default": true
}
```

字段说明：`name` 为配置名；`provider` 默认为 `openai_compatible`；
`api_key`、`base_url`、`model` 为必填项；`enabled` 控制是否可用；
`is_default` 将此配置设为默认配置。一个时刻最多只有一个默认配置。
所选供应商及模型须兼容项目使用的 Responses API、Files API 和视频输入能力。

#### 更新配置

```http
PATCH {BASE_URL}/openapi/capcut-mate/v1/model_configs/{id}
Content-Type: application/json
```

只提交需要修改的字段；例如轮换 API Key 并设为默认配置：

```json
{
  "api_key": "NEW_API_KEY",
  "enabled": true,
  "is_default": true
}
```

#### 删除配置

```http
DELETE {BASE_URL}/openapi/capcut-mate/v1/model_configs/{id}
```

成功响应包含 `deleted: true` 和被删除配置的 `id`。不存在的配置返回错误。

#### 分析任务如何选择模型

提交 `analyze_video` 或 `analyze_video_tasks` 时可以传 `model_config_id`：

```json
{
  "video_path": "uploads/example.mp4",
  "model_config_id": 2,
  "use_external_model": true
}
```

指定 ID 时使用对应的已启用配置；省略时选择已启用的默认配置，
若没有显式默认项则选择 ID 最小的已启用配置。配置未启用或
`use_external_model=false` 时不会调用外部多模态模型。
结果中的 `model_config_id` 和 `model_used` 标识实际使用的配置和模型。

### 本地视觉特征接口

```http
POST /openapi/capcut-mate/v1/extract_features
```

该接口复用现有分镜检测，抽取每个分镜的首/中/尾（或仅中点），返回亮度、
饱和度、边缘密度、运动分数和采样时间点。它是语义模型分析前的稳定输入层，
当前不会伪造人物、商品或“爆量原因”等语义结论。

## 异步视频分析任务

长视频分析使用 NAS SQLite 任务表和后台 Worker。不同视频任务的最大并发由 `VIDEO_ANALYSIS_TASK_CONCURRENCY` 控制，默认 2、上限 8；逐镜模型调用由 `SCENE_MODEL_CONCURRENCY` 控制，默认 4。worker 通过数据库原子领取任务；同一任务不会被重复领取。多个分析任务共享本地媒体 worker 锁，排队等待时间由 `SCENE_WORKER_LOCK_WAIT_SECONDS` 控制，默认 1800 秒。

多台服务器共用 NAS 时，在每台服务器项目根目录的 `env.db.dev` 配置唯一节点：

```dotenv
VIDEO_ANALYSIS_NODE_ID=server-a
```

配置后，分析、脚本分镜、批量草稿、音频和数字人任务库保存到
`video-analysis/database/nodes/{node_id}/`。每台服务器只恢复和执行自己的任务，
上传文件、报告、分镜视频和草稿仍可共用 NAS。现有接口无需传 `node_id`，任务应向
原提交服务器查询。同一个 `node_id` 仍只允许一个 API 进程持有 Worker 锁；不同服务器
不得配置相同节点值。未配置时继续使用原任务库路径和原单节点行为。历史任务可继续查询，
但历史队列不会被新节点自动领取，避免重复调用模型或重复生成草稿。

```http
POST /openapi/capcut-mate/v1/analyze_video_tasks
GET  /openapi/capcut-mate/v1/analyze_video_tasks/{task_id}
GET  /openapi/capcut-mate/v1/analyze_video_tasks/{task_id}/result
```

任务提交后立即返回 `task_id`。状态接口返回 `queued`、`running`、
`completed`、`completed_with_warnings` 或 `failed`，并提供当前阶段和进度。
结果文件保存在 `reports/video-analysis/{task_id}.json`，数据库表为
`video_analysis_tasks`。Docker 已将 `./db` 和 `./reports` 挂载到容器，
重启后任务记录和结果仍然保留；`queued/running` 任务会在服务启动时恢复排队。

### 统一视频分析接口

```http
POST /openapi/capcut-mate/v1/analyze_video
```

当前统一返回人物/商品/场景、OCR 字段、镜头语义、相邻镜头衔接、
吸睛镜头和可替换镜头结果。没有配置多模态模型时使用本地可复现的视觉
启发式；配置模型后保留 `model_config_id` 和 `model_used` 作为增强入口。
爆量分析默认只从视频自身的剪辑节奏、画面设计、脚本结构和情绪变化出发，
不要求接入平台播放数据。
# 分镜视频输入模式（2026-09-23）

综合分析现使用 Files API 上传每个分镜，等待 `active` 后将 `input_video.file_id`
提交至 Responses API。每个视频只上传一次；上一镜的 ID 复用于下一镜的衔接判断。
此模式要求模型和供应商同时支持 Files API 与视频输入，不自动回退到 Base64 图片。

- 本地仅检测镜头和按帧切割、验证视频；不运行 OCR、关键帧视觉统计及本地语义评分。
- `use_transcription` 仍控制 large-v3 转写，转写时间表复用镜头检测结果。
- `video_fps` 默认 2，表示云端采样频率；不是原视频帧率，也不保证每个动作均被观察。
- `sample_points`、`include_sample_images`、`use_ocr` 为兼容旧请求保留，
  在综合分析中不启用对应处理。独立 `extract_features` 接口不变。
- `visual_features` 返回空数组。`semantics[].analysis_status` 为
  `completed` / `failed` / `disabled`，必须先判断状态再使用 `replaceable`。
  失败时的默认 false/0 不是模型“不可替换”的结论。
- `hook_score` 和 `viral_analysis.scene_scores` 统一为模型给出的 0～100 分；
  `replace_score`、`continuity_score` 为 0～1。转场无法判断时为 unknown/null。
- 综合 `viral_analysis.score` 仅为成功镜头分数的均值，非播放量预测；
  `hook_scenes` 为成功镜头中的前三名，相对排名不代表高吸引力。
- 模型失败后不再执行本地规则兜底，保留成功镜头，并明确提示未完成判断。
- 全局报告保留原始镜头时间和逐镜替换结论，仅补充创作描述。
- `analysis_report.timings` 返回检测/切割、转写、逐镜上传与处理、
  逐镜模型、全局模型及总耗时。上传有并发上限，逐镜模型按受控并发运行。
- 临时视频在任务结束后清理；仅删除本任务上传的远端文件，并在上传时申请
  4 天过期作为异常退出兜底（覆盖火山 Responses 的默认期限校验）。
  正常完成后立即删除，不等待过期；清理失败会返回警告。

```powershell
python scripts/test_analysis_task.py --video-path "desktop4-20260917-baseline/08.mp4" --model-config-id 1 --video-fps 2
```

## 模型流式通信与隔离

逐镜分析和全局报告统一引用 `responses_client.py`，后端接收 SSE 后返回完整 JSON。
视频上传、`file_id` 输入、转写、切割、草稿与对外提交/查询接口保持原有流程。
当前主流程的全局分析使用逐镜结论文本，保留的文件报告调用函数同样支持流式。

每个分镜拥有独立 Scope、请求 ID、正文缓冲、回调和重试记录；
全局报告、脚本分镜不会共用这些运行状态。模型配置仅用于读取连接信息。
线程中的进度事件交回视频任务所属事件循环统一汇总，接收进度约每五秒报告一次，
不把正文、思考内容或密钥写入日志/任务记录。

`models_used.scene.calls` 和 `models_used.global.calls` 包含 `call_id`、
`scene_index`、`attempt`、`first_event_seconds`、`first_text_seconds`、
`seconds`、`response_id`、`usage` 和 `outcome`。
任务实时事件通过现有 timings 记录；模型失败仍保留成功分镜和警告。
网络最多重试一次；业务层仅对完整结果结构错误再修正一次，避免双层网络重试。
超时配置和预算边界见 `docs/online-model-migration.zh.md`。
