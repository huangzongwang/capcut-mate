param(
    [Parameter(Mandatory = $true)][string]$SourceDirectory,
    [Parameter(Mandatory = $true)][string]$BatchName
)
$ErrorActionPreference = "Stop"
if ($BatchName -notmatch '^[a-zA-Z0-9_-]+$') { throw "Invalid batch name" }
$project = Split-Path -Parent $PSScriptRoot
$destination = Join-Path $project "media\$BatchName"
$report = Join-Path $project "reports\$BatchName"
if ((Test-Path -LiteralPath $destination) -or (Test-Path -LiteralPath $report)) {
    throw "Batch already exists; choose a new batch name"
}
$files = @(Get-ChildItem -LiteralPath $SourceDirectory -File |
    Where-Object { $_.Extension.ToLowerInvariant() -in '.mp4', '.mov', '.mkv', '.avi', '.webm' } |
    Sort-Object Name)
if (-not $files.Count) { throw "No videos found" }
New-Item -ItemType Directory -Path $destination, $report | Out-Null
$results = @()
$rows = @()
$text = [System.Collections.Generic.List[string]]::new()
$text.Add("# 镜头检测对比报告")
$text.Add("")
$text.Add("来源：$SourceDirectory")
$text.Add("参数：adaptive / threshold=3.0 / min_scene_len=15 帧。时间为帧率换算，结束帧不包含在区间内。")
$text.Add("仅检测画面切换，未进行语义分类或人工校正。")
$text.Add("")
$index = 0
foreach ($file in $files) {
    $index++
    $localName = ("{0:D2}" -f $index) + $file.Extension.ToLowerInvariant()
    Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $destination $localName)
    $payload = @{
        video_path = "$BatchName/$localName"
        detector = "adaptive"
        threshold = 3.0
        min_scene_len = 15
    }
    Write-Output "[$index/$($files.Count)] $($file.Name)"
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $response = Invoke-RestMethod -Method Post `
            -Uri 'http://localhost:30000/openapi/capcut-mate/v1/detect_scenes' `
            -ContentType 'application/json' -Body ($payload | ConvertTo-Json) -TimeoutSec 210
    } catch {
        $response = [PSCustomObject]@{code = -1; message = $_.Exception.Message}
    }
    $watch.Stop()
    $entry = [PSCustomObject]@{
        index = $index
        filename = $file.Name
        source = $file.FullName
        size_bytes = $file.Length
        elapsed_seconds = [math]::Round($watch.Elapsed.TotalSeconds, 2)
        request = $payload
        response = $response
    }
    $results += $entry
    $entry | ConvertTo-Json -Depth 12 |
        Set-Content -LiteralPath (Join-Path $report ("{0:D2}.json" -f $index)) -Encoding utf8
    $text.Add("## $index. $($file.Name)")
    $text.Add("")
    if ($response.code -eq 0) {
        $text.Add("时长：$($response.duration_us / 1000000) 秒；帧率：$($response.fps)；帧数：$($response.frame_count)；镜头数：$($response.scene_count)。")
        $text.Add("")
        $text.Add("| 镜头 | 开始 | 结束 | 时长（秒） | 开始帧 | 结束帧 | 人工差异备注 |")
        $text.Add("|---|---|---|---:|---:|---:|---|")
        foreach ($scene in $response.scenes) {
            $text.Add("| $($scene.index) | $($scene.start_timecode) | $($scene.end_timecode) | $($scene.duration_us / 1000000) | $($scene.start_frame) | $($scene.end_frame) | |")
            $rows += [PSCustomObject]@{
                video_index = $index; filename = $file.Name; scene_index = $scene.index
                start = $scene.start_timecode; end = $scene.end_timecode
                duration_seconds = $scene.duration_us / 1000000
                start_frame = $scene.start_frame; end_frame = $scene.end_frame
                start_us = $scene.start_us; end_us = $scene.end_us; notes = ""
            }
        }
        Write-Output "OK: $($response.scene_count) scenes, $($response.duration_us / 1000000)s, elapsed $($entry.elapsed_seconds)s"
    } else {
        $text.Add("检测失败：code=$($response.code)，$($response.message)")
        Write-Output "FAILED: $($response.code) $($response.message)"
    }
    $text.Add("")
    $results | ConvertTo-Json -Depth 12 |
        Set-Content -LiteralPath (Join-Path $report 'results.json') -Encoding utf8
    $text | Set-Content -LiteralPath (Join-Path $report 'report.md') -Encoding utf8
    $rows | Export-Csv -LiteralPath (Join-Path $report 'scenes.csv') -NoTypeInformation -Encoding utf8BOM
}
Write-Output "Report: $report"
