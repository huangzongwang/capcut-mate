# 分析任务裂变替换草稿 API

本接口根据一个已完成的视频分析任务，按其可替换分镜从素材库查找视频素材，创建指定数量的剪映草稿。任务异步执行；提交后使用返回的任务 ID 轮询结果。

## 接口地址

将 `<host>:<port>` 替换为当前服务地址，例如 `192.168.0.191:30000`。

| 用途 | 方法 | 路径 |
| --- | --- | --- |
| 提交裂变任务 | `POST` | `/openapi/capcut-mate/v1/analyze_video_tasks/{analysis_task_id}/replacement_draft_tasks` |
| 查询裂变任务 | `GET` | `/openapi/capcut-mate/v1/replacement_draft_tasks/{task_id}` |
| 删除指定草稿版本 | `DELETE` | `/openapi/capcut-mate/v1/replacement_draft_tasks/{task_id}/versions/{version}` |
| 查询分镜素材候选 | `GET` | `/openapi/capcut-mate/v1/analyze_video_tasks/{analysis_task_id}/scenes/{shot_index}/material_candidates` |
| 将选定素材替换到指定草稿分镜 | `POST` | `/openapi/capcut-mate/v1/replacement_drafts/replace_material` |

完整示例：

```text
POST http://192.168.0.191:30000/openapi/capcut-mate/v1/analyze_video_tasks/va_20261104_023442_8bfad03b/replacement_draft_tasks
GET  http://192.168.0.191:30000/openapi/capcut-mate/v1/replacement_draft_tasks/vrd_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DELETE http://192.168.0.191:30000/openapi/capcut-mate/v1/replacement_draft_tasks/vrd_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/versions/2
GET    http://192.168.0.191:30000/openapi/capcut-mate/v1/analyze_video_tasks/va_xxx/scenes/3/material_candidates?limit=10&min_match_score=0.58
POST   http://192.168.0.191:30000/openapi/capcut-mate/v1/replacement_drafts/replace_material
```

### 将选定素材替换到指定草稿分镜

从上面的候选查询响应中选定一条素材，提交其 `asset_id` 和 `temporal_feature_id`，并指定剪映草稿 ID、目标视频片段 `segment_id`、候选素材来源的分析任务和分镜。接口会校验素材片段有效时间能覆盖目标草稿分镜，再替换该片段的画面素材。**接口在原草稿上原地更新**，不新建草稿；其他分镜、轨道和该片段已有的画面效果设置保持不变。提交前建议先复制草稿作为备份。

```http
POST /openapi/capcut-mate/v1/replacement_drafts/replace_material
Content-Type: application/json
```

```json
{
  "draft_id": "20261117123456abcdef12",
  "segment_id": "segment-id-from-draft-response",
  "analysis_task_id": "va_xxx",
  "shot_index": 3,
  "asset_id": 17,
  "temporal_feature_id": 31,
  "source_start_us": 5000000,
  "volume": 1.0
}
```

`source_start_us` 可省略；省略时采用候选返回的 `clip_start_us`。指定时必须位于素材 `usable_start_us` / `usable_end_us` 范围内，并且剩余长度足以覆盖草稿目标分镜。`volume` 可选；省略时保留原视频片段音量，显式设为 `0` 可静音新素材。该接口依赖草稿仍在创建它的服务进程缓存中；进程重启后不支持通过此接口重新加载已落盘的任意草稿。

成功响应包含原草稿 `draft_id`、`draft_url`、被替换的 `segment_id`、素材 ID 和使用的源起点。素材不在有效分析结果中、时长不足或草稿不在当前进程缓存时返回 `409`；任务或分镜不存在返回 `404`。

### 查询分镜的素材候选

只读查询指定分析任务中某个 `shot_index` 的前 10 条高分素材，按现有语义相似度与视觉意图分排序，并使用分镜实际时长校验素材可用时间范围。低于自动选材阈值的候选仍会返回，`auto_selectable=false` 表示当前分数不满足阈值。此接口不会调用模型审核、创建草稿或更新素材使用次数。

```http
GET /openapi/capcut-mate/v1/analyze_video_tasks/{analysis_task_id}/scenes/{shot_index}/material_candidates?limit=10&min_match_score=0.58
```

`limit` 范围为 `1-10`，默认 `10`；`min_match_score` 范围为 `0-1`，默认 `0.58`。地址仅返回可公开访问的 HTTP(S) URL，不会返回本地路径或 `file://` 地址。若某候选缺少安全可访问的 URL，会从候选列表中排除，并计入 `url_unavailable_count`。

响应示例：

```json
{
  "analysis_task_id": "va_xxx",
  "shot_index": 3,
  "scene_category": "pain_point",
  "scene_content": "人物连续爬楼梯",
  "scene_duration_us": 3000000,
  "min_match_score": 0.58,
  "candidate_count": 1,
  "url_unavailable_count": 0,
  "candidates": [
    {
      "rank": 1,
      "asset_id": 17,
      "temporal_feature_id": 31,
      "asset_name": "爬楼素材.mp4",
      "url": "http://192.168.0.191:30001/media/library/climb.mp4",
      "clip_start_us": 5000000,
      "clip_end_us": 8000000,
      "usable_start_us": 4000000,
      "usable_end_us": 10000000,
      "match_score": 0.82,
      "semantic_similarity": 0.71,
      "visual_intent_score": 0.93,
      "auto_selectable": true
    }
  ]
}
```

任务不存在或分镜不存在返回 `404`；分析仍在执行、任务失败或分镜分析失败返回 `409` 并附带状态/原因；素材库不可用返回 `503`。没有符合时长和素材可用性要求的候选时返回 `200` 和空 `candidates` 列表。

## 前置条件

- 分析任务必须处于 `completed` 或 `completed_with_warnings`。
- 必须在原分析节点提交。裂变使用该节点的本地 SQLite 任务记录和分析报告定位源视频；仅在线上共享索引中可见的其他节点任务不能在当前节点执行。
- 当前节点必须能访问分析报告、源视频、素材库及草稿媒体目录。
- 只有报告中 `replaceable=true`、分析完成且不是口播的分镜会参与替换。其余分镜保留原画面。

## 提交参数

请求体为 `application/json`，所有参数均可省略。

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `count` | integer | `1` | 生成草稿数量，范围 `1-20`。同一批次不同版本/分镜不重复分配同一素材。 |
| `width` | integer | `1080` | 草稿画布宽度，范围 `1-7680`。 |
| `height` | integer | `1920` | 草稿画布高度，范围 `1-7680`。 |
| `min_match_score` | number | `0.58` | 素材匹配最低分，范围 `0-1`。 |
| `keep_original_audio` | boolean | `true` | 独立保留原视频音轨；替换素材静音。设为 `false` 时不保留原视频音频。 |
| `preserve_dialogue_captions` | boolean | `true` | 恢复分析报告中的口播字幕及时间戳。 |
| `preserve_on_screen_text` | boolean | `true` | 兼容字段：允许 OCR 校正语音字幕文字、时间及位置；不会创建贴纸、包装字或水印轨。 |
| `material_review_primary_model_id` | integer/null | `null` | 可选的主模型素材审核 ID。设置后会额外调用模型审核候选素材。 |
| `material_review_backup_model_id` | integer/null | `null` | 可选的备用审核模型 ID。设置备用模型时必须设置主模型，且两个 ID 不能相同。 |

### PowerShell 提交示例

```powershell
$base = "http://192.168.0.191:30000/openapi/capcut-mate/v1"
$analysisTaskId = "va_20261104_023442_8bfad03b"
$body = @{
    count = 2
    width = 1080
    height = 1920
    min_match_score = 0.58
    keep_original_audio = $true
    preserve_dialogue_captions = $true
    preserve_on_screen_text = $true
} | ConvertTo-Json

$response = Invoke-RestMethod -Method Post `
    -Uri "$base/analyze_video_tasks/$analysisTaskId/replacement_draft_tasks" `
    -ContentType "application/json" `
    -Body $body

# 共享响应中间件会把 HTTP 202 封装为业务 JSON code=202；
# 此时任务数据位于 message 中的 detail JSON，而不是响应顶层。
if ($response.task_id) {
    $taskId = $response.task_id
} elseif ($response.code -eq 202 -and $response.message -match 'detail:\s*(\{.*\})$') {
    $taskId = ($Matches[1] | ConvertFrom-Json).task_id
} else {
    throw "提交失败：$($response | ConvertTo-Json -Depth 8 -Compress)"
}

Write-Output "replacement task_id=$taskId"
```

提交只代表任务已入队，不代表草稿已生成。只有找到至少一个替换素材的版本才创建草稿；无替换素材的版本会标记为 `skipped`。如果所有版本均未匹配到素材，任务结束为 `completed_with_warnings`，不创建原片空草稿。典型 HTTP 响应状态在中间件封装后为 `200`，业务 JSON 示例：

```json
{
  "code": 202,
  "message": "HTTP Error 202, detail: {\"task_id\":\"vrd_xxx\",\"source_analysis_task_id\":\"va_xxx\",\"status\":\"queued\",\"stage\":\"queued\",\"progress\":0,\"count\":2,\"completed_count\":0,\"failed_count\":0,\"skipped_count\":0,\"deleted_count\":0,\"versions\":[]}"
}
```

判断是否提交成功时应检查业务 `code=202` 并从 `message` 的 `detail` JSON 中取得 `task_id`。不要因为 HTTP 包装状态为 `200` 就认为任务已经完成。

## 查询任务

### PowerShell 轮询示例

```powershell
$taskId = "vrd_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
$uri = "$base/replacement_draft_tasks/$taskId"

do {
    Start-Sleep -Seconds 5
    $task = Invoke-RestMethod -Uri $uri
    Write-Output "status=$($task.status) stage=$($task.stage) progress=$($task.progress)%"
} while ($task.status -in @("queued", "running"))

$task | ConvertTo-Json -Depth 12
```

任务状态：

| `status` | 含义 |
| --- | --- |
| `queued` | 已提交，等待本节点 Worker 执行。 |
| `running` | 执行中；`stage` 通常为 `matching` 或 `creating_drafts`。 |
| `completed` | 所有版本完成且没有警告。 |
| `completed_with_warnings` | 有版本生成成功但存在未替换分镜、字幕/OCR或贴纸警告；也用于所有版本均因没有可用替换素材而跳过的正常结束状态。 |
| `failed` | 任务失败；查看顶层 `error_message` 及各版本 `error_message`。 |

查询成功响应字段：

| 字段 | 说明 |
| --- | --- |
| `task_id` | 裂变任务 ID。 |
| `source_analysis_task_id` | 源分析任务 ID。 |
| `status` / `stage` / `progress` | 总任务状态、阶段与进度百分比。 |
| `count` | 请求生成的版本数。 |
| `completed_count` / `failed_count` | 已生成草稿及失败的版本数；删除后的版本不计入 `completed_count`。 |
| `skipped_count` / `deleted_count` | 因无替换素材跳过的版本数 / 已删除草稿的版本数。 |
| `created_at` / `started_at` / `completed_at` | 任务时间。 |
| `error_message` | 总任务错误；成功时通常为空。 |
| `versions` | 每个草稿版本的状态、草稿地址、替换素材、未替换原因及逐片段 `detail`。 |

## 完成结果

单个 `versions[]` 项常用字段：

| 字段 | 说明 |
| --- | --- |
| `version` / `status` | 版本序号及版本状态。 |
| `draft_id` | 剪映草稿 ID。 |
| `draft_url` / `preview_url` | 通过 `GET /openapi/capcut-mate/v1/get_draft?draft_id={draft_id}` 获取草稿文件的地址。 |
| `replaced_scene_indexes` | 已替换的原分析分镜编号。 |
| `retained_scene_indexes` | 保留原画面的分镜编号。 |
| `replacement_count` | 本版本实际替换数。 |
| `skip_reason` | 版本状态为 `skipped` 时说明未创建草稿的原因。 |
| `detail` | 前端预览用的时间轴详情，包含每段实际视频地址、时间范围、原素材裁切范围和替换片段字幕。 |
| `materials` | 每个替换分镜选中的素材 ID、片段 ID、裁切范围、输出地址及匹配分。 |
| `unreplaced_reasons` | 未替换分镜及原因；其中可能包括分析报告本身标记为口播/不可替换的分镜。 |
| `media_mode` / `media_note` | 画面与原音轨的合成方式说明。 |
| `preserved_dialogue_caption_count` | 恢复的口播字幕数。 |
| `ocr_adjusted_caption_count` 等 | OCR 校正文字、时间、位置的数量。 |
| `overlay_warnings` | OCR 不可用、未恢复非文字贴纸等提示。 |
| `elapsed_seconds` | 本版本生成耗时。 |

版本状态还可能为 `skipped`（未找到替换素材，不创建草稿）或 `deleted`（草稿已由删除接口移除）。跳过版本不会分配 `draft_id`，也不会创建只有原视频的草稿。

### 删除指定草稿

通过裂变任务 ID 和版本序号删除对应草稿。只能在创建该裂变任务的节点操作，并且任务必须已结束。接口会删除剪映草稿目录及该版本独有的替换视频文件，不删除其他版本共用的源视频片段，也不会删除素材库原文件。

```powershell
$taskId = "vrd_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
$version = 2
$deleted = Invoke-RestMethod -Method Delete `
    -Uri "$base/replacement_draft_tasks/$taskId/versions/$version"
$deleted | ConvertTo-Json -Depth 12
```

成功时返回更新后的完整裂变任务；指定版本变为 `status=deleted`，其 `draft_url`、`preview_url`、`detail` 会清空，`materials` 等媒体详情列表也会清空。任务不存在或版本不存在返回 `404`；任务仍在执行、版本未成功生成或请求落在非创建节点时返回 `409`。如果草稿删除成功但版本媒体清理失败，会在该版本返回 `cleanup_warning`。

`detail` 示例（字段位于单个 `versions[]` 项内）：

```json
{
  "schema_version": 1,
  "duration_us": 8000000,
  "canvas": {"width": 1080, "height": 1920},
  "segments": [
    {
      "segment_id": "scene:2",
      "scene_index": 2,
      "type": "replacement",
      "start_us": 3000000,
      "end_us": 5000000,
      "duration_us": 2000000,
      "video_url": "http://192.168.0.191:30001/media/replacement-drafts/vrd_xxx/version-01/scene-002-asset-17.mp4",
      "width": 1080,
      "height": 1920,
      "source_start_us": 500000,
      "source_end_us": 2500000,
      "scene_category": "pain_point",
      "content": "人物连续爬楼梯",
      "captions": [
        {
          "start_us": 3400000,
          "end_us": 4500000,
          "text": "人物连续爬楼梯",
          "included_in_draft": true
        }
      ],
      "replacement": {
        "asset_id": 17,
        "temporal_feature_id": 31,
        "source_start_us": 500000,
        "source_end_us": 2500000,
        "match_score": 0.82,
        "vertical_fit": "contain_blur"
      }
    }
  ]
}
```

`segments[]` 按草稿时间轴排列；`type` 为 `original`、`replacement` 或源视频时间轴空隙的 `gap`。`video_url` 是该时间段实际写入草稿的视频文件；字幕只列替换片段恢复的口播字幕，时间戳是整条草稿的微秒时间。`included_in_draft=false` 表示字幕写入失败，但详情仍保留该字幕以便前端提示。媒体 URL 使用 `SCRIPT_VIDEO_MEDIA_URL_BASE`；Windows 下不会回退到 `127.0.0.1`，`start.bat` 根据 `--public-host` 设置此值。

每个 `materials[]` 项的 `match_reason` 包含 `semantic_similarity`、`visual_intent_score` 与 `replacement_quality`。当前裂变匹配分为：

```text
match_score = (semantic_similarity + visual_intent_score) / 2
```

该分数与 `min_match_score` 比较。必需元素覆盖率、商品身份、素材分类角色和运动强度不再作为裂变候选的拒绝条件或排序加分；仍要求素材可访问、片段时长足够并且不是口播素材。动作事件可用于精确确定裁切起点，但不计入匹配分。设置模型审核 ID 后，审核仍会作为额外的候选过滤步骤。

## 常见响应与排查

- 提交返回业务 `code=202`：正常，已入队；从 `message` 内的 JSON 解析 `task_id`，然后查询任务。
- 查询返回 `status=completed_with_warnings`：检查 `completed_count`；若为 `0` 且 `skipped_count` 大于 `0`，表示没有生成草稿，因为所有版本都没有匹配到替换素材。否则结合 `unreplaced_reasons`、`overlay_warnings` 查看警告。
- `409`：分析任务不存在/未完成、不在当前执行节点，或审核模型参数不合法。共享响应中间件可能将其包装为 HTTP 200、业务 JSON `code=409`。
- `503`：素材库配置或访问失败等服务端依赖不可用。共享响应中间件可能将其包装为 HTTP 200、业务 JSON `code=503`。
- 版本 `failed`：查看该版本的 `error_message`；其他已完成版本仍可使用。
- 生成成功但 `replacement_count=0`：任务仍可能成功完成，所有可替换分镜因没有达到匹配阈值、时长不足或没有可用素材而保留原画面。

## 生成行为

- 直接使用分析报告的分镜边界，不重新检测或拆分分镜。
- 只为至少替换一个分镜的版本创建草稿；没有匹配素材的版本标记为 `skipped`，全部版本都无素材时不创建草稿。
- 主画面轨按原分镜时间轴拼接源视频和替换素材；替换素材静音。默认将原视频音轨独立保留。
- 默认尝试恢复分析报告里的口播字幕。OCR 仅可校正语音字幕，不会新增包装字、水印或贴纸文字轨；压平视频里的非文字贴纸不会被抠取恢复。
- 成功创建草稿后才记录素材使用流水与成功使用次数。
- 裂变任务记录保存在节点本地 SQLite，并通过 video_analysis 共享结果索引同步查询状态；源任务和媒体处理仍由原分析节点负责。

实现仅位于 `src/service/video_analysis/replacement_drafts/`，通过 video-analysis 专属路由注册；不修改框架公共路由和其他视频业务。
