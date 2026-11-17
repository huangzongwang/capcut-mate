# 本地 Docker 镜头检测

本地 compose 使用 `Dockerfile.local`，在原 capcut-mate 镜像之上安装
`scenedetect-headless==0.7.1`。工具独立安装到 `/opt/scenedetect`，
不修改 API 的 Python 环境。现在已新增镜头检测 HTTP 接口，
详见 [视频镜头检测 API](video_analysis.zh.md)。

构建并启动：

```powershell
docker compose build capcut-mate
docker compose up -d --no-deps capcut-mate
docker compose exec -T capcut-mate scenedetect version
```

将视频放入项目 `html/output` 下，例如 `input.mp4`。此目录已经挂载到容器
`/app/output`；检测命令如下，结果输出到宿主机 `html/output/scenes`：

```powershell
docker compose exec -T capcut-mate scenedetect -i /app/output/input.mp4 -o /app/output/scenes detect-adaptive list-scenes
```

注意：现有 nginx 会提供 html 下的文件访问；私密视频请改用单独的私有目录挂载，
不要放入公开文件目录。镜头时间表仅表示画面切换，不包含语义分类和可替换判断。

该扩展未安装 FFmpeg，不提供 `split-video` 所需的外部切片工具。
使用 OpenCV 检测并输出镜头时间表不需要提前切出视频文件。
后续代码调用应使用 `/opt/scenedetect/bin/python` 或 CLI；
API 自身的 Python 环境不能直接导入此隔离环境中的包。

可重复的合成视频验证（在项目根目录执行）：

```powershell
docker run --rm --mount "type=bind,source=$((Get-Location).Path)\scripts\verify_scenedetect.py,target=/tmp/verify_scenedetect.py,readonly" capcut-mate-local:scenedetect-0.7.1 /opt/scenedetect/bin/python /tmp/verify_scenedetect.py
```

验证生成临时三镜头视频，检查是否准确识别第 50、100 帧的切镜点，并自动清理测试视频。
