# 视频分析 API 接口文档

更新日期：2026-10-29  
适用版本：支持 Gemini 原生视频、GPT Responses 文本及主备模型切换的当前代码版本。

## 1. 接口概览

接口根路径：

```text
{BASE_URL}/openapi/capcut-mate/v1
```

本文后续路径均相对于该根路径。调用方必须使用已加载本次适配代码的服务实例。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/upload_video` | 上传待分析视频 |
| GET | `/models` | 查询线上模型目录 |
| POST | `/analyze_video` | 同步分析，等待返回完整结果 |
| POST | `/analyze_video_tasks` | 提交异步分析任务 |
| GET | `/analyze_video_tasks/{task_id}` | 查询任务状态和进度 |
| GET | `/analyze_video_tasks/{task_id}/result` | 获取已完成任务的分析结果 |

推荐调用顺序：

```text
上传视频或准备可信视频地址
  → 查询并选择模型
  → 提交异步任务
  → 轮询任务状态
  → 获取分析结果
```

同步和异步接口使用相同的分析请求字段。异步任务完成分析后会自动尝试创建剪映草稿；
同步接口只返回分析结果，不自动创建草稿。草稿创建失败时，异步任务仍保留分析结果。

## 2. 通用约定

- 除文件上传外，请求使用 `Content-Type: application/json`。
- 响应业务字段与 `code`、`message` 位于同一层，不统一包在 `data` 内。
- 成功响应 `code=0`。必须同时检查 HTTP 状态和业务 `code`，不能仅检查 HTTP 200。
- 当前主应用的中间件会将许多 HTTP 错误包装为 HTTP 200、非零 `code`；
  网关或其他部署仍可能直接返回非 2xx 状态。
- `Accept-Language: zh` 或 `en` 可控制通用响应消息语言。
- 模型 ID 是线上模型目录整数 ID，不是模型名称，也不是供应商 ID。
- 模型凭证由服务端读取；本接口不接收调用方传入的模型 API Key。
- 时间轴中 `*_us` 表示微秒；转写片段及词级 `start`、`end` 表示秒。
- 帧区间及镜头时间范围采用左闭右开语义。
- 文档中的模型 ID、文件名、任务 ID 均为示例，使用前应查询当前部署的模型目录。

## 3. 上传视频

```http
POST /upload_video
Content-Type: multipart/form-data
```

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `file` | 文件 | 是 | 无 | 待分析视频，建议 MP4 |
| `purpose` | string | 否 | `video-analysis` | 视频分析使用该值；`digital-human` 属于另一个业务目录 |

示例：

```bash
curl -X POST "$BASE_URL/openapi/capcut-mate/v1/upload_video" \
  -F "file=@./example.mp4" \
  -F "purpose=video-analysis"
```

响应：

```json
{
  "code": 0,
  "message": "成功",
  "video_path": "uploads/0123456789abcdef0123456789abcdef.mp4",
  "purpose": "video-analysis",
  "filename": "0123456789abcdef0123456789abcdef.mp4",
  "size_bytes": 3577700
}
```

后续原样传入 `video_path`，不要拼成调用方本机路径。
视频上传默认限制 200 MiB，可由服务端 `SCENE_MAX_FILE_BYTES` 修改。
上传成功仅代表文件已保存，不代表视频已完成解码或分析。

## 4. 查询模型

```http
GET /models
```

响应示例：

```json
{
  "code": 0,
  "message": "成功",
  "models": [
    {
      "id": 31,
      "provider_id": 10,
      "model": "gemini-2.5-flash-lite",
      "name": "Gemini 视频分析",
      "provider": "zetarouter_gemini",
      "enabled": true,
      "capabilities": [],
      "model_updated_at": "2026-10-29 09:00:00",
      "provider_updated_at": "2026-10-29 09:00:00"
    }
  ]
}
```

仅选择 `enabled=true` 的模型。目录中出现模型不代表上游账号具有调用权限；
`capabilities` 是目录标签，当前分析接口不会依靠它阻止调用，也不会证明实际视频能力。

服务端不返回 API Key、供应商地址或任意 metadata。
旧 `/model_configs` 本地增删改接口已停用，返回业务码 `410`；
模型配置由线上管理平台维护。

## 5. 分析请求字段

适用于：

```http
POST /analyze_video
POST /analyze_video_tasks
```

### 5.1 常用字段

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `video_path` | string / null | 与 URL 二选一 | `null` | 服务端素材根目录下的相对路径，长度 1–2048 |
| `video_url` | string / null | 与路径二选一 | `null` | 可信 HTTP(S) 视频地址，长度 1–8192 |
| `scene_model_id` | integer / null | 外部分析开启时必填 | `null` | 主视频模型 ID，≥1 |
| `scene_fallback_model_id` | integer / null | 否 | `null` | **新增**：备用视频模型 ID，≥1 |
| `global_model_id` | integer / null | 外部分析开启时必填 | `null` | 主文本汇总模型 ID，≥1 |
| `global_fallback_model_id` | integer / null | 否 | `null` | **新增**：备用文本汇总模型 ID，≥1 |
| `use_external_model` | boolean | 否 | `true` | 是否执行外部模型语义分析和汇总 |
| `use_transcription` | boolean | 否 | `true` | 是否本地逐镜语音转写 |
| `detector` | string | 否 | `enhanced` | `adaptive`、`content`、`enhanced`、`auto` |
| `video_fps` | number | 否 | `2.0` | 旧 Files 视频路径的云端采样参数，0 < 值 ≤5；Gemini 原生路径不使用此参数 |

约束：

1. `video_path` 与 `video_url` 必须且只能指定一个，不能传空字符串。
2. 本地路径必须位于服务端 `SCENE_MEDIA_ROOT` 内，不能使用绝对路径或越界路径。
3. URL 的主机及显式端口必须在 `SCENE_ALLOWED_MEDIA_HOSTS` 中；
   不允许 URL 用户名、密码或 fragment，不跟随重定向。
4. 开启外部分析时，两个主模型字段都必填。两个备用字段独立可选。
5. 备用模型不能与对应主模型相同；关闭外部分析时不能指定备用模型。
6. 所有指定模型在执行前校验，异步任务也会校验排队期间配置是否变化。
   缺失、禁用、凭证错误或配置版本变化属于配置错误，不触发运行时备用切换。
7. 旧字段 `model_config_id` 已被拒绝，不能与新字段混用。
8. 这里的 `video_url` 是服务端下载源视频的地址，不代表原样把 URL 交给 Gemini。

### 5.2 检测及兼容字段

| 字段 | 类型 | 默认值 | 范围与用途 |
| --- | --- | --- | --- |
| `threshold` | number / null | `null` | 0 < 值 ≤255；未传时 content 为 27，其余为 3 |
| `min_scene_len` | integer | `15` | 1–1500，最短镜头帧数 |
| `min_content_val` | number / null | `null` | 0–255；enhanced 默认 30，其他适用检测器默认 15 |
| `window_width` | integer / null | `null` | 1–30；enhanced/auto 默认 4，adaptive 默认 2 |
| `score_region` | string | `full` | `full` / `roi`；ROI 仅适用于 enhanced |
| `persistence_ratio` | number / null | `null` | 0–20；enhanced/auto 默认 1.5 |
| `refine_seconds` | number | `0.0` | 0–0.5；非零仅适用于 enhanced |
| `include_diagnostics` | boolean | `false` | 仅 enhanced/auto 支持开启 |
| `sample_points` | `"auto"` / integer | `"auto"` | 兼容字段，仅允许 `"auto"`、1、3；统一分析不走图片特征提取 |
| `include_sample_images` | boolean | `false` | 兼容字段；统一分析中强制关闭 |
| `include_ocr` | boolean | `false` | 兼容字段；统一分析中强制关闭 |
| `use_ocr` | boolean | `false` | 兼容字段；统一分析不执行 OCR |

`content` 不接受显式 `min_content_val`、`window_width`；
`adaptive/content` 不接受 enhanced 后处理参数；
`auto` 不接受 `score_region=roi` 或非零 `refine_seconds`。
通常保留检测默认参数即可。

### 5.3 请求示例

原有火山调用，无备用模型：

```json
{
  "video_path": "uploads/example.mp4",
  "scene_model_id": 14,
  "global_model_id": 13,
  "use_transcription": true
}
```

Gemini 视频分析、火山视频备用，GPT 文本汇总、另一个文本模型备用：

```json
{
  "video_path": "uploads/example.mp4",
  "scene_model_id": 31,
  "scene_fallback_model_id": 14,
  "global_model_id": 16,
  "global_fallback_model_id": 13,
  "use_external_model": true,
  "use_transcription": true,
  "detector": "enhanced"
}
```

使用源视频地址：

```json
{
  "video_url": "https://media.example.com/videos/example.mp4",
  "scene_model_id": 31,
  "scene_fallback_model_id": 14,
  "global_model_id": 16,
  "global_fallback_model_id": 13
}
```

仅本地检测、转写：

```json
{
  "video_path": "uploads/example.mp4",
  "use_external_model": false,
  "use_transcription": true
}
```

## 6. 同步分析

```http
POST /analyze_video
Content-Type: application/json
```

请求体见第 5 节。服务端执行完成后直接返回：

```json
{
  "code": 0,
  "message": "成功",
  "detection": {},
  "transcript": null,
  "visual_features": [],
  "semantics": [],
  "transitions": [],
  "viral_analysis": {},
  "scene_captions": [],
  "scene_model_id": 31,
  "global_model_id": 16,
  "global_model_used_id": 16,
  "model_used": "gemini-2.5-flash-lite",
  "models_used": {},
  "fallbacks": [],
  "analysis_report": {},
  "draft": {},
  "warnings": []
}
```

这是结构示意，嵌套对象及列表内容已省略，不是完整成功样本。
调用方和网关须允许足够的请求等待时间；耗时较长时使用异步接口。

## 7. 异步任务

### 7.1 提交

```http
POST /analyze_video_tasks
Content-Type: application/json
```

请求体见第 5 节。提交响应：

```json
{
  "code": 0,
  "message": "成功",
  "task_id": "va_20261029_090000_a1b2c3d4",
  "status": "queued",
  "stage": "queued",
  "progress": 0,
  "created_at": "2026-10-29T09:00:00+00:00"
}
```

提交后可能已开始执行，不能依赖首次响应一定为 `queued`。
提交接口没有幂等键；重复提交会创建独立任务，可能重复调用模型并生成草稿。

### 7.2 查询状态

```http
GET /analyze_video_tasks/{task_id}
```

```json
{
  "code": 0,
  "message": "成功",
  "task_id": "va_20261029_090000_a1b2c3d4",
  "status": "running",
  "stage": "analyzing_scene_video",
  "progress": 60,
  "current_scene": null,
  "total_scenes": null,
  "error_message": "",
  "created_at": "2026-10-29T09:00:00+00:00",
  "started_at": "2026-10-29T09:00:01+00:00",
  "completed_at": null,
  "heartbeat_at": "2026-10-29T09:00:15+00:00",
  "timings": {}
}
```

| `status` | 含义 | 是否继续轮询 |
| --- | --- | --- |
| `queued` | 排队中 | 是 |
| `running` | 执行中 | 是 |
| `completed` | 完成 | 否，获取结果 |
| `completed_with_warnings` | 完成但有警告 | 否，获取结果并检查警告 |
| `failed` | 任务失败 | 否，读取 `error_message` |

建议每 2–5 秒轮询，并为调用方设置总等待期限。
`progress` 为阶段进度，不是精确剩余时间；`current_scene`、`total_scenes`
当前可能一直为 `null`，不要依靠它们判断任务是否正常。

常见 `stage`：`queued`、`preparing`、`models_resolved`、
`preparing_scene_videos`、`transcribing`、`uploading_scene_video`、
`analyzing_scene_video`、`scene_model_fallback`、`global_model_analysis`、
`global_model_fallback`、`reporting`、`creating_draft`、`completed`、`failed`。
`stage` 不是稳定枚举；终态判断应使用 `status`。

`timings` 可包含阶段耗时及事件，不同协议、不同执行路径的键可能不同。
`error_message` 在带警告完成时也可能有内容，不表示整个任务失败。
服务重启后，中断的运行中任务会标记失败而非自动重复执行；排队任务可恢复。

### 7.3 获取结果

```http
GET /analyze_video_tasks/{task_id}/result
```

```json
{
  "code": 0,
  "message": "成功",
  "task_id": "va_20261029_090000_a1b2c3d4",
  "status": "completed_with_warnings",
  "result": {
    "scene_model_id": 31,
    "global_model_id": 16,
    "global_model_used_id": 13,
    "fallbacks": [],
    "semantics": [],
    "analysis_report": {},
    "draft": {},
    "warnings": []
  }
}
```

示例省略了部分结果字段。`result` 的实际结构与同步分析的业务结果相同，
不再嵌套 `code`、`message`。
只有 `completed`、`completed_with_warnings` 可获取结果；
其他状态，包括 `failed`，结果接口返回业务码 `409`。

## 8. 分析结果字段

### 8.1 顶层

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `detection` | object | 检测器、帧率、总帧数、总时长及镜头边界 |
| `transcript` | object / null | 转写片段、词级时间、逐镜台词及 SRT；未转写时为 null |
| `visual_features` | array | 当前统一分析返回空数组 |
| `semantics` | object[] | 每个镜头的语义分析，失败镜头仍保留一条 |
| `transitions` | object[] | 相邻镜头的衔接判断 |
| `viral_analysis` | object | 逐镜吸睛分、最高分镜头及汇总分 |
| `scene_captions` | object[] | 按镜头整理的字幕，时间为微秒 |
| `scene_model_id` | integer / null | 配置的主视频模型 ID |
| `global_model_id` | integer / null | 配置的主文本模型 ID |
| `model_used` | string / null | 配置的主视频模型名称，不是逐镜实际执行模型 |
| `global_model_used_id` | integer / null | **新增**：实际成功生成全局报告的模型 ID |
| `models_used` | object | 主备配置快照和调用记录 |
| `fallbacks` | object[] | **新增**：主备切换记录 |
| `analysis_report` | object | 综合报告及生产建议 |
| `draft` | object | 异步任务自动创建的草稿信息；同步接口默认空对象 |
| `warnings` | string[] | 失败、降级或说明性警告 |

不能仅凭任务 `completed` 或 `completed_with_warnings` 判断模型全部成功。
应同时检查 `semantics[].analysis_status`、
`analysis_report.global_model_enriched` 以及 `warnings`。

### 8.2 镜头边界 `detection`

| 字段 | 说明 |
| --- | --- |
| `detector`、`threshold`、`min_scene_len` | 检测配置 |
| `fps`、`frame_count`、`duration_us`、`scene_count` | 视频与镜头统计 |
| `scenes[].index` | 镜头索引 |
| `scenes[].start_frame`、`end_frame` | 帧区间 |
| `scenes[].start_us`、`end_us`、`duration_us` | 时间区间与时长 |
| `scenes[].start_timecode`、`end_timecode` | 可读时间码 |
| `settings`、`diagnostics`、`warnings` | 额外配置、诊断与提示 |

### 8.3 逐镜分析 `semantics[]`

| 字段 | 类型 / 范围 | 含义 |
| --- | --- | --- |
| `scene_index` | integer | 镜头索引 |
| `analysis_status` | string | `completed` / `failed` / `disabled` |
| `analysis_source` | string | 当前为 `video_model` |
| `model_id` | integer / null | **新增**：实际成功使用的模型 ID |
| `fallback_used` | boolean | **新增**：是否由备用模型成功完成 |
| `content` | string | 具体画面描述 |
| `scene_category` | string | 场景分类，见下文 |
| `scene_type`、`shot_type` | string | 场景类型与景别 |
| `camera_motion`、`camera_angle` | string | 运镜与机位 |
| `composition`、`lighting` | string | 构图与光照 |
| `people`、`products` | string[] | 人物与产品 |
| `actions`、`emotions`、`clothing`、`environment` | string[] | 动作、情绪、服饰与环境 |
| `semantic_confidence` | number | 模型给出的语义置信度 |
| `replaceable` | boolean | 是否可自动替换 |
| `replace_score` | number，0–1 | 替换分 |
| `replacement_type`、`replace_reason` | string | 替换类型与理由 |
| `hook_score` | number，0–100 / null | 吸睛分；未完成时可为 null |
| `hook_reason` | string | 吸睛评分理由 |
| `transition_type`、`transition_reason` | string | 与上一镜头的衔接类型和理由 |
| `continuity_score` | number，0–1 / null | 连续性评分 |
| `dialogue`、`dialogue_inferred` | string、boolean | 模型输出的台词及推断标记，不应替代正式转写字幕 |
| `ocr_text` | array | 当前统一分析为空数组 |

场景分类及后处理规则：

| `scene_category` | 分类 | 后端最终规则 |
| --- | --- | --- |
| `product_empty_scene` | 产品空境 | 可替换，替换分至少 0.8 |
| `pain_point` | 痛点 | 可替换，替换分至少 0.8 |
| `motion` | 运动 | 可替换，替换分至少 0.8 |
| `intro` | 片头 | 可替换，替换分至少 0.8 |
| `oral_delivery` | 口播 | 不可替换，替换分至多 0.2 |
| `mixed` | 混合 | 需人工复核，替换分至多 0.5 |
| `unknown` | 无法确认 | 需人工复核，替换分至多 0.5 |

上述规则用于成功分析的镜头。失败镜头不按本地启发式补造分析结论。
`viral_analysis.score` 是成功镜头吸睛分的平均值，
`hook_scenes` 是最高分的至多三个镜头，不是播放量预测。

### 8.4 备用切换记录

```json
{
  "fallbacks": [
    {
      "purpose": "scene",
      "scene_index": 2,
      "from_model_id": 31,
      "to_model_id": 14,
      "reason": "HTTPError",
      "succeeded": true
    },
    {
      "purpose": "global",
      "from_model_id": 16,
      "to_model_id": 13,
      "reason": "HTTPError",
      "succeeded": true
    }
  ]
}
```

`reason` 当前记录异常类型，而非完整上游错误正文。
`succeeded=false` 表示尝试了备用模型但也未成功。
没有触发切换时为空数组。成功切换不一定产生 warning。

### 8.5 模型调用记录与报告

`models_used` 使用 `scene`、`global` 以及可选的
`scene_fallback`、`global_fallback` 作为键，每项包含：

- `id`、`provider_id`、`model`、`provider`。
- `model_updated_at`、`provider_updated_at`、`revision`。
- `calls`：模型调用记录，未调用的备用模型为空数组。

记录可能包含 `call_id`、`operation`、`scene_index`、`outcome`、
`seconds`、`response_id`、`usage` 等；不同协议不保证所有字段都存在。
文件上传失败可能尚未产生模型调用记录，此时仍可从 `fallbacks` 查看切换。

`analysis_report` 主要包含：

- `global_model_enriched`：全局模型是否成功补充报告。
- `step_1_global_genes`：内容类型、风格、节奏等整体信息。
- `step_2_core_visual_elements`：人物、产品、环境。
- `step_3_shot_breakdown`：逐镜拆解，包含实际模型 ID 和备用标记。
- `step_4_prompt_configuration`：逐镜生成提示词。
- `scene_captions`、`production_actions`、`timings`。
- `fallbacks`、`global_model_used_id`。

正式报告的台词和 `scene_captions` 使用本地转写；
全局模型输入为逐镜文字证据，不会重新观看整条原视频。

## 9. 主备切换规则

1. 每个镜头先尝试主视频模型。当前镜上传失败、请求失败、超时、
   非 JSON、缺必要字段、字段类型或范围不合格时，尝试备用视频模型。
2. 每个新镜头仍先使用主模型，不因某一镜头失败而全局禁用供应商。
3. 全局文本总结独立切换：请求失败或报告合并校验失败时尝试备用文本模型。
4. Responses 现有有限网络重试结束后再切换；Gemini 原生请求失败直接切换。
5. 指定备用时，无效主模型 JSON 不再额外修复重试；未指定备用时，
   保留原有逐镜无效 JSON 的一次修复重试。
6. 主备均失败时，该镜头标记 `failed`；全局主备均失败则保留逐镜报告。
7. 跨供应商不复用文件 ID。备用 Files 模型重新上传当前和所需上一镜头；
   Gemini 备用直接发送本地完整视频。
8. 切镜、源文件读取、配置解析等前置错误不通过备用模型修复。
9. 返回格式正确但内容判断不准确，不会自动触发备用模型。

## 10. 协议配置与当前限制

调用方无需在分析请求中增加 `protocol` 字段：

| 模型路径 | 服务端调用方式 |
| --- | --- |
| Gemini 视频 | 原生 `generateContent`，发送当前和所需上一镜头的完整 MP4 `inlineData` |
| 火山等原有视频模型 | `/files` 上传，随后 `/responses` 使用 `input_video + file_id` |
| GPT 文本汇总 | `/responses` SSE，输入文字证据 |

Gemini 模型名默认以 `gemini-` 前缀识别。
线上 metadata 的 `scene_protocol` / `global_protocol` 优先于 `protocol`；
值为 `gemini`、`gemini_native` 或 `responses` 可显式指定协议。
这是管理端配置，不是本接口请求字段。本分析适配未新增 Chat Completions 路径。

Gemini 当前只实现完整视频内联上传：

- 单次请求的视频 Base64 总量默认不超过 18,000,000 字节。
- 该限制是当前镜头与上一镜头合计的编码后大小，不是单个原始 MP4 大小。
- 超限会失败并尝试备用；管理员可调整模型 metadata 的 `gemini_max_inline_bytes`。
- 尚未实现 Gemini Files API 大文件上传，未向 Gemini 直接转交源视频 URL。
- `video_fps` 不作用于 Gemini 路径，当前使用供应商默认采样。

第三方供应商是否实际开放某个模型，以真实接口响应为准；
模型目录已添加、协议可调用与上游账号有权限是不同条件。

## 11. 错误与降级

| 业务 `code` | 常见含义 | 调用方处理 |
| --- | --- | --- |
| `0` | 接口成功 | 继续检查任务状态及分析子结果 |
| `1001` | 请求参数校验失败 | 检查来源二选一、模型 ID、备用 ID 和参数范围 |
| `400` | 视频路径、URL 或处理参数不合法 | 修正请求 |
| `404` | 源视频或任务不存在 | 核对路径或任务 ID |
| `409` | 结果未完成或当前状态不支持操作 | 先查询任务状态 |
| `410` | 旧本地模型配置接口已停用 | 使用线上模型目录 |
| `413` | 上传为空或超过大小限制等上传校验错误 | 检查上传文件 |
| `500` | 结果文件或上传存储异常等 | 检查服务端存储与日志 |
| `503` | 模型配置目录、配置校验或分析运行环境不可用 | 检查配置及服务环境 |
| `504` | 视频处理超时 | 检查任务负载和输入 |
| `9998` | 未分类内部异常 | 根据消息及服务日志排查 |

上游模型的 HTTP 404、429、502 等发生在任务执行过程中时，
通常由分析层捕获并触发备用或记录警告，不会原样成为任务查询接口的业务码。
`GET` 查询成功的 `code=0` 只说明读取成功，任务仍可能是 `status=failed`。

## 12. 从旧接口迁移

| 项目 | 调整方式 |
| --- | --- |
| 现有接口 URL | 不变 |
| `video_path` / `video_url` | 不变 |
| 继续使用原有火山模型 | 保留主模型 ID 即可 |
| 改用 Gemini 视频 | 修改 `scene_model_id` |
| 改用 GPT 文本 | 修改 `global_model_id` |
| 开启视频备用 | 增加 `scene_fallback_model_id` |
| 开启文本备用 | 增加 `global_fallback_model_id` |
| 判断实际视频模型 | 读取 `semantics[].model_id`，不能只看顶层主模型字段 |
| 判断实际文本模型 | 读取 `global_model_used_id` |
| 判断是否切换 | 读取 `fallbacks` 和 `semantics[].fallback_used` |
| 更早版本的 `model_config_id` | 改为两个主模型字段，不再支持 |

## 13. 项目内调用工具

异步任务提交、轮询和结果落盘：

```bash
python scripts/test_analysis_task.py \
  --api-url http://localhost:30000 \
  --video-path uploads/example.mp4 \
  --scene-model-id 31 \
  --scene-fallback-model-id 14 \
  --global-model-id 16 \
  --global-fallback-model-id 13 \
  --output reports/analysis-result.json
```

上述为 Bash 换行写法，PowerShell 可写成一行。
不需要备用时省略对应 `--*-fallback-model-id` 参数。

相关文档：

- [模型协议与备用模型说明](video-analysis-model-fallback.zh.md)
- [镜头检测接口](video_analysis.zh.md)
- [镜头切割接口](scene_splitting.zh.md)
- [语音转写说明](video_transcription.zh.md)
