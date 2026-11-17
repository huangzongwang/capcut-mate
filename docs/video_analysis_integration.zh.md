# 视频分析接口接入文档

> 当前完整接口字段、响应结构及调用示例请使用 [视频分析 API 接口文档](video-analysis-api.zh.md)。
> 本文保留历史接入说明，若与新文档冲突，以新文档和当前代码为准。

> 2026-10-29：新增 Gemini 原生视频输入、GPT Responses 文本汇总及逐镜/全局备用模型。
> 请求字段、协议选择与切换规则见 [模型协议与备用模型](video-analysis-model-fallback.zh.md)。

> 2026-10-14 模型接口升级：本文旧 model_config_id 和本地模型增删改示例已废弃。
> 新请求使用 scene_model_id / global_model_id，配置只读线上数据库；
> 当前模型接口约定以 `docs/online-model-migration.zh.md` 为准。

本文面向需要从其他项目调用 CapCut Mate 视频分析能力的服务端开发者。接口根路径：

```text
{BASE_URL}/openapi/capcut-mate/v1
```

例如本地部署的 `{BASE_URL}` 可以是 `http://localhost:30000`。分析接口没有在本文定义独立的调用方身份认证方式；对公网提供服务时，应由部署方在网关配置认证、限流和访问控制。

## 接入流程

长视频或生产环境优先使用异步任务：

1. 调用 `POST /upload_video` 上传本地视频文件，保存响应中的 `video_path`。
2. 管理员通过模型配置接口保存并启用支持 OpenAI-compatible API、Files API 和视频输入的模型。
3. 调用 `POST /analyze_video_tasks`，在请求体中传入 `video_path` 和分析选项，保存返回的 `task_id`。
4. 轮询 `GET /analyze_video_tasks/{task_id}`，直到状态为 `completed`、`completed_with_warnings` 或 `failed`。
5. 成功或部分成功时调用 `GET /analyze_video_tasks/{task_id}/result` 获取结果。

异步任务分析完成后会自动尝试创建剪映草稿。这是当前任务接口的附带行为；草稿创建失败不会使分析结果丢失，但会出现在警告中。若调用方不需要该副作用，可使用同步 `POST /analyze_video`，但请求连接需等待整个分析完成，长任务不建议使用。

## 通用约定

- 方法和路径见各接口说明；请求和响应均使用 UTF-8 JSON。
- 成功响应为 HTTP 200，响应体在业务字段外增加 `code` 和 `message`。调用方必须判断 `code == 0`，不能只看 HTTP 状态码。
- 项目响应中间件可能把参数、业务和服务错误也包装为 HTTP 200；非零 `code` 表示失败。
- `video_path` 和 `video_url` 必须且只能传一个。
- `video_path` 是服务端 `SCENE_MEDIA_ROOT` 下的相对路径，不是调用方机器上的路径。
- `/upload_video` 返回的 `video_path` 已位于服务端素材根目录内，可直接用于分析。
- `video_url` 只能使用部署方明确配置为可信的 HTTP(S) 主机。服务端不跟随重定向、不使用环境代理；不要把用户可控制的主机加入允许列表。
- 默认限制：文件不超过 200 MiB、时长不超过 600 秒、分辨率不超过 3840×2160。具体部署值以服务端配置为准。
- 视频时间字段以微秒（`*_us`）表示；镜头帧区间采用左闭右开 `[start_frame, end_frame)`。变帧率素材用于帧精确剪辑前应先规范化。

### 错误处理

常见响应业务码包括：

| 情况 | 处理建议 |
|---|---|
| `code == 0` | 请求成功，读取接口返回字段 |
| `code == 1001` | 参数校验失败，检查字段、范围及视频来源是否恰好提供一个 |
| `code == 404` | 视频文件或任务不存在 |
| `code == 409` | 任务结果尚未就绪，或资源正忙；稍后重试 |
| `code == 413` | 视频超过服务端文件大小、时长或分辨率限制 |
| `code == 503` | 分析运行环境不可用 |
| `code == 504` | 处理超时 |
| 其他非零值 | 读取 `message`，结合服务端日志排查 |

## 模型配置

模型配置是服务端级配置，通常由管理员预先创建。API Key 只在写入请求中出现；查询结果只返回掩码值。

### 查询配置

```http
GET /model_configs
```

成功响应字段为 `configs` 数组，每项包含 `id`、`name`、`provider`、`api_key_masked`、`base_url`、`model`、`enabled`、`is_default`、`created_at` 和 `updated_at`。

### 创建配置

```http
POST /model_configs
Content-Type: application/json
```

```json
{
  "name": "视频分析模型",
  "provider": "openai_compatible",
  "api_key": "YOUR_API_KEY",
  "base_url": "https://api.example.com/v1",
  "model": "video-model",
  "enabled": true,
  "is_default": true
}
```

成功时返回配置对象，含服务端生成的 `id` 和 `api_key_masked`，不会回传明文 Key。`base_url` 应填写供应商要求的 OpenAI-compatible API 根地址。

### 更新配置

```http
PATCH /model_configs/{id}
Content-Type: application/json
```

只传需要更新的字段。例如：

```json
{
  "api_key": "NEW_API_KEY",
  "enabled": true,
  "is_default": true
}
```

### 删除配置

```http
DELETE /model_configs/{id}
```

成功响应包含 `deleted: true` 和被删除的 `id`。

## 统一视频分析

### 上传视频

```http
POST /upload_video
Content-Type: multipart/form-data
```

表单字段名为 `file`，值为视频文件。成功响应：

```json
{
  "code": 0,
  "message": "成功",
  "video_path": "uploads/4b7a2f8d63ea4c8b99d603c947429fa1.mp4",
  "filename": "4b7a2f8d63ea4c8b99d603c947429fa1.mp4",
  "size_bytes": 12345678
}
```

将返回的 `video_path` 原样传入 `POST /analyze_video_tasks`。文件保存在服务端素材目录，当前没有配套删除接口；部署方需管理上传文件的保留和磁盘空间。上传大小上限使用 `SCENE_MAX_FILE_BYTES`（默认 200 MiB）。公网部署还应在反向代理设置请求体大小上限。

### 请求字段

`POST /analyze_video` 和 `POST /analyze_video_tasks` 使用相同请求体。

```http
POST /analyze_video_tasks
Content-Type: application/json
```

```json
{
  "video_path": "uploads/4b7a2f8d63ea4c8b99d603c947429fa1.mp4",
  "detector": "enhanced",
  "use_external_model": true,
  "model_config_id": 1,
  "use_transcription": true,
  "video_fps": 2
}
```

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `video_path` | string | 无 | 服务端素材根目录内的相对路径；与 `video_url` 二选一 |
| `video_url` | string | 无 | 服务端允许列表中的可信视频地址；与 `video_path` 二选一 |
| `detector` | string | `enhanced` | `adaptive`、`content` 或 `enhanced` 镜头检测模式 |
| `threshold` | number | 按模式决定 | 大于 0 且不超过 255；越低通常检出更多切镜 |
| `min_scene_len` | integer | `15` | 最短镜头间隔，单位帧，范围 1～1500 |
| `min_content_val` | number | 按模式决定 | adaptive/enhanced 检测参数 |
| `window_width` | integer | 按模式决定 | adaptive/enhanced 前后分析窗口，范围 1～30 帧 |
| `score_region` | string | `full` | enhanced 模式使用 `full` 或 `roi` |
| `persistence_ratio` | number | enhanced 默认 `1.5` | enhanced 持续变化复核参数；设为 0 关闭该复核 |
| `refine_seconds` | number | `0` | enhanced 局部精修半径，范围 0～0.5 秒 |
| `include_diagnostics` | boolean | `false` | 是否返回 enhanced 候选边界诊断 |
| `video_fps` | number | `2` | 云端视频采样频率，范围大于 0 且不超过 5；不是原视频帧率 |
| `model_config_id` | integer | 默认配置 | 使用指定模型配置；省略时选择服务端默认启用配置 |
| `use_external_model` | boolean | `true` | 是否运行外部多模态模型分析 |
| `use_transcription` | boolean | `true` | 是否运行本地 faster-whisper large-v3 转写 |
| `sample_points` | string/integer | `auto` | 为兼容特征接口保留；统一分析链路不使用 |
| `include_sample_images` | boolean | `false` | 为兼容特征接口保留；统一分析链路不使用 |
| `use_ocr` | boolean | `false` | 为兼容旧请求保留；统一分析链路不运行 OCR |

`detector=content` 不接受 `min_content_val` 和 `window_width`；`score_region`、`persistence_ratio`、`refine_seconds`、`include_diagnostics` 仅适用于 `enhanced`。

配置了 `model_config_id` 时，该配置必须已启用且请求 `use_external_model=true` 才会调用模型。没有可用模型配置时仍可完成镜头检测和请求的转写，但不会产生模型语义判断。

### 同步调用

```http
POST /analyze_video
```

响应直接包含完整分析结果。该接口会一直等待分析结束，不适合容易发生网关超时的调用链。

### 异步提交

```http
POST /analyze_video_tasks
```

成功响应示例：

```json
{
  "code": 0,
  "message": "成功",
  "task_id": "va_20261006_035111_f7e1ddf1",
  "status": "queued",
  "stage": "queued",
  "progress": 0,
  "created_at": "2026-10-06T03:51:11+00:00"
}
```

### 查询任务状态

```http
GET /analyze_video_tasks/{task_id}
```

主要字段：

| 字段 | 说明 |
|---|---|
| `status` | `queued`、`running`、`completed`、`completed_with_warnings` 或 `failed` |
| `stage` | 当前处理阶段，例如 `preparing_scene_videos`、`transcribing`、`uploading_scene_video`、`analyzing_scene_video`、`global_model_analysis`、`creating_draft`、`completed` |
| `progress` | 0～100 的进度值，为阶段性估算 |
| `current_scene`、`total_scenes` | 当前/总镜头数，可能为空 |
| `error_message` | 失败原因或完成警告；完成但有警告时仍可取结果 |
| `created_at`、`started_at`、`completed_at`、`heartbeat_at` | 任务时间信息 |
| `timings` | 已记录的阶段耗时与事件；处理中可能持续更新 |

服务端当前使用单并发后台 Worker，队列中的任务会依次处理。轮询间隔建议从 2～5 秒开始，并逐步退避；不要高频轮询。

### 获取任务结果

```http
GET /analyze_video_tasks/{task_id}/result
```

仅当状态为 `completed` 或 `completed_with_warnings` 时可取。成功响应包含：

```json
{
  "code": 0,
  "message": "成功",
  "task_id": "va_20261006_035111_f7e1ddf1",
  "status": "completed",
  "result": {
    "detection": {},
    "transcript": null,
    "visual_features": [],
    "semantics": [],
    "transitions": [],
    "viral_analysis": {},
    "scene_captions": [],
    "model_config_id": 1,
    "model_used": "video-model",
    "analysis_report": {},
    "draft": {},
    "warnings": []
  }
}
```

`result` 对象会随任务包含详细数组和报告字段。集成时应按需读取字段，并允许新增字段，避免将响应结构写成封闭 schema。

## 结果字段说明

- `detection.scenes`：镜头区间、帧号、微秒时间、时间码及检测参数。
- `transcript`：开启转写后包含逐段/逐词时间戳、按镜头聚合文本和 SRT；关闭时为 `null`。
- `semantics`：逐镜模型判断。先检查 `analysis_status`（`completed`、`failed`、`disabled`），失败或未启用时不能把默认的 `replaceable=false`、`replace_score=0` 当成模型结论。
- `transitions`：相邻镜头衔接判断；无法判断时类型可为 `unknown`，分数可为 `null`。
- `viral_analysis`：逐镜 `hook_score` 范围为 0～100；汇总 `score` 是成功镜头分数的均值，不是播放量或爆款概率预测。`hook_scenes` 是相对排名，不保证分数高。
- `scene_captions`：按镜头整理的字幕数据，时间单位为微秒。
- `analysis_report`：整体内容与镜头分析报告，并包含 `timings`。
- `warnings`：部分镜头、全局报告或自动建草稿失败等非致命问题。
- `draft`：异步任务自动创建的剪映草稿信息；同步分析通常为空对象。

## 独立分析辅助接口

以下接口适用于只需要某个单项能力的调用方，不需要创建统一分析任务。

### 镜头检测

```http
POST /detect_scenes
```

请求体使用统一分析请求中的视频来源和检测参数。响应返回镜头区间、FPS、总帧数、时长及警告；该接口只检测边界，不进行语义分析或切割导出。

### 本地语音转写

```http
POST /transcribe_video
```

除视频来源和检测参数外，可传：

```json
{
  "video_url": "https://media.example.com/videos/demo.mp4",
  "model_name": "large-v3",
  "language": "zh",
  "word_timestamps": true,
  "vad_filter": true
}
```

返回转写片段、词级时间戳、按镜头映射的文本、检测结果和 SRT。当前只支持 `large-v3`。

### 本地视觉特征

```http
POST /extract_features
```

在视频来源和检测参数外，可传 `sample_points`（`auto`、`1` 或 `3`）、`include_sample_images` 和 `include_ocr`。返回每个镜头的亮度、饱和度、边缘密度、运动分数及可选 OCR/采样图；这是视觉统计与文字抽取，不等同于人物、商品或场景语义判断。

## 最小调用示例

以下示例使用 curl；将服务地址和本地视频路径替换为实际值。

```bash
BASE_URL="http://localhost:30000/openapi/capcut-mate/v1"

UPLOAD=$(curl -sS -X POST "$BASE_URL/upload_video" \
  -F "file=@./demo.mp4")
VIDEO_PATH=$(printf '%s' "$UPLOAD" | jq -r '.video_path')

curl -sS -X POST "$BASE_URL/analyze_video_tasks" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg path "$VIDEO_PATH" '{
    video_path: $path,
    "detector": "enhanced",
    "use_external_model": true,
    "use_transcription": true,
    "video_fps": 2
  }')"
```

提交成功后，使用返回的 `task_id` 请求状态接口；终态成功后再请求结果接口。
