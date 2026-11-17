# 可选 auto 分镜检测

## 兼容范围

默认仍为 `enhanced`。`content`、`adaptive`、`enhanced` 保持原检测参数和执行路径。
只有新请求显式传入 `"detector": "auto"` 才使用新策略。历史任务、报告、草稿不会重算。
改动仅在视频分析扩展的 schema、worker、检测/切片/草稿模块及测试文档；
未修改 main.py、框架路由、中间件、剪映核心导出、模型配置或 Grok 模块。
需按现有部署方式重启 API，才能识别新枚举；本次开发不自动重启正在运行的服务。

以下现有接口支持新策略，路径前缀为 `/openapi/capcut-mate/v1`：

- `POST /detect_scenes`：只检测，不调用云端。
- `POST /split_scenes`：检测并切片，默认 strict。
- `POST /analyze_video`、`POST /analyze_video_tasks`：完整分析。
- 继承检测请求的本地转写/特征接口也接受 auto。

只做检测的示例：

```json
{
  "video_path": "uploads/example.mp4",
  "detector": "auto"
}
```

分析任务示例（模型 ID 应替换为本部署实际启用记录）：

```json
{
  "video_path": "uploads/example.mp4",
  "detector": "auto",
  "scene_model_id": 14,
  "global_model_id": 13
}
```

## 策略

一次原视频解码，保留全画面/固定 ROI 评分以及每帧 32×32 缩略图。
全画面始终参与候选发现，固定 ROI 只是补充，不把裁剪范围当作实际画面边界。
只有缩略图的时间变化支持“中间活跃、边缘稳定”时，才补充活跃区域评分；
无法确认时不使用该动态裁剪。区域判断是启发式，不是 OCR 或语义画面识别。
活跃区域评分使用已安装 PySceneDetect 的 ContentDetector，不再解码原视频。

候选需满足局部中位数基线下的突变比例、绝对最低分、前后变化持续性、
变化覆盖面积和最短镜头间隔。单帧闪光、小面积字幕变化不能只凭突变分数直接切镜。
最短片段限制也适用于 auto 的片头/片尾，因此更短的镜头可能需要人工确认。

对超过 12 秒的片段复查缓存候选和低分辨率画面，模糊突变或渐变只输出
`needs_review` 证据/区间，不自动当作可信边界，不按固定时间强行拆分。
不新增云端复核，不依据模型的文字描述自动重切。
大幅动作、灯光变化、叠化及低对比切镜仍可能误判或漏检；本策略不是通用语义识别保证。

默认参数：`threshold=3`、`min_content_val=15`、`window_width=4`、
`persistence_ratio=1.5`、`min_scene_len=15`（帧）。前四项可显式设置。
auto 不接受 `score_region="roi"` 或非零 `refine_seconds`，避免静默忽略参数。
`include_diagnostics=false` 不会关闭 auto 的证据输出；旧策略语义不变。

## 最终边界和复核

auto 的边界复核不再把区域切点移动到附近全画面字幕峰值。
成功切片的帧区间是最终来源，随后分析、转写、字幕映射及草稿均读取相同区间。
auto 草稿素材按 `trim=start_frame:end_frame` 导出；旧策略仍走原时间导出路径。
切片沿用现有 CFR、帧数及质量校验，仍要求支持 `-fps_mode` 的 FFmpeg。

返回现有 `detection.settings` 和 `detection.diagnostics`，无需变更响应顶层结构：

- settings.auto_version：策略版本。
- settings.active_rows_32：动态区域的起止行，基于 32 行缩略图，结束行不包含。
- settings.regions：实际参与检测的区域。
- settings.review_required：是否存在待复核内部变化。
- diagnostics：候选帧、最终帧、接受/拒绝/待复核状态、理由和各区域证据。
- 渐变候选包含 start_frame/end_frame 区间，candidate_frame 仅是采样位置，不是确认切点。

`split_scenes` 的 strict 模式遇到待复核项返回 needs_review 且不导出。
调用方可通过现有 confirmed_ranges 提交人工确认区间，再次切片。
完整分析仍沿用 allow_review：只在可信边界切片，并将内部模糊变化放入诊断和警告，
不暂停已有异步任务协议。调用方应检查 settings.review_required。
没有新增复核 UI。

## 验证与性能

隔离分析环境运行 `tests/test_scene_auto.py`、`tests/test_scene_enhanced.py`；
API 环境运行 `tests/test_scene_auto_api.py`、原检测与切片测试。
测试覆盖固定边缘、全屏/顶部内容、真实编码视频、短镜头、闪光、小字幕、
连续变化、渐变复核、strict 阻止导出、人工确认区间，以及带音频/无音频草稿。
这些可重复合成测试不能替代更多真实视频的人工标注评测。

任务 va_20261016_053703_0b09697d 的本地验证中，旧策略保持 4 个切点，
auto 找到 18 个切点（19 段），没有修改原任务。测量数据在
`reports/audit-va-0b09697d/auto-benchmark.json`。
最终版本还标记了 2 个模糊变化区间，仅待复核，没有增加切点。
本地检测测量不包含编码、上传、逐镜模型调用；切片数量增加会增加这些阶段的耗时与费用。
暂不更换默认策略，待更多实际视频的漏切、误切、帧误差和耗时评测后再决定。
