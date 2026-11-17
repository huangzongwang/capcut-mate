# 产品口播视频接口

## 隔离与启动

实现位于 `src/grok_video`，由默认入口 `main.py` 自动挂载，后台 Worker 随项目启动和停止。
不修改剪映导出、分析任务或线上数据库表结构。
使用原来的项目启动方式即可启用此功能，例如：

```powershell
D:\anaconda3\envs\capcut-mate\python.exe -m uvicorn main:app --host 0.0.0.0 --port 30000 --workers 1
```

`python main.py` 和现有 Windows 启动脚本同样会启用。旧入口 `grok_video_app:app` 保留兼容，
复用同一个应用，不重复注册路由。更新代码后需要重启现有服务。

本功能仅提供 API，不新增桌面页面。任务遵循分析模块的提交、查询状态、领取结果流程。
没有单独的远程 Worker 领取接口；后台 Worker 原子领取本地持久化队列。

独立 SQLite：`db/grok_video.sqlite3`；视频：`reports/grok-video`。
可用 `GROK_VIDEO_DB_PATH`、`GROK_VIDEO_RESULT_ROOT` 覆盖，默认路径与工作目录无关。
使用操作系统文件锁限制同一任务库只有一个 Worker；请使用单进程部署。
不要把多个独立实例接到同一供应商 Key 后期待此锁实现跨机器并发限制。

## 线上模型

只读复用现有线上模型目录的数据库查询、字段解析、密钥解密，不调用分析的模型选择或 Responses 客户端。
数据库及密钥设置见 `online-model-migration.zh.md`。没有本地模型回退。
请求 `model_id` 是 `myh_video_models.id`，不是模型名称，也不是接入记录 ID。
本功能要求数据库模型名称为文档支持的图生视频模型 `grok-imagine-video-1.5`。
模型和接入均须启用、未删除。`metadata.protocol` 建议设为 `grok_video`；
允许未设置，明确设为 `responses` 等其他协议会拒绝。
接入 `base_url` 填域名根地址或 `/v1`，不填 `/responses` 或完整生成端点。
视频接入 `timeout_seconds` 范围为 1–180 秒；生图接入为 1–300 秒，建议配置 300 秒。
通过既有 `GET /openapi/capcut-mate/v1/models` 查询线上记录。
提交保存非密钥配置指纹，执行/恢复时配置发生变化会拒绝，不自动换模型。

## 创建任务

统一前缀：`/openapi/capcut-mate/v1`。

`POST /grok_video_tasks`

```json
{
  "model_id": 14,
  "image_url": "https://your-public-host/product.png",
  "person_description": "35岁左右的虚构成年女性，齐肩黑发，干练亲切",
  "product_name": "示例产品",
  "script": "这里填写本次普通话口播文案。",
  "display_mode": "handheld",
  "duration": 15,
  "resolution": "720p"
}
```

示例 ID 14 仅为占位，须替换为实际线上图生视频模型记录 ID。

| 字段 | 约束 |
| --- | --- |
| model_id | 必填正整数；不接受默认分析模型 |
| image_url | 原有直接图生视频模式必填；公网场景图片 URL 或 PNG/JPEG/WebP Base64 Data URL |
| person_description | 模板模式必填，每次请求重新提供 |
| product_name | 模板模式必填 |
| script | 模板模式必填，保留原始中文 |
| display_mode | `table` 默认；或 `handheld` |
| duration | 默认 15；只接受 1–15 的整数，17 秒会拒绝 |
| resolution | 默认 `720p`；可用 `480p`、`720p`、`1080p` |
| aspect_ratio | 默认 `9:16`；其他比例按上游文档枚举 |
| n | 固定 1 |
| prompt | 自定义完整提示词；与人物、产品、文案三个模板字段互斥 |

自定义 prompt 原样传入，调用方自行确保其时长与 duration 一致。
模板自动生成英文视觉描述、保留中文口播，根据 duration 缩放动作时间轴。
手持模式只允许空闲手做手势，产品外观以参考图为准。
直接图生视频模式的 URL 原样交给上游，本服务不会下载该场景图。Data URL 做 Base64、签名和 10 MiB 大小检查，
不是完整图像解码校验；实际图像兼容性由上游确认。没有上传本地路径接口，
调用方读取本地图片并编码为 Data URL 即可。
不支持参考视频、多图、size、分析模型 ID 等未定义字段。
提示词不能保证语音逐字准确、嘴型同步、包装文字准确或产品完全一致，须验收样片。

返回 `task_id`、`status=queued`、`stage`、`progress`、`created_at`。
进度是阶段估计，不是上游真实百分比。当前单任务串行执行。

## 产品图生成首帧再生成视频

旧请求继续使用 `image_url`，不会触发生图。新增流程使用 `product_image_url`，
不允许同时传 `image_url`。产品图只交给生图模型；视频模型接收已保存的完整场景首帧。
两个阶段均由调用方指定线上数据库模型 ID，不使用分析模型默认值，不自动切换模型。

```json
{
  "model_id": 27,
  "image_model_id": 28,
  "product_image_url": "https://example.com/product.png",
  "first_frame_mode": "prompt",
  "first_frame_prompt": "虚构成年女性站在明亮的展示桌后，右手自然握住产品，现代室内环境",
  "display_mode": "handheld",
  "prompt": "固定机位，人物自然介绍手中的产品，保持脸部、服装和产品外观一致",
  "confirm_first_frame": false,
  "resolution": "720p",
  "aspect_ratio": "9:16",
  "duration": 10
}
```

模型 ID 为示例。`first_frame_mode=auto` 时不传 `first_frame_prompt`，
生图提示词依据产品、摆放方式和视频描述自由设计环境与服装；
自动创作以一名人物正在介绍上传的产品为核心，可选择发布会、演讲台、居家、
花园、露台、公园等单一合理场景，不固定白桌、西装或幕布。
未指定人物时由远端自由设计成年人物的年龄、性别与外貌，可为青年、中年或老年；
用户在人物设定、首帧提示词或视频描述中明确指定的要求优先。
场景示例不是随机抽签或轮换规则，不保证每次输出不同。
手持模式保持自然持握，摆放模式按场景选择稳定台面，人物与产品均须清晰可见。
`prompt` 模式必须提供 `first_frame_prompt`。
视频仍可用原有人物、产品、口播模板字段替代 `prompt`，
此时视频模板会沿用首帧人物、服装和场景，避免与用户首帧设定冲突。
产品图可使用与 `image_url` 相同的公网 URL 或 Data URL。这里没有新增文件上传接口。

生图按照用户提供的 `gpt-image-2-api(1).md` 的图生图协议接入。
模型名称、接入地址和密钥由 `image_model_id` 对应的线上配置决定，不限制生图模型名称。
所选模型需兼容本文的 multipart 图生图协议；
`metadata.protocol` 建议设为 `gpt_image_2`，也允许未设置；
明确配置为 `responses`、`grok_video` 或旧的 `grok_image_edit` 会拒绝。
旧的待验证 JSON 图片编辑适配器已替换，不再向 `/images/edits` 发送 JSON。
接入地址和密钥从该模型的线上配置读取，不读取本地生图技能密钥，不改线上配置。
文档根地址为 `http://api.zetarouter.com`；配置使用供应商实际支持的根地址或 `/v1`。
模型及接入均须启用，Key 所属分组须开通该模型权限。当前已做离线协议测试，尚未付费联调。

适配器契约：

- Bearer 鉴权，同步 `POST /v1/images/edits`，采用 `multipart/form-data`。
- 产品图通过文件字段 `image` 上传；请求库自动设置 multipart boundary，不手工指定 Content-Type。
- 表单字段：`model`（线上配置的模型名称）、`prompt`、`n=1`、`size`、
  `quality=high`、`response_format=b64_json`、`output_format=png`，不传 `aspect_ratio`。
- `size` 根据视频尺寸取最接近的 16 倍数，默认 `720x1280`；
  例如 1080p 竖屏请求 `1088x1920`，首帧校验后规范为 `1080x1920`。
  文档建议 16 倍数，但未穷举所有支持尺寸，实际供应商拒绝时会明确报错，不自动重复付费请求。
- 成功响应 HTTP 200，返回 `data[0].b64_json`；不支持只返回图片 URL、SSE 或异步生图任务 ID。
- 生图使用独立模型配置指纹；响应保存到本模块任务库后才继续。
  同步协议没有强制要求上游任务 ID，因此不提供上游生图轮询或结果未知时自动重试。

产品图为 Data URL 时直接解码；为公网 URL 时先下载后上传文件。
下载不携带供应商密钥、不接受重定向、不读取代理环境配置；
校验 DNS 地址并固定连接到公共 IP，HTTPS 保留原域名的证书校验。
输入文件上限 10 MiB、2500 万像素，仅支持有效的单帧 PNG/JPEG/WebP。
`preparing_product` 表示准备产品文件，此时尚未提交生图；
只有进入 `image_submitting` 后才存在结果未知或生图计费的可能。

生成首帧后校验图片格式、像素数、大小及画面比例，默认规范到 720×1280。
比例误差超过 2% 会进入 `first_frame_failed` 并阻止视频提交，不裁剪或补黑边伪装首帧构图；
小范围比例误差用于兼容模型尺寸取整。通过校验的首帧保存在 `{task_id}.first-frame.png`。
首帧处理复用已安装的 Pillow，不修改分析模块。
自动校验不包含产品外观识别、持握姿态或品牌文字准确性。

`confirm_first_frame=true` 时进入 `awaiting_frame_confirmation`，暂停视频生成：

- `GET /grok_video_tasks/{task_id}` 返回 `first_frame`，包含预览地址、像素尺寸及生图模型 ID。
- `GET /grok_video_tasks/{task_id}/first_frame` 获取 PNG，任务完成后仍可读取。
- `POST /grok_video_tasks/{task_id}/confirm_first_frame` 原子确认并继续，同一状态只能确认一次。
- `POST /grok_video_tasks/{task_id}/retry_cached_stage` 显式重试已缓存阶段：
  仅允许已保存生图响应的 `first_frame_failed`，或已保存首帧且尚无视频上游 ID 的 `failed`。
  前者重新校验原响应，不重新生图，因此不能自动修复错误构图；
  后者复用首帧再次尝试视频提交，可能产生视频费用。
  `submission_unknown` 不允许重试；已提交视频的下载失败使用原来的 `retry_download`。

默认 `confirm_first_frame=false` 自动继续。最终结果增加 `first_frame`，
图片原始响应及首帧独立持久化，重启恢复不会重复生成已保存的首帧。
若进程在生图提交后、响应保存前中断，标记 `submission_unknown`，
`stage=image_submission_unknown`，须核对生图供应商。
视频轮询的 15 分钟从视频提交开始计时，不计入生图或人工确认等待时间。
本模块 SQLite 自动增加三个可空内容的文本列，旧任务继续可读，不改线上模型库表结构。

## 查询、领取和下载

- `GET /grok_video_tasks/{task_id}`：建议每 5–10 秒查询。
- `GET /grok_video_tasks/{task_id}/result`：成功后领取结果，可重复读取，不删除、不消耗。
- `GET /grok_video_tasks/{task_id}/content`：下载本地 MP4，无须暴露供应商 Key。
- `POST /grok_video_tasks/{task_id}/retry_download`：仅 `download_failed` 可用，不重新生成。

结果包含模型 ID/名称、上游任务 ID、实测视频时长、请求时长、分辨率参数、比例参数和 content_url。
`resolution`、`aspect_ratio` 保留请求值；`width`、`height`、`actual_aspect_ratio`
返回成片实测像素尺寸和显示比例。`source_width`、`source_height`、`source_aspect_ratio`
记录上游原片尺寸，`geometry_processing` 为 `none` 或 `fit_pad`，`warnings` 说明修正情况。

下载后进入 `validating_output` 阶段，用 FFprobe 检查视频。尺寸或显示比例不符时，
按请求分辨率的短边等比缩放并补黑边，不裁剪原画面；保留音频，并验证时长。
例如 `480p`、`9:16` 输出 480×854，使用显示比例元数据保持精确 9:16。
补边不能变成原生竖屏构图，放大也不会增加原生细节。
原片保存为结果目录下的 `{task_id}.original.mp4`。
处理需要 FFmpeg（支持 libx264）和 FFprobe；默认从 PATH 查找，
也可通过 `GROK_VIDEO_FFMPEG`、`GROK_VIDEO_FFPROBE` 指定可执行文件，
未设置时兼容 `SCENE_FFMPEG`、`SCENE_FFPROBE`。
校验或转换失败时进入 `download_failed`，可调用原有重试下载接口，不重新付费生成。
下载检查 MP4 文件头及已声明的字节长度，不是完整视频解码验收。下载上限 512 MiB。

状态：`queued`、`running`、`completed`、`failed`、`submission_unknown`、`download_failed`。
产品首帧流程另有 `awaiting_frame_confirmation`、`first_frame_failed`。
未成功完成时领取结果返回逻辑 409，不存在返回逻辑 404。
沿用框架响应中间件：传输 HTTP 200 不一定成功，必须检查响应 `code` 和任务 `status`。
视频使用完整流式响应，不提供 Range/206，以避免现有中间件破坏播放或下载。

## 恢复与失败处理

原有直接图生视频只对上游三个文档端点发请求：创建、状态查询、内容下载。
可选产品首帧流程额外使用上述经配置确认的图片编辑端点。
POST 提交不自动重试。网络错误、5xx、成功响应缺少有效 ID 或提交时宕机，
会标记 `submission_unknown`，须先人工核对供应商，不能盲目重提导致重复计费。
没有提交幂等键，调用方也不能因为自身请求超时就自动重复创建。

上游 ID 已保存的任务重启后继续查询，绝不再次创建；已获取结果但下载失败可单独重试。
状态查询对网络错误、429、5xx 每 5 秒继续等待，累计等待期限约 15 分钟
（从首次提交开始计时，重启不重置）。超时不代表远端取消，远端仍可能完成及计费。
远端 failed/expired、不可恢复 HTTP 错误或配置变更会标记 failed。
暂不提供远端取消、自动清理、列表分页或额外重生成接口。
本功能不新增鉴权体系，沿用部署入口；公网部署需在网关限制认证、请求体大小和访问权限。
视频和请求可能含产品资料，应按部署方策略管理目录权限、备份与保留周期。

## 离线验证

```powershell
D:\anaconda3\envs\capcut-mate\python.exe -m unittest discover -s tests -p test_grok_video.py -v
D:\anaconda3\envs\capcut-mate\python.exe -m unittest discover -s tests -p test_grok_video_geometry.py -v
D:\anaconda3\envs\capcut-mate\python.exe -m unittest discover -s tests -p test_grok_video_first_frame.py -v
```

测试使用临时目录和模拟上游，不读取实际线上 Key、不提交付费生成任务。
尺寸测试需要本地 FFmpeg/FFprobe，并检查真实转换、音频保留及原片完整性。
