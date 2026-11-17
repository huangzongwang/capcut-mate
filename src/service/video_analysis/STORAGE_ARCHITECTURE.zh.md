# video_analysis 分布式存储约定

## 职责划分

- 节点本机 SQLite：任务排队、领取、重试、进度、Worker 锁和共享结果 outbox。
- 线上 MySQL：`myh_video_analysis_task_results` 共享任务结果索引；`myh_video_storyboards` 共享脚本分镜文档。
- NAS：上传媒体、分镜视频、音频、数字人成品、草稿依赖素材和最终报告。
- 本机临时目录：FFmpeg、Whisper、上传暂存、下载暂存、结果 JSON 暂存。

NAS 不创建 `.tmp`、`.part`、探测文件或临时数据库。最终文件在本机完成校验后直接复制到 NAS 的最终文件名。

## 配置

```dotenv
VIDEO_ANALYSIS_NODE_ID=desktop-03nuq9t
VIDEO_ANALYSIS_NODE_DB_ROOT=E:/project/capcut-mate/.runtime/video-analysis/database
VIDEO_ANALYSIS_TEMP_ROOT=C:/Temp/capcut-mate/video-analysis
VIDEO_ANALYSIS_STORAGE_ROOT=//192.168.0.30/技术部/hzw
VIDEO_ANALYSIS_PUBLIC_BASE_URL=http://192.168.0.191:30000
```

- `VIDEO_ANALYSIS_NODE_DB_ROOT` 和 `VIDEO_ANALYSIS_TEMP_ROOT` 必须是本机磁盘，UNC/NAS 路径会被拒绝。
- 多台机器使用不同 `VIDEO_ANALYSIS_NODE_ID`；接口无需传 `node_id`。
- 节点只领取自己的本地任务；查询本机不存在的任务时，从 MySQL 读取共享状态和结果索引。
- 脚本分镜文档直接保存到 MySQL，旧 NAS `storyboards.sqlite3` 在首次读取对应分镜时自动迁移。
- MySQL 暂时不可用不会阻断已提交的普通本地任务状态更新；结果由本地 outbox 重试同步。脚本分镜保存依赖 MySQL，保存失败会明确标记任务失败。

服务重启后新配置生效；本次改动不会自动重启服务。
