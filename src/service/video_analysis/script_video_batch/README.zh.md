# 脚本口播批量草稿 API

本文描述 `video_analysis` 下的脚本口播批量制作接口，适用于把一份**已经独立生成的脚本分镜**和口播母版组合成 1–20 条剪映草稿。分镜人工审核是可选的：待审核、部分确认和全部确认的分镜均可提交；批量接口不生成、不修改分镜，只负责读取指定分镜、对齐口播字幕、匹配 B-roll、保留完整口播音轨、创建草稿并记录素材使用情况。

## 1. 地址与通用约定

服务前缀：

```text
/openapi/capcut-mate/v1
```

本模块接口：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `POST` | `/script-video-batch-tasks` | 提交批量草稿任务，立即返回任务 ID |
| `GET` | `/script-video-batch-tasks/{task_id}` | 查询批次和每条草稿状态 |
| `GET` | `/script-video-batch-tasks/{task_id}/captions.srt` | 下载分镜字幕 SRT |

完整地址示例：`http://127.0.0.1:30000/openapi/capcut-mate/v1/script-video-batch-tasks`。

请求和响应均为 UTF-8 JSON。提交接口返回 HTTP `202 Accepted`，表示任务已受理，不代表草稿已经生成。主应用响应可能附加顶层 `code=0` 和 `message`；不要假设响应有 `data` 包装层。校验错误在主应用中可能被统一响应中间件包装，应同时查看 HTTP 状态和 JSON 业务 `code/message`。

## 2. 调用前准备

### 2.1 先生成脚本分镜（审核可选）

批量制作前必须先调用已有的独立脚本分镜接口。同步流程如下：

```text
POST /openapi/capcut-mate/v1/script_storyboards
GET  /openapi/capcut-mate/v1/script_storyboards/{storyboard_id}
PATCH /openapi/capcut-mate/v1/script_storyboards/{storyboard_id}
POST /openapi/capcut-mate/v1/script_storyboards/{storyboard_id}/confirm
```

也可先调用异步接口 `POST /script_storyboard_tasks`，任务成功后取得 `storyboard_id`。可先通过读取、编辑和确认接口人工审核，也可以直接使用生成结果提交批量视频任务；审核状态不会阻止生成。分镜至少需要包含一个镜头。

### 2.2 现有口播视频上传

先调用已有上传接口，上传视频使用 `purpose=video-analysis`：

```powershell
curl.exe -X POST "http://127.0.0.1:30000/openapi/capcut-mate/v1/upload_video" `
  -F "purpose=video-analysis" `
  -F "file=@C:\\media\\talking-head.mp4"
```

上传成功响应中的 `video_path` 是后续请求的 `source_video_path`。如果音频单独上传，仍可使用此接口上传 WAV 等音频，响应路径可填入 `source_audio_path`。

上传响应示例：

```json
{
  "video_path": "uploads/3f9840c6b97d4fe2a5541bdc32670236.mp4",
  "purpose": "video-analysis",
  "filename": "3f9840c6b97d4fe2a5541bdc32670236.mp4",
  "size_bytes": 18432000
}
```

上传音频时响应字段仍叫 `video_path`，将该值作为 `source_audio_path` 使用。

数字人素材可以通过 `purpose=digital-human` 上传。`avatar_video_path` 应指向提前准备好的数字人无声底片，不是任意人物视频；杜绝把一个带原声的口播视频当成 Duix 驱动底片。

### 2.3 依赖和可访问性

- 本机安装并可调用 `ffmpeg`、`ffprobe`。
- Whisper `large-v3` 模型文件可用；默认用 `SCRIPT_VIDEO_WHISPER_MODEL=large-v3`。
- 服务端可访问素材库 MySQL，并有目录表及使用统计表的相应权限。
- 草稿生成所用的 CapCut/MATE 服务进程能访问本机口播媒体和素材媒体 URL。
- 数字人模式还需要音频凭证或现成 WAV、Duix 服务以及 Duix 共享目录。

## 3. 提交任务

### 3.1 HTTP 请求

```http
POST /openapi/capcut-mate/v1/script-video-batch-tasks
Content-Type: application/json
```

### 3.2 请求字段

除注明可选外，均可省略并使用默认值。未知字段会被拒绝。

| 字段 | 类型 | 必需 | 默认/范围 | 说明 |
| --- | --- | --- | --- | --- |
| `storyboard_id` | string | 是 | `sb_` 加 32 位十六进制字符 | 已独立生成的脚本分镜 ID；人工审核可选。脚本、商品名称和商品资料均读取该分镜保存的数据。 |
| `source_mode` | string | 否 | `existing_video` | `existing_video` 使用现有口播视频；`digital_human` 调用 Duix 生成数字人口播母版。 |
| `source_video_path` | string | 现有视频模式必需 | 相对媒体目录 | `/upload_video` 返回的相对路径；不得传本机绝对路径或 `..`。 |
| `source_audio_path` | string | 否 | 无 | 现有音频文件路径。现有视频模式可用它替代视频内音轨；数字人模式可用它跳过音频生成。 |
| `voice_element_id` | string | 数字人模式二选一 | 无 | 用项目音频生成接口配置的凭证和音色生成口播音频。与 `source_audio_path` 二选一。 |
| `voice_speed` | number | 否 | `1.1`，范围 `0.5–2` | 音频生成语速。仅生成音频时生效。 |
| `pronunciation_overrides` | array | 否 | `[]`，最多 50 项 | 多音字/专有名词读音覆盖，每项为 `{"text":"赛乐赛","phonetic":"sai4 le4 sai4"}`。 |
| `avatar_video_path` | string | 数字人模式必需 | 相对媒体目录 | 已准备好的 Duix 数字人底片。 |
| `duix_api_base_url` | string | 数字人模式必需 | 无 | Duix API 地址。 |
| `duix_shared_dir` | string | 数字人模式必需 | 无 | Duix Worker 可访问的共享目录；Windows 使用 UNC 路径。 |
| `count` | integer | 否 | `20`，范围 `1–20` | 目标草稿数量。 |
| `width` / `height` | integer | 否 | `1080` / `1920`，范围 `1–7680` | 剪映草稿画布尺寸。 |
| `split_screen_enabled` | boolean | 否 | `false` | 开启后，被素材命中的分镜使用上方口播、下方匹配素材；未命中的口播片段仍保持整屏。可与随机画中画同时开启。 |
| `split_fit_mode` | string | 否 | `contain_blur` | 上下分屏画面适配：`contain_blur` 完整显示并用模糊背景补边；`contain_black` 完整显示并用黑色补边；`cover` 填满面板但可能裁边。 |
| `split_ratio` | number | 否 | `0.5`，范围 `0.25–0.75` | 上方面板占可用分屏高度的比例。 |
| `split_gap_px` | integer | 否 | `0`，范围 `0–512` | 上下两个面板之间的像素间隔；默认无缝衔接，传入正数会显示草稿背景间隔。 |
| `pip_enabled` | boolean | 否 | `false` | 随机把部分素材替换镜头改为圆形或镜面组合布局，可与上下分屏同时开启。 |
| `pip_probability` | number | 否 | `0.35`，范围 `0.05–1` | 每个已匹配素材镜头使用组合布局的概率；重试同一任务时选择保持稳定。 |
| `pip_scale` | number | 否 | `0.28`，范围 `0.1–0.5` | 右上角圆形口播相对画布的缩放比例。 |
| `pip_mask_mode` | string | 否 | `circle` | `circle` 使用右上角圆形口播；`mirror` 使用上方完整口播、下方完整素材的 50/50 镜面布局；`auto` 在两种模式间稳定随机切换。 |
| `max_drafts_in_parallel` | integer | 否 | `2`，范围 `1–4` | 本批次内部同时创建的草稿数量，不代表整个服务的批次并发数。 |
| `keep_master_audio` | boolean | 否 | `true` | 当前必须为 `true`，保留整条口播音轨；传 `false` 会导致任务失败。 |
| `background_music_enabled` | boolean | 否 | `true` | 从线上 `myh_music_archive` 按脚本及分镜语义匹配时长足够的无人声音乐，并作为独立背景音轨加入草稿；音乐长于成片时自动从开头裁到与成片一致，不变速、不延长时间轴；查询或下载失败时草稿仍继续生成。 |
| `background_music_volume` | number | 否 | `0.0`，范围 `0–2` | 背景音乐音量；默认保留音乐轨但静音。完全不需要音乐轨时传 `background_music_enabled=false`。 |
| `subtitle_font` | string | 否 | `点宋体` | 剪映草稿字幕字体名称，必须能被框架字体库解析。 |
| `subtitle_font_size` | integer | 否 | `12`，范围 `1–200` | 剪映草稿字幕字号。 |
| `subtitle_text_color` | string | 否 | `#ffde00` | 字幕颜色，六位十六进制 RGB。 |
| `subtitle_bold` | boolean | 否 | `false` | 字幕是否加粗。 |
| `subtitle_underline` | boolean | 否 | `false` | 字幕是否加下划线。 |
| `subtitle_italic` | boolean | 否 | `false` | 字幕是否使用斜体。 |
| `subtitle_transform_y` | number/null | 否 | `null` | 字幕垂直位移像素；剪映坐标中负数向下。`null` 默认使用 `-height/2`，即放在画面约 75% 高度处；1080×1920 时为 `-960px`。 |
| `subtitle_remove_punctuation` | boolean | 否 | `true` | 显示字幕时移除 Unicode 标点；平台断句和时间映射仍使用完整原文。 |
| `subtitle_in_animation` | string/null | 否 | `打字机 II` | 每条字幕的入场动画名称；传 `null` 关闭入场动画。默认使用框架动画的默认时长。 |
| `pause_mode` | string | 否 | `hybrid` | `hybrid` 按 Whisper 词级时间戳同步压缩视频和音频中的较长停顿；`off` 保持原始时间轴。 |
| `pause_detect_ms` | integer | 否 | `300` | 超过该时长的相邻词间隔才会被视为可压缩停顿。 |
| `pause_keep_ms` | integer | 否 | `220` | 300–700ms 停顿压缩后的目标保留时长，不能大于 `pause_detect_ms`。 |
| `long_pause_ms` | integer | 否 | `1000` | 长停顿阈值；默认将更长停顿保留约 350ms。 |
| `cover_long_pauses` | boolean | 否 | `true` | `true` 同样压缩长停顿；`false` 保留达到 `long_pause_ms` 的停顿原长。 |
| `material_review_primary_model_id` | integer/null | 否 | `null` | 线上 `myh_video_models.id`。传入后启用数据库粗筛后的候选画面审核；支持 Responses 视频、Gemini generateContent 视频或多图片视觉输入。 |
| `material_review_backup_model_id` | integer/null | 否 | `null` | 主模型调用失败、能力不兼容或没有确认任何候选时使用。必须同时传主模型，且不能与主模型相同。 |

媒体路径须为上传目录内的安全相对路径。可传 `uploads/文件名.mp4` 或不带 `uploads/` 前缀的等效相对路径；不支持任意文件系统路径。

### 3.3 停顿优化规则

`hybrid` 模式不会再跑一次 Whisper。它使用已有词级时间戳找出相邻词之间的静音，从停顿中间删除多余区间，并使用同一组区间重建母版视频和母版音频。说话词的范围不被裁剪，之后直接重映射原转写、分镜和字幕时间，保持口型与字幕同步。

默认规则：

- 小于 300ms：保留原样。
- 300–700ms：压缩到约 220ms。
- 700ms 到长停顿阈值：压缩到约 280ms。
- 达到长停顿阈值：`cover_long_pauses=true` 时压缩到约 350ms。

任务成功后，每个 `versions[]` 会附带 `pause_optimization.pause_count` 和 `pause_optimization.removed_us`。需要完全保持旧画面节奏时传 `"pause_mode":"off"`。

### 3.4 现有口播视频示例

```powershell
curl.exe -X POST "http://127.0.0.1:30000/openapi/capcut-mate/v1/script-video-batch-tasks" `
  -H "Content-Type: application/json" `
  -d '{
    "storyboard_id": "sb_0123456789abcdef0123456789abcdef",
    "source_mode": "existing_video",
    "source_video_path": "uploads/talking-head.mp4",
    "count": 20,
    "material_review_primary_model_id": 14,
    "material_review_backup_model_id": 18,
    "max_drafts_in_parallel": 2,
    "background_music_volume": 0.0,
    "split_screen_enabled": true,
    "split_fit_mode": "contain_blur",
    "split_ratio": 0.5,
    "split_gap_px": 0,
    "pip_enabled": true,
    "pip_probability": 0.35,
    "pip_scale": 0.28,
    "pip_mask_mode": "auto"
  }'
```

开启上下分屏时，服务会先把口播画面和命中素材分别预处理为独立面板，再写入不同视频轨道。`contain_blur` 的前景始终等比例完整显示，模糊副本只负责填充空白区域，因此人物、商品和边缘文字不会因面板比例不同被裁掉。若同时开启画中画，`circle` 使用全屏素材并把原口播放到右上角圆形蒙版；`mirror` 固定使用无间隔的 50/50 面板，上方完整显示口播、下方完整显示素材，并给口播面板应用镜面蒙版；`auto` 在两者间稳定随机切换。其余素材镜头继续使用上下分屏，口播音频始终只保留一条独立音轨。

竖屏草稿使用横屏素材时，审核模型可以返回方向纠正角度 `rotation_degrees`（0/90/180/270）；只有画面本身侧倒或倒置时才旋转，正常横屏不得为填满竖屏而旋转。方向纠正后适合竖屏的素材可整屏使用；不能旋转成竖屏且不能安全裁切的横屏素材，在 `split_screen_enabled=true` 时使用上下分屏，否则放弃该候选。方向纠正角度会应用到整屏和上下屏素材渲染，并参与渲染缓存键。

若提供 `source_audio_path`，该文件作为母版音轨及转写来源，不使用视频内音轨；音频长度必须覆盖视频。未提供时从视频中提取音轨。若原视频开头有固定静音/片头偏移，可设置环境变量 `SCRIPT_VIDEO_AUDIO_OFFSET_US`（微秒，默认 `0`）；它只用于从原视频音轨对齐脚本，不适用于单独提供的音轨。

### 3.5 数字人口播示例

先设置音频接口凭证，或上传现成的 WAV。生成的文字必须与该 `storyboard_id` 保存的脚本转写匹配，否则不会继续生成草稿。

```json
{
  "storyboard_id": "sb_0123456789abcdef0123456789abcdef",
  "source_mode": "digital_human",
  "avatar_video_path": "uploads/avatar-base.mp4",
  "voice_element_id": "voice-id",
  "voice_speed": 1.1,
  "duix_api_base_url": "http://192.168.0.26:8000",
  "duix_shared_dir": "\\\\192.168.0.26\\duix_avatar_data\\face2face\\temp",
  "count": 20
}
```

若传 `source_audio_path`，它优先作为驱动音频并跳过项目音频生成；即使同时传了 `voice_element_id`，也不会生成新音频。无现成音频时才传 `voice_element_id`。两种情况都会调用 Duix。数字人任务默认最多等待 1800 秒，可用 `SCRIPT_VIDEO_DUIX_TIMEOUT_SECONDS` 调整。超时不会自动重复提交 Duix 请求。

### 3.6 提交响应

HTTP `202`，示例：

```json
{
  "task_id": "svb_9a6a6cc75c374f02a287aa52e1a44b53",
  "status": "queued",
  "stage": "queued",
  "progress": 0,
  "count": 20,
  "completed_count": 0,
  "failed_count": 0,
  "storyboard_id": "sb_0123456789abcdef0123456789abcdef",
  "error_message": "",
  "created_at": "2026-10-26T08:00:00+00:00",
  "completed_at": null,
  "versions": []
}
```

## 4. 查询任务

```http
GET /openapi/capcut-mate/v1/script-video-batch-tasks/{task_id}
```

轮询建议间隔 5–10 秒。`progress` 是阶段估算百分比，不是模型/FFmpeg 精确进度。

### 4.1 状态字段

| 字段 | 说明 |
| --- | --- |
| `status` | `queued`、`running`、`completed`、`partial_success` 或 `failed`。 |
| `stage` | 当前节点：`queued`、`loading_storyboard`、`preparing`、`transcribing`、`generating_audio`、`generating_avatar`、`optimizing_pauses`、`splitting_captions`、`matching`、`verifying_materials`、`creating_drafts`，或终态 `completed`、`partial_success`、`failed`。 |
| `progress` | 0–100 的估算进度。 |
| `count` | 计划生成数量。 |
| `completed_count` / `failed_count` | 成功和失败的版本数量。 |
| `storyboard_id` | 本任务读取的既有脚本分镜 ID，任务提交后即返回。 |
| `material_review_primary_model_id` / `material_review_backup_model_id` | 本任务绑定的主、备用素材审核模型 ID。未传主模型时保持旧的仅数据库匹配行为。 |
| `versions` | 每条草稿独立状态和结果；部分成功时保留成功项。 |
| `error_message` | 整批失败或部分失败摘要。 |

任务状态终态是 `completed`、`partial_success`、`failed`。每个版本的 `status` 为 `queued`、`running`、`completed` 或 `failed`。

可通过以下接口读取任务使用的脚本分镜及镜头分类。批量任务不会为它新建副本，也不会修改它。分镜文档里的 `source_start/source_end` 是脚本文字索引，不是视频时间戳：

```http
GET /openapi/capcut-mate/v1/script_storyboards/{storyboard_id}
```

### 4.2 完成响应示例

```json
{
  "task_id": "svb_9a6a6cc75c374f02a287aa52e1a44b53",
  "status": "partial_success",
  "stage": "partial_success",
  "progress": 100,
  "count": 2,
  "completed_count": 1,
  "failed_count": 1,
  "storyboard_id": "sb_123456",
  "material_review_primary_model_id": 14,
  "material_review_backup_model_id": 18,
  "error_message": "1 条草稿创建失败",
  "created_at": "2026-10-26T08:00:00+00:00",
  "completed_at": "2026-10-26T08:07:00+00:00",
  "versions": [
    {
      "version": 1,
      "status": "completed",
      "draft_id": "20261026080000abcdef12",
      "draft_url": "http://127.0.0.1:30000/openapi/capcut-mate/v1/get_draft?draft_id=20261026080000abcdef12",
      "preview_url": "http://127.0.0.1:30000/openapi/capcut-mate/v1/get_draft?draft_id=20261026080000abcdef12",
      "captions_url": "/openapi/capcut-mate/v1/script-video-batch-tasks/svb_9a6a6cc75c374f02a287aa52e1a44b53/captions.srt",
      "background_music": {
        "id": 7,
        "title": "明亮科技感电子纯音乐",
        "url": "https://media.example/music.mp3",
        "duration_us": 123758000,
        "volume": 1.0,
        "match_score": 1.0,
        "match_reasons": ["专业科技感", "克制口播背景"]
      },
      "shot_count": 3,
      "segment_ids": ["..."],
      "replaced_count": 1,
      "retained_count": 2,
      "materials": [
        {
          "shot_id": "shot_123",
          "category": "product_empty_scene",
          "visual_intent": "otc_certification",
          "unit_id": "shot_123",
          "target_text": "有红标OTC标识",
          "asset_id": 42,
          "temporal_feature_id": 81,
          "source_start_us": 1200000,
          "source_end_us": 4200000,
           "vertical_fit": "smart_crop",
           "focus_x": 0.68,
           "focus_y": 0.45,
           "model_verification": {
             "matched": true,
             "confidence": 0.95,
             "best_start_ms": 1200,
             "best_end_ms": 4200,
             "evidence": ["手指明确指向红色OTC标识"],
             "model_id": 14,
             "adapter": "native_video",
             "cache_hit": false
           },
          "match_score": 0.85,
          "match_reason": {
            "role": "product_detail",
            "semantic_similarity": 0.61,
            "visual_intents": {"intent:otc_certification": true},
            "analysis_id": 19
          }
        }
      ],
      "detail": {
        "schema_version": 1,
        "duration_us": 32000000,
        "width": 1080,
        "height": 1920,
        "captions_url": "/openapi/capcut-mate/v1/script-video-batch-tasks/svb_9a6a6cc75c374f02a287aa52e1a44b53/captions.srt",
        "shots": [
          {
            "shot_id": "shot_123",
            "category": "product_empty_scene",
            "visual_intent": "otc_certification",
            "start_us": 0,
            "end_us": 4200000,
            "script_text": "有红标OTC标识",
            "replacement_status": "replaced",
            "layout": "pip_circle",
            "visuals": [
              {
                "role": "main",
                "type": "material_video",
                "video_url": "http://127.0.0.1:30001/media/script-video-batch/svb_x/clips/clip-x.mp4",
                "source_start_us": 1200000,
                "source_end_us": 5400000,
                "width": 1080,
                "height": 1920,
                "fit_mode": "smart_crop",
                "focus_x": 0.68,
                "focus_y": 0.45,
                "asset_id": 42,
                "temporal_feature_id": 81
              },
              {
                "role": "pip",
                "type": "master_video",
                "video_url": "http://127.0.0.1:30001/media/script-video-batch/svb_x/master-source.mp4",
                "source_start_us": 0,
                "source_end_us": 4200000,
                "scale": 0.28,
                "transform_x": 692,
                "transform_y": 1228,
                "position": "top_right",
                "mask": "circle"
              }
            ],
            "captions": [
              {
                "start_us": 500000,
                "end_us": 1800000,
                "text": "有红标OTC标识",
                "animation": "打字机 II",
                "style": {
                  "font": "点宋体",
                  "font_size": 12,
                  "color": "#ffde00",
                  "alignment": 1,
                  "transform_x": 0,
                  "transform_y": 480,
                  "bold": false,
                  "underline": false,
                  "italic": false,
                  "has_shadow": true
                }
              }
            ],
            "materials": [
              {
                "unit_id": "shot_123",
                "asset_id": 42,
                "temporal_feature_id": 81,
                "source_start_us": 1200000,
                "source_end_us": 5400000,
                "match_score": 0.85
              }
            ],
            "stickers": []
          }
        ],
        "timeline_gaps": [],
        "sticker_status": "not_generated",
        "sticker_note": "当前脚本视频流程没有独立贴纸图层生成或提取；压平在源视频中的贴纸不作为独立素材返回。"
      }
    },
    {"version": 2, "status": "failed", "error_message": "草稿媒体无法下载"}
  ]
}
```

草稿 URL 用于剪映草稿管理或预览，不是渲染后 MP4 成片地址。每一版都是单独草稿。

`versions[].detail` 是同一查询接口返回的逐镜预览结构，不改变原 `materials`、`layout`、`captions_url` 等兼容字段。`shots[]` 每项按脚本分镜返回时间范围、脚本文本、实际使用的 `visuals[]`、该镜头内的字幕及样式和素材来源；`visuals[].video_url` 指向草稿实际使用的可访问媒体文件。`layout` 可为 `retained`、`replace`、`split_screen`、`pip_circle` 或 `mirror`。画中画/分屏图层提供 `role`、`mask`、`position`、缩放、画布位置和素材源区间，前端可据此绘制逐镜预览。字幕时间仍为整条时间线的微秒绝对值。`stickers` 当前返回空数组，`sticker_status=not_generated` 表示本流程没有独立生成/提取贴纸；若原视频贴纸已压进视频画面，不会伪装成独立图层。

## 5. 下载字幕

```powershell
curl.exe -L "http://127.0.0.1:30000/openapi/capcut-mate/v1/script-video-batch-tasks/svb_9a6a6cc75c374f02a287aa52e1a44b53/captions.srt" `
  -o captions.srt
```

字幕文本边界直接使用音频平台 `split_sentences` 的断句结果，依赖音频模块已经配置且验证通过的 Cookie/BPID；时间边界继续使用 Whisper 词级时间戳映射，不采用音频平台估算时间。视频替换范围会覆盖相邻台词之间的静音间隙，但字幕单独保留真实首词开始时间和末词结束时间，不会因为分镜补齐而提前出现。平台断句只请求一次，SRT 和本批次全部草稿共用同一份结果。若凭证未配置、Cookie 失效、断句请求失败，或断句文本与脚本不一致，批量任务会明确失败，不会静默退回本地按字数切分。平台断句跨越语义分镜边界时仍会在分镜边界截断，以保持视频替换、字幕和口播时间轴一致。SRT 精度为毫秒；内部时间轴使用微秒。

## 6. 分镜与素材替换规则

- 批量任务只读取指定的既有分镜，不调用脚本分镜模型。分镜不存在或没有镜头时任务失败；待审核、部分确认和全部确认状态均可继续生成。
- Whisper 转写须与脚本相似度至少 `0.82`，并提供可用词级时间戳；无法可靠对齐时整批停止。
- 每个可替换分镜还会校验局部转写相似度，默认至少 `0.82`。相邻分镜的画面边界取两句词级时间戳间隙的中点，内部以微秒记录，避免停顿处闪回母版画面。
- 语义分镜和字幕仍逐条保留；生成草稿视频轨时，仅将相邻且均为 `oral_delivery`、均保留原画面的分镜合并为一个连续母版片段。其他类型无论是否相同都保留独立切点，避免产品空境等不同语义需求因合并而影响素材匹配。
- `talking_head`/`oral_delivery` 保留人物口播；其他镜头先按六类兼容大类过滤，再用 `visual_intent` 精确匹配痛点、产品整体、包装细节、说明书、OTC、溯源、真假、用法、服药时刻、工厂、客服、身材、运动、减肥机理、生活、体验或CTA素材。`medication_moment` 必须有拿药、送入口中、喝水或吞服等真实动作证据，普通药盒或胶囊摆拍不会代替服药动作。
- 素材自带字幕、贴字或水印不会被过滤；这些分析标记只作为结果元数据保留，不参与候选淘汰。候选仍须满足分镜分类、视觉意图、可用时间区间和目标时长。
- OTC、二维码/条形码/条码溯源、真假辨别、用法用量和服药时刻属于严格证据类型；素材分析文本或用途角色没有对应证据时不会被选中，避免普通产品摆拍误替换。扫码、溯源、二维码、条形码或条码统一要求画面中至少一种编码可见，不要求实际扫码动作。`otc_certification` 还必须由审核模型同时确认 OTC 文字清晰，以及手指/指示棒指向、放大镜放大或镜头局部放大三类强调动作之一；包装自然带有 OTC、普通产品摆拍和普通包装特写均不通过。`medication_moment` 必须由素材分析或审核模型确认人物将药送入口中、喝水服药或吞服等真实动作，药盒、胶囊、说明书和用量文字展示均不通过。`product_overview`、`product_detail` 等普通产品空镜不套用这些动作规则。
- 商品空境必须满足商品身份匹配，命中 `forbidden_elements` 的候选直接淘汰。商品空境和机理类的 `required_elements` 至少命中一项；其他类别须全部命中。所有类别仍需通过语义、视觉意图及禁用元素校验。
- 商品分镜会把台词意图映射为可接受的视觉证据：OTC 优先手指指向、放大或特写标识；扫码、溯源、二维码、条形码或条码统一要求画面中至少一种编码可见；首次介绍商品优先药盒摆放、手持展示或突然出现。OTC、溯源、真假和用法会先做专项证据过滤，其余意图参与排序和达标评分；所有类型仍须通过商品一致、时长、禁用元素及语音边界校验。
- 相关性评分使用素材库已分析的逐镜画面摘要、物体、动作、场景、品牌及商品信息。商品身份、用途类型和视觉内容共同达到阈值才会替换，避免仅按固定关键词检索，也不会退化为同类商品随机替换。
- 素材可用时长小于目标分镜时长时直接淘汰，不减速、不循环、不补帧凑时长。共享素材检索会读取可选的 `myh_media_action_events`，按分析 ID、素材 ID 和时间重叠为候选补充动作、对象、置信度及证据帧；事件置信度至少 0.5 且从事件起点能完整覆盖目标时长时，未启用模型审核的流程从事件起点裁切，并在 `match_reason.action_event` 返回证据。无匹配事件时仍使用时间片摘要和原有 `usable_start_us` 起点。启用模型审核后仍由模型定位候选内的最佳画面。
- 草稿生成严格沿用保存的脚本分镜边界，不再按时长、标点或停顿二次拆分。一个分镜只检索并使用一段能够完整覆盖该分镜时长的素材；没有足够长且匹配的素材时保留原口播画面。
- 同一草稿的长分镜内部不会重复使用相同素材片段；产品、身材、运动、痛点和机理仍分别沿用原分镜类别与局部台词检索，不会跨类别随机拼接。若某个内部片段没有合格素材，仅该区间保留原口播画面。
- `versions[].materials[]` 会返回 `category`、`visual_intent`、`unit_id`、`sub_index=0`、`sub_count=1`、`target_text` 和完整分镜微秒区间；兼容字段继续保留，但不会产生内部子片段。`replaced_clip_count` 与实际替换分镜数量一致。
- 素材不足时保留母版口播画面，不会为了凑数放宽商品身份约束。
- 每一版本优先使用未在该分镜其他版本用过的候选；池中不够时才复用。每个版本内同一素材片段不会重复分配给多个分镜。
- B-roll 在 FFmpeg 裁剪阶段移除音轨并转换为草稿尺寸。模型判断主体可安全裁剪时使用 `focus_x/focus_y` 智能裁剪；横向多人、完整说明书或裁剪会丢失证据时使用等比完整显示和模糊背景补边。主口播音轨覆盖全片，字幕按分镜添加。

未传 `material_review_primary_model_id` 时，仅使用素材入库分析字段，不调用审核模型。传入主模型后，候选按每批最多 3 个成组审核，并根据草稿数量继续审核后续候选，直到得到足够的不同素材或候选耗尽。火山视频模型使用多个 `input_video`；Gemini 原生协议使用 `generateContent` 的 MP4 `inlineData`；GPT兼容视觉模型使用带候选编号和相对时间戳的多张 `input_image`。主模型失败或没有确认候选时才调用备用模型；两者都失败时保留原口播。

Gemini 适配优先读取模型 `metadata.protocol`（支持 `gemini`、`gemini_native`、`generate_content`），也兼容 provider 名称中包含 `gemini` 的现有模型记录。接口仍只传模型 ID，不传模型名称、Base URL 或密钥。需要强制指定输入方式时，可在模型 metadata 设置 `material_review_input_mode=gemini_video`；可选设置 `material_review_thinking_budget` 和 `material_review_gemini_max_inline_bytes`。

审核结论按目标语义、候选素材哈希、模型配置版本和提示词版本缓存在 `myh_media_match_verifications`。同一批的20条草稿共享审核结果；后续相同匹配也可直接命中缓存。正向结果默认缓存30天，拒绝结果默认缓存7天。

## 7. 数据库与素材访问配置

### 7.1 表的创建和权限

素材读取来源为 `myh_assets`、`myh_media_analysis`、`myh_media_temporal_features`，并可选读取已有的 `myh_media_action_events` 提升动作定位精度。动作事件表不由本模块创建或修改；表缺失或没有事件记录时继续用原有逐镜摘要检索。当 `myh_assets` 存在而两张分析表有缺失时，首次任务以 `CREATE TABLE IF NOT EXISTS` 自动创建缺失表；不会 `ALTER` 已存在的表，也不会创建空的 `myh_assets`。

使用量另存在 `myh_media_usage_stats` 和 `myh_media_usage_events`；模型审核缓存使用 `myh_media_match_verifications`，均不修改素材分析表。数据库用户需要 `SELECT/CREATE/INSERT/UPDATE` 权限；失败时批任务会返回数据库错误，不会假装成功或跳过计数。

### 7.2 素材媒体 URL

数据库 `access_url` 必须是草稿构建端可访问的 HTTP(S) 地址。若数据库保存的是 `127.0.0.1`/`localhost`，设置：

```text
MEDIA_LIBRARY_PUBLIC_BASE_URL=http://<素材服务可达地址>:<端口>
```

程序会把该 base URL 和素材 `storage_path` 拼接。素材服务应从运行 CapCut/MATE 草稿生成代码的主机可达。

### 7.3 上传媒体与临时文件 URL

媒体根目录由 `VIDEO_ANALYSIS_MEDIA_ROOT` 指定；未设置时使用 video-analysis 默认目录。批次快照和裁剪片段放在该根目录的 `script-video-batch/{task_id}/`。用于草稿访问的根 URL：

```text
SCRIPT_VIDEO_MEDIA_URL_BASE=http://<媒体 sidecar 可达地址>/media
```

Windows `scripts/windows_analysis/start.bat` 会根据 `--public-host` 和 `--media-port` 自动设置该地址，默认主机是 `127.0.0.1`；局域网部署应传入服务器局域网 IP，例如 `--public-host 192.168.0.191`。Docker 默认 `http://nginx/media`。手动启动 API 时需自行设置此变量为草稿使用设备可达的媒体服务地址。

线上素材和本地批次媒体都必须从草稿生成侧可访问。媒体下载默认由草稿接口处理；若启用 `USE_REMOTE_MEDIA_URL=true`，草稿只保存远程 URL，剪映设备打开草稿时也必须能访问这些 URL。

## 8. 失败、重试和并发

| 现象 | 常见原因/处理 |
| --- | --- |
| HTTP `404` 查询任务 | `task_id` 不存在或已连到另一服务实例/数据库。 |
| HTTP `422` 提交校验失败 | 字段超范围、路径不是安全相对路径、模式缺少必需字段或请求带未知字段。 |
| SRT `404` | 任务目录或字幕文件不存在，通常表示任务尚未完成分镜与字幕时间对齐。 |
| SRT `400` | `task_id` 格式非法。 |
| HTTP `503` 提交失败 | 后台线程不可用或本地任务数据库/NAS 不可写。 |
| 查询状态 `failed` | 检查 `error_message` 和服务日志；常见为模型、Whisper、媒体权限/URL、FFmpeg、数据库或 Duix 错误。 |
| `partial_success` | 部分版本成功；使用 `versions[]` 保留的成功草稿，不要重跑整批以免产生重复草稿。 |
| 转写与脚本不匹配 | 检查视频/音轨是否对应脚本、片头偏移、语速或音频质量。 |
| 没有替换镜头 | 当前分镜是口播、素材分析尚未完成/没有有效片段，或分类、视觉意图、商品身份、时长要求未满足。字幕、贴字和水印不参与过滤；候选不足时保留母版，不会强行匹配。 |

批次任务 Worker 当前每个服务进程同一时刻执行一个批次；批次内部最多并发 4 条草稿。服务重启时 `queued/running` 任务标记为失败，不自动重放，避免重复生成。检查任务记录和已创建草稿后，再决定是否重新提交。接口没有幂等键，客户端超时后应先按业务侧记录确认是否已返回任务 ID，不能盲目重复 POST。

## 9. 任务记录及文件保留

- SQLite 任务库：NAS 下 `video-analysis/database/script-video-batch.sqlite3`。
- 口播输入快照、下载素材、裁剪片段和 SRT：`VIDEO_ANALYSIS_MEDIA_ROOT/script-video-batch/{task_id}/`。
- 当前没有自动清理批次媒体文件的任务；部署方应制定保留和清理周期。删除目录前确认对应任务不再需要预览或重试。
- 草稿保存在既有剪映草稿目录，批任务不会自动删除用户已成功生成的草稿。

## 10. 安全和生产注意事项

- 不要把数据库密码、模型 API Key、音频 Cookie、Duix 凭证写入请求日志或公开文档。
- 限制接口网络访问和调用频率；既有分镜内容及媒体链接均应按不可信输入处理。
- 生成前确认产品宣传内容合规。模型分镜不构成产品功效核验或法律审查。
- 首次建表需要 MySQL DDL 权限，建议先由 DBA 在测试库验证；在线已有表不会被改动。
