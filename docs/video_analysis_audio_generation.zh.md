# 视频分析模块：文本生成语音

接口前缀：

```text
/openapi/capcut-mate/v1/audio
```

本功能调用用户有权使用的 Ocean Engine 智能剪辑语音接口。Cookie 只保存在当前 API 进程内存，不写入任务数据、日志或响应；服务重启后需要重新设置。请勿将这些接口暴露在未受保护的公网环境中。

## 1. 设置并验证凭证

`POST /credentials` 会使用短文本调用断句接口验证 BPID 和 Cookie。仅在验证成功后，凭证才保留在当前进程内存。

```json
{
  "bpid": "你的 BPID",
  "cookie": "浏览器 Cookie 请求头内容"
}
```

成功响应只说明凭证已配置，不回显 BPID 或 Cookie：

```json
{
  "code": 0,
  "message": "成功",
  "configured": true
}
```

其他凭证接口：

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/credentials` | 查看当前进程是否已配置，不返回凭证 |
| `DELETE` | `/credentials` | 清除内存中的凭证 |

## 2. 提交语音生成任务

`POST /tasks` 接收文本、音色 ID、语速及可选的多音字读音覆盖。提交后立即返回 `task_id`，后台执行断句、语音生成、下载和 WAV 转换。

```json
{
  "text": "需要生成语音的完整文本",
  "voice_element_id": "29644801026",
  "voice_speed": 1.1,
  "pronunciation_overrides": [
    {
      "text": "乐",
      "phonetic": "le4"
    }
  ]
}
```

`pronunciation_overrides` 默认为空。每项会将匹配文本转换成上游语音接口的拼音标注，不会默认对任何品牌词进行改写。

提交响应示例：

```json
{
  "code": 0,
  "message": "成功",
  "task_id": "vaa_...",
  "status": "queued",
  "stage": "queued",
  "progress": 0,
  "created_at": "..."
}
```

## 3. 查询任务

`GET /tasks/{task_id}` 查询任务状态。`status` 为 `completed` 时，响应包含 `download_url`；如果凭证失效，`error_message` 会提示更新 Cookie。

```text
GET /openapi/capcut-mate/v1/audio/tasks/{task_id}
```

任务状态保存在 NAS 上的 `video-analysis/database/audio-generation-tasks.sqlite3`；Cookie/BPID 不落盘。服务重启后，未完成任务会保留查询记录并标记失败，提示核实后重新提交；服务重启后须重新设置凭证。音频文件保存在 NAS 的 `audio-generation/results`，可通过 `VIDEO_ANALYSIS_AUDIO_ROOT` 指定目录。

## 4. 下载音频

```text
GET /openapi/capcut-mate/v1/audio/files/{task_id}
```

输出为 WAV，单声道、16 kHz、16-bit PCM。下载前会检查音频参数，超过大小限制或格式校验失败的结果不会标记为完成。

## cURL 示例

PowerShell：

```powershell
$base = "http://127.0.0.1:30000/openapi/capcut-mate/v1/audio"

Invoke-RestMethod "$base/credentials" -Method Post -ContentType "application/json" `
  -Body (@{ bpid = "你的BPID"; cookie = "你的Cookie" } | ConvertTo-Json)

$task = Invoke-RestMethod "$base/tasks" -Method Post -ContentType "application/json" `
  -Body (@{
    text = "需要生成语音的文本"
    voice_element_id = "29644801026"
    voice_speed = 1.1
  } | ConvertTo-Json)

Invoke-RestMethod "$base/tasks/$($task.task_id)"
```
