# 本地 large-v3 视频转录

## 接口

```http
POST /openapi/capcut-mate/v1/transcribe_video
Content-Type: application/json
```

```json
{
  "video_path": "desktop4-20260921-dual-api/08.mp4",
  "model_name": "large-v3",
  "language": "zh",
  "word_timestamps": true,
  "vad_filter": true,
  "detector": "enhanced"
}
```

`model_name` 当前固定允许 `large-v3`，模型首次使用时下载到宿主机
`models/whisper`，之后离线复用。Docker 使用 `HF_ENDPOINT=https://hf-mirror.com`
下载模型；如果已经手工放入模型目录，也可以断网运行。

## 返回内容

- `segments`：Whisper 语音片段、开始/结束秒、文本、词级时间戳和置信字段。
- `srt`：可直接保存为 `.srt` 的字幕文本。
- `detection`：同一次请求产生的 enhanced 分镜时间表。
- `scenes`：按语音片段与视觉分镜的时间重叠映射后的台词、语音时长和语音密度。
- `device` / `compute_type`：实际使用的推理设备和精度。

分镜映射是时间重叠映射，不会修改视觉切点。一个语音片段跨过两个镜头时，会出现在两个分镜中，避免丢失口播上下文。

## 本地验证结果

样片：`media/desktop4-20260921-dual-api/08.mp4`（555.mp4）。

| 项目 | 结果 |
|---|---:|
| 模型 | large-v3 |
| 设备 | NVIDIA CUDA |
| 精度 | float16 |
| 视频时长 | 72.149 秒 |
| 语音片段 | 52 条 |
| 视觉分镜 | 8 段 |
| SRT 长度 | 2223 字符 |

完整返回保存于 `reports/whisper-555-large-v3.json`。

首次调用包含模型下载，耗时较长；模型缓存后只需执行分镜检测和转录。生产环境建议将转录作为异步任务，不要让前端请求长期占用 HTTP 连接。

## 资源与限制

- GPU 运行时使用 `ctranslate2`，当前容器验证可识别 1 个 CUDA 设备。
- large-v3 模型缓存约 3 GB，CUDA 运行库已包含在镜像中，镜像体积会明显增加。
- 医疗、价格、品牌和数字内容仍需人工复核；语音识别结果不能直接作为发布文案。
- 现有 `detect_scenes` 和 `split_scenes` 接口行为未改变。
