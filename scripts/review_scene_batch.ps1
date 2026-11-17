param(
    [Parameter(Mandatory = $true)][string]$SourceDirectory,
    [Parameter(Mandatory = $true)][string]$BatchName
)
$ErrorActionPreference = "Stop"
if ($BatchName -notmatch '^[a-zA-Z0-9_-]+$') { throw "Invalid batch name" }
$project = Split-Path -Parent $PSScriptRoot
$media = Join-Path $project "media\$BatchName"
$report = Join-Path $project "reports\$BatchName"
if ((Test-Path -LiteralPath $media) -or (Test-Path -LiteralPath $report)) {
    throw "Batch already exists; use a new name"
}
$files = @(Get-ChildItem -LiteralPath $SourceDirectory -File |
    Where-Object { $_.Extension.ToLowerInvariant() -in '.mp4', '.mov', '.mkv', '.avi', '.webm' } |
    Sort-Object Name)
if (-not $files.Count) { throw "No videos found" }
New-Item -ItemType Directory -Path $media, $report | Out-Null
$results = [System.Collections.Generic.List[object]]::new()

function Call-SceneAPI($endpoint, $payload, $timeout) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $response = Invoke-RestMethod -Method Post `
            -Uri "http://localhost:30000/openapi/capcut-mate/v1/$endpoint" `
            -ContentType 'application/json' -Body ($payload | ConvertTo-Json -Depth 8) -TimeoutSec $timeout
    } catch {
        $response = [PSCustomObject]@{ code = -1; message = $_.Exception.Message }
    }
    $watch.Stop()
    return [PSCustomObject]@{
        request = $payload; elapsed_seconds = [math]::Round($watch.Elapsed.TotalSeconds, 2)
        response = $response
    }
}

function Save-Report {
    $text = [System.Collections.Generic.List[string]]::new()
    $text.Add("# 视频分镜双接口评估")
    $text.Add("")
    $text.Add("来源：$SourceDirectory")
    $text.Add("本轮保持 enhanced 默认参数；切割使用 strict + compatible。原视频不修改。")
    $text.Add("画面复核是启发式判断；needs_review 不是已证实的错误，completed 也不是语义准确率认证。")
    $text.Add("")
    $text.Add("| 编号 | 文件 | 时长秒 | 检测镜头 | 切割状态 | 待复核切点 | 移动切点 | 导出段数 |")
    $text.Add("|---|---|---:|---:|---|---:|---:|---:|")
    foreach ($entry in $results) {
        $d = $entry.detect.response
        $s = $entry.split.response
        $flagged = @($s.boundary_reviews | Where-Object status -eq 'needs_review').Count
        $shifted = @($s.boundary_reviews | Where-Object shift_frames -ne 0).Count
        $status = if ($s.code -eq 0) { $s.status } else { "error:$($s.code)" }
        $text.Add("| $($entry.index) | $($entry.filename) | $($d.duration_us / 1000000) | $($d.scene_count) | $status | $flagged | $shifted | $(@($s.clips).Count) |")
    }
    foreach ($entry in $results) {
        $d = $entry.detect.response
        $s = $entry.split.response
        $text.Add("")
        $text.Add("## $($entry.index). $($entry.filename)")
        $text.Add("")
        $text.Add("SHA-256：$($entry.sha256)")
        $text.Add("检测耗时：$($entry.detect.elapsed_seconds) 秒；切割接口耗时：$($entry.split.elapsed_seconds) 秒。")
        if ($d.code -ne 0) { $text.Add("检测失败：$($d.message)") }
        if ($s.code -ne 0) { $text.Add("切割失败：$($s.message)") }
        if ($s.code -eq 0) {
            $text.Add("切割状态：$($s.status)；两个接口的原始检测帧区间一致：$($entry.detection_consistent)。")
            if ($s.status -eq 'completed') {
                $text.Add("输出目录：$project\output\scene-clips\$($s.job_id)")
                $text.Add("片段总大小：$([math]::Round(($s.clips | Measure-Object size_bytes -Sum).Sum / 1MB, 2)) MiB。")
            } else {
                $text.Add("严格模式未导出文件；需要查看下方待复核边界后决定是否人工确认。")
            }
        }
        if ($d.code -eq 0) {
            $text.Add("")
            $text.Add("| 镜头 | 开始 | 结束（不含） | 时长秒 | 帧范围 |")
            $text.Add("|---|---|---|---:|---|")
            foreach ($scene in $d.scenes) {
                $text.Add("| $($scene.index) | $($scene.start_timecode) | $($scene.end_timecode) | $($scene.duration_us / 1000000) | [$($scene.start_frame),$($scene.end_frame)) |")
            }
        }
        if ($s.boundary_reviews) {
            $text.Add("")
            $text.Add("| 原切点秒 | 复核切点秒 | 原帧 | 新帧 | 状态 | 原因 |")
            $text.Add("|---:|---:|---:|---:|---|---|")
            foreach ($boundary in $s.boundary_reviews) {
                $text.Add("| $([math]::Round($boundary.detected_frame / $d.fps, 6)) | $([math]::Round($boundary.selected_frame / $d.fps, 6)) | $($boundary.detected_frame) | $($boundary.selected_frame) | $($boundary.status) | $($boundary.reason) |")
            }
        }
        if ($s.clips) {
            $text.Add("")
            $text.Add("| 片段 | 实际开始秒 | 实际结束秒 | 帧数 | 最低单帧 PSNR dB | 下载 |")
            $text.Add("|---|---:|---:|---:|---:|---|")
            foreach ($clip in $s.clips) {
                $text.Add("| $($clip.filename) | $($clip.start_us / 1000000) | $($clip.end_us / 1000000) | $($clip.frame_count) | $($clip.min_frame_psnr_db) | [下载](http://localhost:30000$($clip.download_path)) |")
            }
        }
    }
    $results.ToArray() | ConvertTo-Json -Depth 20 |
        Set-Content -LiteralPath (Join-Path $report 'results.json') -Encoding utf8
    $text | Set-Content -LiteralPath (Join-Path $report 'report.md') -Encoding utf8
}

$index = 0
foreach ($file in $files) {
    $index++
    $name = ("{0:D2}" -f $index) + $file.Extension.ToLowerInvariant()
    $destination = Join-Path $media $name
    Copy-Item -LiteralPath $file.FullName -Destination $destination
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash -ne $hash) {
        throw "Copied video hash mismatch: $($file.Name)"
    }
    Write-Output "[$index/$($files.Count)] DETECT $($file.Name)"
    $detect = Call-SceneAPI 'detect_scenes' @{
        video_path = "$BatchName/$name"; detector = 'enhanced'; include_diagnostics = $true
    } 210
    Write-Output "[$index/$($files.Count)] SPLIT detect_code=$($detect.response.code) scenes=$($detect.response.scene_count)"
    $split = Call-SceneAPI 'split_scenes' @{
        video_path = "$BatchName/$name"; detector = 'enhanced'; include_diagnostics = $true
        boundary_policy = 'strict'; encoding = 'compatible'
    } 960
    $consistent = $null
    if ($detect.response.code -eq 0 -and $split.response.code -eq 0) {
        $left = ($detect.response.scenes | ForEach-Object { "$($_.start_frame):$($_.end_frame)" }) -join ','
        $right = ($split.response.detection.scenes | ForEach-Object { "$($_.start_frame):$($_.end_frame)" }) -join ','
        $consistent = $left -eq $right
    }
    $entry = [PSCustomObject]@{
        index = $index; filename = $file.Name; source = $file.FullName; sha256 = $hash
        size_bytes = $file.Length; detect = $detect; split = $split; detection_consistent = $consistent
    }
    $results.Add($entry)
    $entry | ConvertTo-Json -Depth 20 |
        Set-Content -LiteralPath (Join-Path $report ("{0:D2}.json" -f $index)) -Encoding utf8
    Save-Report
    Write-Output "[$index/$($files.Count)] DONE code=$($split.response.code) status=$($split.response.status) clips=$(@($split.response.clips).Count) elapsed=$($split.elapsed_seconds)s"
    if ($split.response.code -eq -1) { throw "Transport failed; stop to avoid overlapping an unknown running job" }
}
Write-Output "REPORT $report"
