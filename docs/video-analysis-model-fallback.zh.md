# 视频分析模型协议与备用模型

完整接口路径、参数表、响应字段及调用示例见 [视频分析 API 接口文档](video-analysis-api.zh.md)。

适用于同步 `POST /openapi/capcut-mate/v1/analyze_video` 和异步
`POST /openapi/capcut-mate/v1/analyze_video_tasks`。未传备用模型时保持原有行为。

## 请求示例

```json
{
  "video_path": "uploads/example.mp4",
  "scene_model_id": 31,
  "scene_fallback_model_id": 14,
  "global_model_id": 16,
  "global_fallback_model_id": 13,
  "use_transcription": true,
  "use_external_model": true
}
```

ID 是线上模型目录 ID，示例值不保证在其他部署中存在。通过 `GET /models` 查询。
两个备用字段都可省略或传 `null`，必须为正整数，且不能与对应主模型相同。
关闭外部分析时不能指定备用模型。视频备用模型仍须具有视频理解能力。
所有指定模型在提交和执行时均校验配置；禁用、缺失、密钥错误或排队期间配置变化
属于配置错误，不会借助备用模型绕过验证。

## 协议

- Gemini：模型名以 `gemini-` 开头时默认走原生
  `/v1beta/models/{model}:generateContent`。当前镜头与上一镜头的完整 MP4 通过
  `inlineData` 发送，不在客户端抽帧。Google 官方端点使用 `x-goog-api-key`，
  中转端点使用 Bearer 鉴权。API 根地址可包含 `/v1` 或 `/v1beta`。
- GPT 文本汇总：保留 `/responses` SSE 协议，只发送逐镜文字证据与字幕。
  GPT/o 系列请求不固定发送 `temperature`。此版本不新增 Chat Completions 适配。
- 原有视频模型：仍使用 Files 上传及 Responses `input_video + file_id`。
- 线上模型 metadata 可通过 `protocol` 或优先级更高的 `scene_protocol` /
  `global_protocol` 指定 `gemini`、`gemini_native` 或 `responses`。
  其他旧协议标签不改变默认路由。

Gemini 单次请求内的视频 Base64 总量默认限制为 18,000,000 字节，可通过模型
metadata 的 `gemini_max_inline_bytes` 调整。超过限制会失败并尝试备用模型；
尚未实现 Gemini Files API 大文件上传。`video_fps` 仍控制旧 Files 路径，
Gemini 当前使用服务端默认采样。

## 切换规则

1. 每个镜头先用主视频模型。当前镜上传失败、超时、HTTP 错误、非 JSON、
   缺字段、字段类型或数值范围不合法时，尝试一次备用视频模型。
2. 全局总结独立使用主文本模型。请求失败或报告合并校验失败时，尝试备用文本模型。
3. Responses 底层现有有限网络重试结束后才切换；Gemini 原生请求失败直接切换。
   有备用时不额外重跑主模型的无效 JSON；没有备用时保留原有一次 JSON 修复重试。
4. 不重复已经成功的镜头；每个新镜头仍先尝试主模型，不全局停用供应商。
5. 跨供应商不会复用文件 ID。备用 Files 模型按需重新上传，完成后清理；
   备用 Gemini 模型直接发送本地完整视频。
6. 主备都失败：该镜标记失败并要求人工复核；全局主备都失败则保留逐镜报告。
   视频准备或切镜失败不触发模型备用。

## 结果与审计

- `semantics[].model_id`：该镜实际成功使用的模型 ID，失败时为 `null`。
- `semantics[].fallback_used`：是否由备用模型完成。
- `global_model_used_id`：实际成功生成全局报告的模型 ID，未完成时为 `null`。
- `fallbacks[]`：切换的 purpose、镜头索引（逐镜时）、主备 ID、错误类型及
  `succeeded`。成功切换也会保留记录，不一定产生 warning。
- `models_used`：保留主模型及指定备用模型的配置快照与调用记录；未调用者
  `calls` 为空。任务快照同样包含备用模型，防止排队期间配置悄然变化。
- 原有顶层 `scene_model_id`、`global_model_id`、`model_used` 仍表示主模型配置，
  不应拿来判断每个镜头的实际执行模型。

备用机制只处理失败，不会自动评判语义质量。模型返回合法但不准确的描述时，
仍需人工抽检。
