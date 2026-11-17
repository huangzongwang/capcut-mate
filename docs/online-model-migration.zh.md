# 分析与脚本分镜：线上模型配置升级

日期：2026-10-14

## 范围与状态

模型配置改为只读线上 MySQL 的 `myh_video_models` 和 `myh_video_model_providers`。
不再读取、写入或自动创建本地 `video_analysis_model_configs`，不做旧 ID 映射或本地回退。
本次没有删除本地历史表，也没有修改线上数据。

视频任务、脚本任务、报告和草稿仍保存在本地。Whisper large-v3、OCR、分镜检测、
FFmpeg、原框架 UUID 导出 Key 不属于此次云端模型配置迁移。

代码更新需要重启服务才能生效。本次没有自动重启服务或提交真实云端分析。

## 数据库配置

固定读取项目根目录 `env.db.dev`，与启动工作目录无关。推荐当前字段：

```dotenv
ANALYSIS_DB_HOST=database-host
ANALYSIS_DB_PORT=3608
ANALYSIS_DB_NAME=database-name
ANALYSIS_DB_USER=read-only-user
ANALYSIS_DB_PASSWORD=database-password
```

已有 `DATABASE_HOST/PORT/NAME/USER/PASSWORD` 命名也可以读取，专属 `ANALYSIS_DB_*`
字段优先。这仅是数据库配置字段兼容，不是本地模型兼容。

连接采用只读事务，不运行建表、更新或删除语句。建议账号只授予所需表的 SELECT 权限。
`env.db.dev` 已加入 Git 与 Docker 构建忽略规则。Docker 中需将该文件只读挂载到项目根目录。
跨网络部署应使用可信内网/VPN；当前连接实现不自动配置 TLS，不建议直接暴露公网数据库。

## API 环境依赖

在运行 API 的 `capcut-mate` 环境安装，而不是只在媒体 Worker 环境安装：

```bat
conda run -n capcut-mate python -m pip install -r scripts/windows_analysis/requirements-api.txt
```

Windows 分析安装脚本已增加上述安装步骤，不修改框架依赖清单。

## 模型密钥解密

使用线上后台 `CryptoUtil` 的 Fernet 算法：

- 主密钥：API 进程环境变量 `DATA_ENCRYPTION_KEY`，未提供时读取 `SECRET_KEY`。
- 当前派生：HKDF-SHA256，info 为 `fastapiadmin:data-encryption:v1`。
- 轮换：`DATA_ENCRYPTION_OLD_KEYS`，逗号分隔。
- 历史密文：额外支持 SHA256(SECRET_KEY) 派生。

这些值必须与实际加密该接入 Key 的线上服务保持一致。不要重新生成一个不同的密钥，
不要将数据库中的 ciphertext 填成 API Key，也不要把密钥贴到聊天或提交到 Git。
本模块不使用线上代码里的隐式默认密钥，不自动读取其他项目的配置。
运行服务前，通过受控环境注入密钥。没有密钥或无法解密时明确拒绝模型调用。
数据库配置文件只保存数据库连接信息，不建议混放解密主密钥。

## 线上模型能力和协议

两张表必须同时启用、未软删除；仅按模型名称不能证明输入能力。
所有当前分析云端请求统一使用 Responses 协议（2026-10-15 更新）。不再读取协议标签决定调用方式，也不因能力标签缺失而拒绝用户指定模型。以下标签仅供目录展示，可选填写：

```json
{
  "protocol": "responses"
}
```

不同用途要求：

| 用途 | 建议能力标签（非调用门槛） | 输入 |
| --- | --- | --- |
| 视频逐镜 | `video`、`video_file` | 上传分镜视频，使用 input_video/file_id |
| 全局报告 | `text` | 逐镜结论、时间区间、字幕、转场的文本证据 |
| 脚本规划、纠错、复核 | `text` | 文本 |

空数组或空 metadata 不阻止调用；即使协议标签填写其他值，本分析模块仍使用 Responses。
实际输入兼容性由上游接口验证，模型不兼容时报告错误，不自动换用其他模型。
模型存在性、启用状态、地址、密钥解密及超时配置校验保留。

全局报告现在直接发送文本，不再生成并上传临时 PDF。逐镜仍上传视频，不发送 Base64 图片。
视频文件上传、查询、清理始终使用该逐镜模型对应接入的凭据。

模型可在 metadata 分别配置超时，例如：

```json
{
  "protocol": "responses",
  "scene_stream_idle_seconds": 120,
  "scene_stream_total_seconds": 300,
  "global_stream_idle_seconds": 120,
  "global_stream_total_seconds": 300,
  "script_stream_idle_seconds": 120,
  "script_stream_total_seconds": 300,
  "upload_timeout_seconds": 180,
  "file_process_timeout_seconds": 300
}
```

流式参数为正数且不超过 1800 秒；idle 默认 120 秒，total 默认 300 秒。
`*_stream_total_seconds` 为一次业务调用的预算，包含最多一次网络重试。
连接超时 15 秒，读取阻塞由 idle 限制；总预算在收包和重试前检查，
因此网络完全停顿时退出可能晚于总预算，最多额外等待一次 idle。
旧 `timeout_seconds` / `*_timeout_seconds` 不再覆盖流式读取超时，上传/文件处理配置不变。
上传、文件处理等待默认分别为 180、300 秒。
脚本生成、修正、复核共用 600 秒模型预算；每个视频分镜含结构修正共用 600 秒预算。
这些预算不包含排队、本地切割、上传和转写。
Base URL 填到 API 根路径，例如 `/api/v3`；兼容尾部 `/responses`，
不接受 `/files`、`/chat/completions`、URL 内嵌凭据或查询参数。

## 接口变化

以下路径统一位于 `/openapi/capcut-mate/v1` 下。

### 查询模型

`GET /models`

返回线上模型记录 ID、接入 ID、名称、启用状态、能力和更新时间。
不返回 API Key、密文、任意 metadata 或数据库连接信息。查询不需要解密密钥。

旧 `/model_configs` GET/POST 和 `/{config_id}` PATCH/DELETE 返回退休提示；
模型增删改请通过线上管理系统完成。

### 视频分析

`POST /analyze_video` 和 `POST /analyze_video_tasks`：

```json
{
  "video_path": "uploads/example.mp4",
  "use_external_model": true,
  "scene_model_id": 14,
  "global_model_id": 13
}
```

启用云端时两个模型 ID 必填，均指向 `myh_video_models.id`，不是 provider_id。
`use_external_model=false` 时可不传 ID，仅执行本地流程。
旧 `model_config_id` 明确拒绝，不静默忽略。

结果包含 `scene_model_id`、`global_model_id`、`models_used`；
其中记录实际模型、接入和配置更新时间及请求耗时，不包含密钥。
`model_used` 继续表示逐镜模型名称，完整两阶段信息以 `models_used` 为准。

### 脚本分镜

`POST /script_storyboards` 和 `POST /script_storyboard_tasks`：

```json
{
  "script": "下班后去跑步。",
  "product_names": ["产品全称"],
  "product_reference": "",
  "model_id": 13
}
```

`model_id` 必填，规划、结构纠错、语义复核使用本次模型。

`PATCH /script_storyboards/{board_id}` 与
`POST /script_storyboards/{board_id}/shots/{shot_id}/regenerate`：

- 保留原有 expected_version、镜头等必填字段。
- 可以增加 `model_id`，指定本次编辑复核或重生成模型。
- 不传时使用文档绑定的线上模型。
- 单次覆盖不改变整篇文档的默认模型绑定。
- 每个镜头 `model_usage` 记录最近一次 AI 操作的模型及调用耗时。
- 人工确认和只锁定/解锁仍不调用模型。

## 队列与历史文档

提交时校验线上配置，并保存不含密钥的模型绑定。执行前重新读取配置：
如果配置版本发生变化、被禁用、被删除、数据库断连或解密失败，明确失败；
不悄悄更换模型，也不回退本地。

旧排队请求没有线上绑定，执行时失败并提示重新提交。
历史报告文件不改写；旧脚本文档可以读取，但没有线上模型绑定，
首次编辑复核或重生成必须明确指定 model_id。

公共框架响应包装保持不变：调用端需检查 JSON 的 code，不只检查 HTTP 状态码。

## 代码位置

| 文件 | 修改内容 |
| --- | --- |
| `src/service/video_analysis/model_config_store.py` | 替换为线上只读查询、能力检查、解密、配置快照 |
| `src/router/video_analysis.py` | 模型只读目录、旧管理接口退休、配置异常响应 |
| `src/schemas/video_analysis.py` | 新模型 ID 请求和结果字段，拒绝旧请求参数 |
| `src/service/video_analysis/semantic_analysis.py` | 分离逐镜/全局模型，全局改为文本证据 |
| `src/service/video_analysis/model_client.py` | 超时、脱敏错误、每次请求及重试审计 |
| `src/service/video_analysis/video_files.py` | 对应接入上传、分阶段超时、脱敏失败信息 |
| `src/service/video_analysis/analysis_task_manager.py` | 提交和执行时校验配置版本 |
| `src/service/video_analysis/storyboard/` | 生成、复核、纠错、编辑、重生成及异步任务适配 |
| `scripts/test_analysis_task.py` | 改用逐镜/全局线上模型 ID |
| `scripts/configure_and_test_model.py` | 改为列出线上模型和测试，不再写本地配置 |
| `scripts/windows_analysis/requirements-api.txt` | API 侧新增依赖 |

## 测试调用

```bat
python scripts/configure_and_test_model.py
python scripts/test_analysis_task.py --video-path "uploads/example.mp4" --scene-model-id 14 --global-model-id 13
```

上述 13、14 是检查时已有的线上记录，不表示其视频能力已经验证通过。

## 本机检查结果

- 已通过 env.db.dev 成功只读查询线上目录，读取模型 13、14。
- 定向回归共 73 项，72 项通过、1 项原生媒体集成测试按开关跳过：
  线上配置 8 项、视频模型流程 7 项、脚本及队列 48 项、分析配套 10 项。
- 尝试全量 tests 目录发现时，当前 API 环境缺少 pytest、cv2、httpx2 等测试依赖，
  出现模块导入失败；没有为此改动原框架依赖或将媒体 Worker 依赖混入 API 环境。
- 检查时，两条模型的 capabilities 仍为空。
- 当前工具进程中没有 DATA_ENCRYPTION_KEY 或 SECRET_KEY。
- 因此尚未验证真实云端请求，不应宣称完整分析可以直接成功。
- 需要先在受控运行环境提供正确解密密钥，并在验证能力后配置 capabilities/metadata，
  然后重启服务、重新提交测试。
