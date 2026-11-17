# 本地剪映批量导出

该模块只负责把已经生成的剪映草稿交给当前 Windows 桌面上的剪映专业版导出为 MP4。
它不调用 `/gen_video`，不校验简创 API Key，不扣积分，也不上传 COS、OSS 或 TOS。

## 运行条件

- API 必须原生运行在 Windows 已登录的交互式桌面，不能运行在 Docker 或 Windows Service Session 0。
- 剪映专业版需要已经登录并保持运行，建议停留在草稿首页。
- `DRAFT_SAVE_PATH` 必须指向当前用户的剪映草稿目录。
- 安装项目 Windows 扩展：`pip install -e ".[windows]"`。
- 运行期间不要锁屏。远程桌面直接断开可能导致 UI Automation 不可用。
- 仅支持剪映专业版 6.x 及以下版本；7.x 及更高版本会在提交任务前直接拒绝，不执行 UI 自动化。

## 配置

```text
VIDEO_ANALYSIS_LOCAL_EXPORT_ROOT=\\192.168.0.30\技术部\hzw\video-analysis\local-exports
VIDEO_ANALYSIS_LOCAL_EXPORT_PUBLIC_BASE_URL=http://192.168.0.191:30001/output/local-exports
```

未配置输出目录时，默认使用：

```text
{VIDEO_ANALYSIS_STORAGE_ROOT}/video-analysis/local-exports
```

未配置公开地址时，使用 `DOWNLOAD_URL + /output/local-exports`。

## 健康检查

```http
GET /openapi/capcut-mate/v1/local-export/health?probe_ui=true
```

`probe_ui=true` 会实际连接剪映窗口，但不会打开或修改草稿。
响应中的 `supported_version` 必须为 `true` 才能提交导出任务。

## 提交批量导出

```http
POST /openapi/capcut-mate/v1/local-export-tasks
Content-Type: application/json
```

```json
{
  "draft_ids": [
    "20261028142459e2899da6",
    "20261028142511a0575e18"
  ],
  "resolution": "1080P",
  "framerate": 30,
  "timeout_seconds": 1800,
  "continue_on_error": true
}
```

接口使用 HTTP 200 返回排队任务。单次支持 1–20 个不重复草稿。草稿不存在于本机剪映目录时，
程序会通过现有 `get_draft` 地址下载并本地化草稿。一个 Windows 桌面只有一个导出 Worker，
所以批量任务自动串行导出，避免多个 UI 自动化任务互相点击。

## 查询、取消和重试

```http
GET  /openapi/capcut-mate/v1/local-export-tasks/{task_id}
POST /openapi/capcut-mate/v1/local-export-tasks/{task_id}/cancel
POST /openapi/capcut-mate/v1/local-export-tasks/{task_id}/retry-failed
```

取消不会强制关闭正在导出的剪映窗口；当前草稿完成后，剩余排队草稿会标记为取消。
每个条目返回准备、导出、校验及总耗时。成功后 `video_url` 可以在局域网直接播放或下载。

## 状态

- 任务：`queued`、`running`、`completed`、`partial_success`、`failed`、`cancelled`
- 条目阶段：`queued`、`preparing_draft`、`exporting`、`validating`、`completed`、`failed`

服务重启时，正在操作剪映的条目会标记失败，避免无法确认剪映是否已完成时重复导出；可通过
`retry-failed` 明确重试。已成功生成的 MP4 和原草稿均不会自动删除。

