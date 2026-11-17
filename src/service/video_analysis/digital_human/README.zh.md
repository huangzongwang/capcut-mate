# 数字人视频生成

该模块只接收人物视频和驱动音频，调用 Duix `/easy/submit`、`/easy/query`。不接入 ASR、TTS 或声音克隆。

## 存储

Windows 默认使用：

- 上传源、任务库、最终视频、分析临时文件：`\\192.168.0.30\技术部\hzw`
- Duix 预处理输入及上游输出共享目录：由每次任务请求传入，不设代码默认值

两者互相独立。源上传文件不会由生成任务自动删除。Duix 预处理文件仅在成功或明确失败后清理；超时任务会保留，以便恢复查询。

可通过环境变量配置 `VIDEO_ANALYSIS_STORAGE_ROOT`。Linux/Docker 必须先挂载 SMB，再配置两个容器都能访问的挂载点；未配置 `VIDEO_ANALYSIS_STORAGE_ROOT` 时服务会明确报错，不会静默写入容器本地磁盘。Duix 地址和共享目录由每个创建任务的请求传入并随任务持久化，便于任务恢复时仍查询原 Duix 实例。

```powershell
$env:VIDEO_ANALYSIS_STORAGE_ROOT = '\\192.168.0.30\技术部\hzw'
$env:DUIX_PUBLIC_MEDIA_BASE_URL = 'http://192.168.0.191:30001'
```

Docker 示例（SMB 挂载由宿主机负责）：

```text
VIDEO_ANALYSIS_STORAGE_ROOT=/mnt/hzw
DUIX_SHARED_ALLOWED_ROOT=/mnt/duix-temp
```

将 `/mnt/hzw` 和 `/mnt/duix-temp` 分别挂载到 API 容器；Duix 服务也必须将同一个 Duix 共享目录挂载到它自身的配置路径。`DUIX_PUBLIC_MEDIA_BASE_URL` 应是调用方可访问的媒体 sidecar 地址。Linux/Docker 的任务请求共享目录必须位于 `DUIX_SHARED_ALLOWED_ROOT` 内。

## 调用

先上传视频和音频，两个请求都必须传 `purpose=digital-human`：

```bash
curl -X POST "http://127.0.0.1:30000/openapi/capcut-mate/v1/upload_video" \
  -F "purpose=digital-human" \
  -F "file=@person.mp4"

curl -X POST "http://127.0.0.1:30000/openapi/capcut-mate/v1/upload_video" \
  -F "purpose=digital-human" \
  -F "file=@drive.wav"
```

取响应中的 `video_path` 和 `audio_path`，提交异步任务：

```bash
curl -X POST "http://127.0.0.1:30000/openapi/capcut-mate/v1/digital-human/tasks" \
  -H "Content-Type: application/json" \
  -d '{"video_path":"uploads/<视频文件名>.mp4","audio_path":"uploads/<音频文件名>.wav","duix_api_base_url":"http://192.168.0.26:8383","duix_shared_dir":"\\\\192.168.0.26\\duix_avatar_data\\face2face\\temp"}'
```

`duix_api_base_url` 要传 Duix 服务根地址（协议、主机和端口），只接受 RFC1918 IPv4 或 IPv6 ULA 私网地址；Windows 下 `duix_shared_dir` 必须是与服务 IP 同主机的 UNC 路径。此示例适用于 API 服务运行在 Windows 且该共享可写的情况。

响应返回本地 `task_id`。使用它查询：

```bash
curl "http://127.0.0.1:30000/openapi/capcut-mate/v1/digital-human/tasks/<task_id>"
```

若任务显示 `timed_out`，可恢复上游状态查询，不会再次提交生成任务：

```bash
curl -X POST "http://127.0.0.1:30000/openapi/capcut-mate/v1/digital-human/tasks/<task_id>/refresh"
```

完成后查询响应中的 `preview_url`。`task_id` 是本地任务标识；Duix 使用独立的 UUID `provider_code`，不对外返回。

## 任务恢复规则

- 生成提交成功后超时：保留 `running/timed_out`，可在服务启动时自动恢复，也可调用 refresh。
- 提交阶段中断：启动恢复时只查询已有 UUID，不会重复调用 `/easy/submit`。
- 旧记录中没有有效 Duix UUID：不能安全恢复，记录会转为失败并提示重新提交。
- 源上传文件不会自动删除。
