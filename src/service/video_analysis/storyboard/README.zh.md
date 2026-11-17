# 脚本语义分镜

> 实现状态：异步任务队列、同步分镜、读取、编辑、锁定、人工确认和局部重生成均已实现。更新代码后重启API服务即可注册任务接口，无需额外开启配置。

## 一句话说明

提交一段脚本，系统在本地队列中调用一次大模型生成分镜初稿，完成本地结构校验后保存到共享MySQL并把JSON报告发布到NAS，然后等待人工确认。

六类分镜为：

```text
产品空境、场景空境、身材展示、运动、机理图、口播
```

十八类 `visual_intent` 为：痛点、产品整体、包装细节、说明书、OTC/资质、二维码/溯源、真假辨别、用法用量、服药时刻、工厂生产发货、客服服务、身材展示、运动、减肥机理、生活场景、体验证据、行动引导和人物口播。`category` 保持旧流程兼容，`visual_intent` 用于精确拆镜和素材匹配。

注意：`赛乐赛奥利司他胶囊`等产品名称不会被强制归为产品空境。系统必须结合上下文和表达意图判断，例如（仅说明分类思路，不是固定映射）：

- “镜头展示赛乐赛奥利司他胶囊包装” -> 产品空境
- “赛乐赛奥利司他胶囊属于什么类型” -> 口播
- “它如何作用于脂肪吸收” -> 机理图，但需要产品资料支持人工确认

## 推荐使用流程

```text
1. 提交脚本任务
        |
        v
2. 后台排队并执行语义拆镜
        |
        v
3. 查询任务状态
        |
        v
4. 获取分镜结果
        |
        v
5. 人工修改、锁定和确认
        |
        v
6. 后续生成数字人、匹配素材、切片合成
```

本模块目前只做到第5步，不生成数字人、不生成音频、不检索素材、不切片、不创建剪映草稿。后续阶段通过
`storyboard_id`和`shot_id`关联。

## 队列接入流程

接口根路径：

```text
/openapi/capcut-mate/v1
```

### 1. 提交异步任务

```http
POST /script_storyboard_tasks
Content-Type: application/json
```

请求：

```json
{
  "script": "下班后去跑步。",
  "product_names": ["赛乐赛奥利司他胶囊"],
  "product_reference": "",
  "model_id": 13
}
```

在请求校验、任务持久化后返回任务ID，不等待模型。首次提交响应如下，完整字段含义见“任务字段”：

```json
{
  "code": 0,
  "message": "成功",
  "task_id": "sbt_20261013_030000_a1b2c3d4",
  "status": "queued",
  "stage": "queued",
  "progress": 0,
  "storyboard_id": null,
  "created_at": "2026-10-13T03:00:00+00:00",
  "started_at": null,
  "completed_at": null,
  "heartbeat_at": null,
  "error_message": null,
  "timings": {}
}
```

调用方必须保存`task_id`。网络超时后不要直接重复提交，因为模型调用可能产生费用。

### 2. 查询任务

```http
GET /script_storyboard_tasks/{task_id}
```

常见状态：

| `status` | `stage` | 含义 |
|---|---|---|
| `queued` | `queued` | 等待执行 |
| `running` | `planning` / `saving` | 正在生成或保存初稿 |
| `completed` | `waiting_review` | 已生成，可获取分镜 |
| `completed_with_warnings` | `waiting_review` | 已生成，但存在警告，需重点审核 |
| `failed` | `failed` | 任务失败 |

建议2～5秒查询一次，持续处理中可逐步退避到10秒。`waiting_review`不是失败，而是等待人工审核。

### 3. 获取结果

```http
GET /script_storyboard_tasks/{task_id}/result
```

仅`completed`和`completed_with_warnings`可以获取结果。返回的`storyboard_id`用于后续读取、编辑和确认。
下例对应上述脚本；`result`使用下文完整`Storyboard`结构，无顶层`warnings`，警告位于`shots[].warnings`。
结果接口读取生成时的快照；编辑后需要通过分镜读取接口获取当前版本，不能拿快照版本覆盖后续修改。

```json
{
  "code": 0,
  "message": "成功",
  "task_id": "sbt_20261013_030000_a1b2c3d4",
  "status": "completed",
  "storyboard_id": "sb_7f91",
  "result": {
    "storyboard_id": "sb_7f91",
    "version": 1,
    "created_at": "2026-10-13T03:00:01+00:00",
    "updated_at": "2026-10-13T03:00:01+00:00",
    "request": {
      "script": "下班后去跑步。",
      "product_names": ["赛乐赛奥利司他胶囊"],
      "product_reference": "",
      "model_id": 13
    },
    "model_used": "configured-text-model",
    "status": "needs_review",
    "timing_status": "awaiting_audio_alignment",
    "shots": [{
      "shot_id": "shot_example",
      "source_text": "下班后去跑步。",
      "source_start": 0,
      "source_end": 7,
      "category": "motion",
      "visual_intent": "exercise_action",
      "semantic_reason": "本句描述下班后的跑步行为。",
      "evidence": "去跑步",
      "visual_description": "人物在户外跑步，中景展示跑步动作。",
      "required_elements": ["跑步人物", "跑步动作"],
      "forbidden_elements": [],
      "relation": "affirmative",
      "confidence": 0.9,
      "locked": false,
      "review_status": "pending",
      "review_note": "",
      "warnings": [],
      "semantic_review": {
        "shot_index": 0,
        "supported": true,
        "reason": "画面与跑步行为一致。"
      }
    }]
  }
}
```

### 4. 审核和修改

```text
GET   /script_storyboards/{storyboard_id}
PATCH /script_storyboards/{storyboard_id}
POST  /script_storyboards/{storyboard_id}/confirm
POST  /script_storyboards/{storyboard_id}/shots/{shot_id}/regenerate
```

修改镜头后需要重新人工确认；锁定的镜头不能修改、删除、移动或重新生成。所有镜头确认后，
`storyboard.status=confirmed`。

## 任务是如何执行的

分镜规划使用以下生产约束：

- 逗号、顿号、分号、冒号、句号、问号、感叹号和省略号（中英文）每出现一个结束标点形成一个标点段，标点归属前一段。
- 标点只是模型规划分镜时允许使用的边界，不要求每个标点段单独成镜；口播按连续语义合并，非口播优先覆盖上下相邻2至3个标点段且最多3个。以上规则由模型执行，本地不按标点重新切分或拒绝结果。
- 产品介绍、产品外观和包装归产品空境；OTC、二维码、溯源码、真假辨别和服药用法分别使用对应的产品视觉意图。
- 肥胖、赘肉、裤子穿不上、体态或走路喘等语义归痛点/身材；工厂、仓库、生产和发货归工厂生产发货；只有明确运动动作才归运动。
- 分类必须结合完整语义和上下文，空镜的必需元素必须与当前原文一致；没有明确可视语义时保留人物口播。
- 草稿生成阶段可以合并相邻口播画面，因此短语义分镜不会必然造成最终视频轨碎片化。

```text
提交任务
  -> 独立分镜队列
  -> 单并发Worker领取
  -> planning：一次大模型调用生成分镜初稿
  -> saving：本地校验后保存共享MySQL文档并发布NAS JSON报告
  -> waiting_review：等待人工确认
```

使用独立资源，不修改原视频分析任务：

```text
代码：src/service/video_analysis/storyboard/
本地队列：节点SQLite storyboard-tasks.sqlite3
共享文档：线上MySQL myh_video_storyboards
NAS报告：{VIDEO_ANALYSIS_STORAGE_ROOT}/video-analysis/reports/storyboards/{task_id}.json
```

队列实现文件：

```text
task_store.py
task_manager.py
task_router.py
```

不修改原视频分析任务表、原视频分析逻辑、剪辑服务、共享Schema和桌面端协议。

## 任务字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | `sbt_`前缀的任务ID；查询和结果接口的必填路径参数，无请求体 |
| `status` | string | `queued`、`running`、`completed`、`completed_with_warnings`、`failed` |
| `stage` | string | 新任务使用`queued`、`planning`、`saving`、`waiting_review`、`failed`；旧记录可能包含`validating`、`reviewing` |
| `progress` | integer | 0～100的阶段性进度；排队为0，成功为100，失败保留失败前进度 |
| `storyboard_id` | string/null | 保存后的分镜文档ID，保存前为null；文件发布失败的任务也可能已有分镜ID |
| `error_message` | string/null | 失败原因或非致命警告摘要，没有时为null |
| `created_at` | string | 创建时间，UTC ISO 8601 |
| `started_at` / `completed_at` | string/null | 开始和结束时间，未发生时为null；格式同创建时间 |
| `heartbeat_at` | string/null | Worker最近一次心跳，未执行时为null |
| `timings` | object | 阶段耗时，单位毫秒，如`planning_ms`；尚无记录时为`{}` |

提交和查询响应使用上述字段，并由主应用添加`code`、`message`。
结果响应包含`code`、`message`、`task_id`、`status`、`storyboard_id`和`result`，
其中`result`为完整`Storyboard`。任务不存在返回404，未完成或失败时取结果返回409。
已完成任务的结果文件缺失、损坏或路径不合法返回500。
任务失败时，查询本身仍可返回`code=0`，必须同时检查`status=failed`。

任务表`storyboard_tasks`除上述持久化字段外，保存`request_json`（校验后的请求）、
`result_path`（内部结果路径）、`result_json`（可恢复的生成快照）和`timings_json`（耗时序列化数据）。
这些内部字段不直接返回客户端。
数据库沿用`STORYBOARD_DB_PATH`，默认路径见上文；`STORYBOARD_RESULT_ROOT`覆盖结果目录。

结果文件必须先落盘，再将任务更新为完成，避免出现“任务完成但结果不存在”。
生成任务完成后不因人工确认继续改变；人工审核状态以当前`Storyboard.status`为准。

## 异常处理和恢复

- 模型临时网络错误或限流：使用传输层统一重试预算，目标最多重试2次，队列层不叠加整任务重试。
- 模型输出结构、原文覆盖或两句上限校验不合法：直接失败，不自动调用模型修复，用户可检查后重新提交。
- 不执行第二次模型语义复核；低置信度、否定关系等本地警告仍可使任务标记为`completed_with_warnings`。
- 服务重启：恢复未开始的`queued`任务。
- 第一版按单服务进程、单Worker部署。持续更新心跳；心跳超时不能单独作为重新调用付费模型的依据。
- 重启遇到`running`任务：先核对已保存文档和结果文件，能恢复结果则补齐状态；无法确认是否执行完成时标记失败并人工决定重试。
- 失败任务保留请求快照、错误信息和阶段耗时。

任务先写数据库再加入内存队列，启动时从数据库恢复排队任务。领取时以状态条件更新防止重复消费。
本地任务用任务ID唯一关联共享分镜文档；恢复时复用已保存结果，不能重复调用付费模型。
`planner.generate()`只执行一次规划模型调用和本地校验，不做自动修复及第二次模型复核。
第一版不提供请求幂等键、取消、任务列表或自动重提失败任务。

部署使用单个API进程（`--workers 1`），不要让多个实例共享同一分镜任务库。Worker在路由生命周期中启动，
以任务库旁的操作系统文件锁避免双重恢复；锁文件不需要手动删除。每5秒更新运行任务心跳。
关闭服务时停止领取新任务并最多等待5秒；未结束的模型调用保留Worker所有权，强制退出后按上述规则恢复。
任务必须指定线上 model_id。提交时保存非敏感配置版本，执行前校验；排队期间配置变更需重新提交，不自动换模型。
已有分析传输层最多重试2次，队列层不叠加重试；同步生成仍可并行调用，单并发限制仅针对分镜任务队列。

更新后可在`/docs`查看`script-storyboard-tasks`，或检查`/openapi.json`是否包含
`/openapi/capcut-mate/v1/script_storyboard_tasks`。若仍404，应核对实际运行目录、部署代码和反向代理转发路径。

验收重点：提交不等待模型、排队任务重启可恢复、完成前结果已落盘、警告可见、原文完整覆盖、
产品名称不强制分类、现有编辑和确认接口保持兼容。

## 后续生产链路

```text
脚本分镜任务
  -> 人工确认
  -> 数字人口播任务
  -> 素材检索/生成任务
  -> 音频时间轴对齐任务
  -> 分镜切片与合成任务
  -> 剪映草稿或视频导出任务
```

`source_start/source_end`只是原始脚本的Unicode码点索引，不是视频时间。实际切片必须等音频或视频时间轴建立后再计算。

---

本模块仅负责“脚本 -> 语义分镜 -> 人工修订确认”，不生成数字人、不调用素材生成服务、不创建草稿。
所有新增实现、测试及说明都在本目录。唯一外部接入点是既有 `src/router/video_analysis.py`
挂载本模块路由；不修改原框架入口、剪辑服务、共享 Schema、依赖或桌面客户端。

## 现有接口使用（已实现）

使用项目现有启动方式，访问 `/docs` 中的 `script-storyboard` 接口。
从项目 env.db.dev 指向的线上模型目录读取用户指定模型；统一调用 Responses，不再以 capabilities 或 metadata.protocol 作为调用门槛。脚本模型实际需支持文本输入，由上游接口验证。
不会主动使用现有密钥运行演示或发送脚本；只有请求生成/修改内容/重新生成时才调用模型。

接口前缀：`/openapi/capcut-mate/v1/script_storyboards`。

| 方法 | 相对路径 | 功能 |
|---|---|---|
| GET | `/categories` | 六种兼容大类、十七种视觉意图与规则版本 |
| POST | 空 | 生成并保存分镜 |
| GET | `/{id}` | 读取分镜，也可保存返回JSON作为导出 |
| PATCH | `/{id}` | 修改、拆分、合并、锁定、解锁 |
| POST | `/{id}/confirm` | 人工确认指定镜头 |
| POST | `/{id}/shots/{shot_id}/regenerate` | 带完整上下文重新生成一个镜头，可拆为多镜 |

生成请求示例：

```json
{
  "script": "下班后去跑步。",
  "product_names": ["赛乐赛奥利司他胶囊"],
  "product_reference": "",
  "model_id": 13
}
```

示例只演示分类，不代表产品用法、机制或广告建议。`product_reference` 应填写审核过的产品资料。
必须指定 myh_video_models.id，不读取本地默认配置。原文上限12000字符，最多100镜；产品资料上限20000字符。
当前文档中的 `POST` 生成接口仍是同步调试接口，在线程池执行；只调用一次规划模型。生产调用优先使用异步任务接口。
正式业务使用上文的异步接口：
`POST /script_storyboard_tasks` 提交任务，轮询
`GET /script_storyboard_tasks/{task_id}`，完成后通过
`GET /script_storyboard_tasks/{task_id}/result` 获取分镜。
任务队列只负责“脚本 -> 语义分镜 -> 保存结果 -> 等待人工审核”，不触发数字人、素材生成或剪辑导出。
调用方必须保存 `task_id`；网络超时后不要自动重复提交可能产生模型费用的任务。

成功结果包含 `storyboard_id`、`version`、原请求和 `shots`。各镜头包含原文、大类、视觉意图、语义依据、
画面描述、必须/禁止元素、否定等语义关系、模型置信度、警告和人工确认状态。
模型置信度不是经校准的概率；当前不执行独立模型复核，初稿必须由人工确认。

`source_start/source_end` 是原始字符串的Unicode码点索引，左闭右开，不是秒数或UTF-16索引。
前端处理非BMP字符时须按码点转换。`timing_status=awaiting_audio_alignment` 明确表示尚无音频时间轴。
后续数字人/素材模块应通过镜头ID和原文范围进行语音对齐，不能直接把字符索引当作切片时间。

## 通用协议与调用顺序

- JSON请求设置 `Content-Type: application/json`。请求不接受未声明字段；所有字符长度均按Unicode码点计算。
- 以下HTTP路径均相对于上文接口前缀，不存在额外的尾部 `/` 要求；创建接口直接POST前缀。
- `board_id` 是生成响应的 `storyboard_id`；`shot_id` 是该文档的 `shots[].shot_id`。不要使用数组索引替代ID。
- 生成返回版本1；成功编辑、确认、重生成都会加1。每次写入使用最近响应中的版本，不要固定写1。
- 主应用响应在业务字段**顶层**添加 `code`、`message`，没有 `data` 包装层。`code=0` 才是成功。
- 本模块未新增身份认证。部署方应沿用网关访问控制，勿将含产品资料的接口直接暴露给不受信任的调用方。

同步调用顺序：分类查询 -> 同步生成 -> 读取/编辑 -> 人工确认。
推荐调用顺序：分类查询 -> 提交任务 -> 轮询任务 -> 获取结果 -> 读取/编辑 -> 人工确认。
需要修订某一镜时调用局部重生成，再确认新结果。同步生成接口保留用于调试和短脚本。
当前没有列表、删除、音频时间对齐、素材匹配、数字人生成或导出视频接口。

## 请求字段

### 1. GET /categories：分类查询

无路径参数、查询参数或请求体。返回字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `categories` | object | 下方六类分镜的英文枚举到中文名称的映射 |
| `visual_intents` | object | 十八类视觉意图的英文枚举到中文名称的映射 |
| `rules_version` | string | 当前规则版本 `1.5`，不是文档保存版本 |
| `code` | integer | 主应用包装字段，成功为0 |
| `message` | string | 主应用包装的结果消息，可能随语言变化 |

```json
{
  "code": 0,
  "message": "成功",
  "categories": {
    "product_empty_scene": "产品空境",
    "scene_empty_scene": "场景空境",
    "body_display": "身材展示",
    "motion": "运动",
    "mechanism": "机理图",
    "oral_delivery": "口播"
  },
  "visual_intents": {
    "pain_point": "痛点展示",
    "product_overview": "产品整体展示",
    "product_detail": "包装细节",
    "instruction_manual": "说明书展示",
    "otc_certification": "OTC/资质标识",
    "qr_traceability": "二维码/溯源码",
    "authenticity_comparison": "真假辨别",
    "usage_instruction": "使用方法/用量",
    "medication_moment": "服药时刻",
    "factory_process": "工厂、生产、发货",
    "service_support": "客服、指导服务",
    "body_condition": "身材展示、体态",
    "exercise_action": "运动行为",
    "mechanism_explanation": "减肥机理、作用机制",
    "lifestyle_scene": "饮食、工作、生活",
    "experience_proof": "体验和效果描述",
    "cta": "链接、购买、行动引导",
    "talking_head": "保留人物口播"
  },
  "rules_version": "1.5"
}
```

### 2. POST 前缀：生成

| 字段 | 类型 | 必填 | 默认值 | 说明与约束 |
|---|---|---|---|---|
| `script` | string | 是 | 无 | 原始脚本，1至12000字符，包含有效文字；保留所有标点、空白和原文顺序 |
| `product_names` | string[] | 否 | `["赛乐赛奥利司他胶囊"]` | 1至20项，每项1至300字符且非空白；去首尾空白并去重，用于身份识别和名称完整性保护，不决定分类 |
| `product_reference` | string | 否 | `""` | 最多20000字符的已审核产品资料；没有非空资料时禁止确认机理图 |
| `model_id` | integer | 是 | 无 | 线上 myh_video_models.id，正整数；没有默认模型回退 |

以上字段不接受null。历史文档读取时 model_id 可以为空，首次模型操作须显式指定。`request.product_names` 保存的是校验、去重后的列表。
本接口同步返回完整 `Storyboard`，不是任务ID。没有自动付费素材或数字人调用。

### 3. GET /{board_id}：读取

| 参数 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `board_id` | path | string | 是 | 创建响应的 `storyboard_id` |

无查询参数和请求体，不调用模型。返回当前完整 `Storyboard`。读取不会自动套用新规则重算旧结果。

### 4. PATCH /{board_id}：编辑

路径参数 `board_id` 同上。请求体：

| 字段 | 类型 | 必填 | 默认值 | 说明与约束 |
|---|---|---|---|---|
| `expected_version` | integer | 是 | 无 | 最近读取的 `version`，至少1；不匹配返回版本冲突 |
| `shots` | ShotEdit[] | 是 | 无 | 编辑后的**完整列表**，1至100镜，不是只传发生变化的镜头 |

每个 `ShotEdit` 须提交后文“镜头内容字段”中所有必填字段，并支持：

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `shot_id` | string或null | 否 | `null` | 修改原镜头时携带现有ID，不可重复或引用其他文档ID；新增镜头省略或传null |
| `locked` | boolean | 否 | `false` | 保存后的锁状态；省略会按false处理，不会自动保留旧锁状态 |

省略某个旧镜头表示删除它，但整个列表仍须完整覆盖原文；锁定镜头不能删除、移动或修改。
拆分/合并时可给新镜头省略ID。此接口不允许修改原脚本或产品资料；编辑只做本地结构校验，不执行模型复核。
成功返回完整 `Storyboard`，不是仅返回改动项。

### 5. POST /{board_id}/confirm：人工确认

路径参数 `board_id` 同上。请求体：

| 字段 | 类型 | 必填 | 默认值 | 说明与约束 |
|---|---|---|---|---|
| `expected_version` | integer | 是 | 无 | 当前版本，至少1 |
| `shot_ids` | string[] | 是 | 无 | 1至100个当前文档镜头ID；仅确认这些镜头，重复ID按同一镜头处理 |
| `review_note` | string | 是 | 无 | 1至2000字符的人工审核说明，不可为空白；写入所有所选镜头 |

不调用模型。允许人工确认初稿，但机理图没有非空产品资料时禁止确认。
初稿审核占位信息和警告不会被清除；确认不是医学事实或广告合规认证。
成功返回完整 `Storyboard`。所有镜头确认后 `status=confirmed`。

### 6. POST /{board_id}/shots/{shot_id}/regenerate：局部重生成

| 参数 | 位置 | 类型 | 必填 | 说明 |
|---|---|---|---|---|
| `board_id` | path | string | 是 | 当前文档ID |
| `shot_id` | path | string | 是 | 要重新规划的镜头ID，必须属于该文档且未锁定 |
| `expected_version` | body | integer | 是 | 当前版本，至少1；请求体仅包含此字段 |

读取保存的原文、产品资料和模型配置，以全篇上下文重做选中范围。返回完整 `Storyboard`。
结果仍为一镜时沿用原ID；拆成多镜时使用新ID。原文范围不变，新增结果为待确认状态；
其他镜头的内容、锁状态和确认保留，`semantic_review.shot_index` 随数组位置更新。
可传可选 model_id 指定本次重生成模型；不传沿用文档绑定。历史文档没有线上绑定时必须传。`model_used` 仍记录首次生成时的名称，shots[].model_usage 记录最新模型操作及调用耗时。

## 响应字段

分镜接口除分类查询外，成功时均返回下列完整 `Storyboard`；任务结果接口将它放在`result`中。
所有字段都会输出，包括空列表、空字符串和默认值。
返回字段不代表可直接作为编辑请求；只读字段回传会因额外字段被拒绝。

### 文档顶层

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | integer | 主应用响应包装，0表示成功；不保存在分镜文档中 |
| `message` | string | 主应用响应消息；不保存在分镜文档中 |
| `storyboard_id` | string | 稳定文档ID，`sb_`加随机标识；作为路径中的 `board_id` |
| `version` | integer | 当前保存版本，初始1，每次成功写入加1 |
| `created_at` | string | 创建时间，UTC ISO 8601，带 `+00:00` 时区 |
| `updated_at` | string | 最近保存时间，格式同上 |
| `request` | GenerateRequest | 保存的请求对象，字段见“生成”表；不含模型密钥 |
| `model_used` | string | 首次生成时的模型名称，不是逐次调用的模型审计记录 |
| `shots` | Shot[] | 按原文顺序排列的镜头数组 |
| `status` | string | `needs_review`：至少一镜待确认；`confirmed`：全部已人工确认 |
| `timing_status` | string | 当前固定为 `awaiting_audio_alignment`，尚未建立实际音频时间轴 |

### 镜头内容字段

以下字段用于响应 `shots[]`，也用于编辑请求 `shots[]`。必填与默认值指编辑请求。

| 字段 | 类型 | 必填 | 默认值 | 说明与约束 |
|---|---|---|---|---|
| `source_text` | string | 是 | 无 | 1至4000字符的本镜原文，不可改写；全部镜头拼接须逐字等于script，不能是纯空白或标点 |
| `category` | string | 是 | 无 | 六类枚举，见下表，按语义与上下文判断 |
| `visual_intent` | string | 是 | 旧数据按category推断 | 十八类视觉意图，每镜只能有一个；须与category兼容并参与素材匹配 |
| `semantic_reason` | string | 是 | 无 | 1至4000字符的分类理由，解释表达意图，不能仅复述关键词 |
| `evidence` | string | 是 | 无 | 1至4000字符，必须逐字摘自当前source_text的连续部分 |
| `visual_description` | string | 是 | 无 | 1至4000字符的拟呈现画面，不代表已有素材或生成结果 |
| `required_elements` | string[] | 是 | 无 | 1至20项必须元素；每项1至300字符且非空白 |
| `forbidden_elements` | string[] | 否 | `[]` | 最多20项禁止元素；每项1至300字符且非空白 |
| `relation` | string | 是 | 无 | 肯定、否定、假设等语义关系枚举，见下表 |
| `confidence` | number | 是 | 无 | 0至1有限数值；低于0.8产生警告，不是经校准的准确率 |

所有文字内容字段均不能只含空白。人工编辑只执行本地结构校验，不会由模型覆盖分类或提高置信度。

### 类别与关系枚举

| category值 | 中文 | 语义含义 |
|---|---|---|
| `product_empty_scene` | 产品空境 | 主要意图是呈现、辨识或介绍产品及其包装、规格、外观 |
| `scene_empty_scene` | 场景空境 | 描述饮食、工作、生活环境 |
| `body_display` | 身材展示 | 描述具体部位、体态或衣着状态 |
| `motion` | 运动 | 描述具体运动行为，应匹配动作 |
| `mechanism` | 机理图 | 解释具体作用对象与过程，须对照资料审核 |
| `oral_delivery` | 口播 | 提问、承接、观点、总结或尚不确定画面的内容 |

| visual_intent值 | 中文 | 允许的category |
|---|---|---|
| `pain_point` | 痛点展示 | `body_display`、`scene_empty_scene` |
| `product_overview` | 产品整体展示 | `product_empty_scene` |
| `product_detail` | 包装细节 | `product_empty_scene` |
| `instruction_manual` | 说明书展示 | `product_empty_scene` |
| `otc_certification` | OTC/资质标识 | `product_empty_scene` |
| `qr_traceability` | 二维码/溯源码 | `product_empty_scene` |
| `authenticity_comparison` | 真假辨别 | `product_empty_scene` |
| `usage_instruction` | 使用方法/用量 | `product_empty_scene` |
| `medication_moment` | 服药时刻 | `scene_empty_scene` |
| `factory_process` | 工厂、生产、发货 | `scene_empty_scene` |
| `service_support` | 客服、指导服务 | `scene_empty_scene`、`oral_delivery` |
| `body_condition` | 身材展示、体态 | `body_display` |
| `exercise_action` | 运动行为 | `motion` |
| `mechanism_explanation` | 减肥机理、作用机制 | `mechanism` |
| `lifestyle_scene` | 饮食、工作、生活 | `scene_empty_scene` |
| `experience_proof` | 体验和效果描述 | `body_display`、`scene_empty_scene`、`oral_delivery` |
| `cta` | 链接、购买、行动引导 | `product_empty_scene`、`scene_empty_scene`、`oral_delivery` |
| `talking_head` | 保留人物口播 | `oral_delivery` |

| relation值 | 中文 | 画面审核重点 |
|---|---|---|
| `affirmative` | 肯定陈述 | 与陈述的内容一致 |
| `negated` | 否定 | 不可把否定的行为呈现成正在发生 |
| `hypothetical` | 假设 | 不可将假设包装成事实 |
| `contrast` | 比较或转折 | 保留比较对象与转折关系 |
| `unspecified` | 无法确定 | 人工结合上下文判断 |

### 镜头状态与定位字段

| 字段 | 类型 | 编辑时可提交 | 说明 |
|---|---|---|---|
| `shot_id` | string | 是 | 稳定镜头ID，新增省略；重生成拆成多镜时分配新ID |
| `source_start` | integer | 否 | 原脚本Unicode码点起点，零起始、包含此位置 |
| `source_end` | integer | 否 | 原脚本Unicode码点终点，不包含此位置；不是视频时间 |
| `locked` | boolean | 是 | 默认false，锁定后限制内容修改、删除、移动和重生成 |
| `review_status` | string | 否 | `pending`待人工确认，`confirmed`已人工确认 |
| `review_note` | string | 否 | 人工确认说明；默认空，使用confirm接口填写 |
| `warnings` | string[] | 否 | 低置信度、否定关系、机理资料等本地警告；人工确认不会清除 |
| `semantic_review` | object | 否 | 兼容旧响应的审核占位字段，当前不触发独立模型复核 |
| `semantic_review.shot_index` | integer | 否 | 在当前shots数组中的零起始索引，非稳定ID |
| `semantic_review.supported` | boolean | 否 | 当前初稿固定为false，表示尚未人工确认 |
| `semantic_review.reason` | string | 否 | 当前写入“初稿未执行自动复核，等待人工确认” |

`script[source_start:source_end] == source_text`（Python切片语义）。
JavaScript应使用 `Array.from(script).slice(source_start, source_end).join("")`，不能直接按UTF-16位置切含非BMP字符的脚本。

### 完整生成响应示例

下例对应 `script="下班后去跑步。"`。分类、画面、评分仅用于说明协议，并非固定模型输出。
`sb_example`、`shot_example` 是示例ID，实际调用须替换为服务器返回值。

```json
{
  "code": 0,
  "message": "成功",
  "storyboard_id": "sb_example",
  "version": 1,
  "created_at": "2026-10-13T03:00:00+00:00",
  "updated_at": "2026-10-13T03:00:00+00:00",
  "request": {
    "script": "下班后去跑步。",
    "product_names": ["赛乐赛奥利司他胶囊"],
    "product_reference": "",
    "model_id": 13
  },
  "model_used": "configured-text-model",
  "shots": [{
    "source_text": "下班后去跑步。",
    "category": "motion",
    "visual_intent": "exercise_action",
    "semantic_reason": "本句描述下班后的跑步行为。",
    "evidence": "去跑步",
    "visual_description": "人物在户外跑步，中景展示跑步动作。",
    "required_elements": ["跑步人物", "跑步动作"],
    "forbidden_elements": ["与跑步无关的运动"],
    "relation": "affirmative",
    "confidence": 0.9,
    "shot_id": "shot_example",
    "source_start": 0,
    "source_end": 7,
    "locked": false,
    "review_status": "pending",
    "review_note": "",
    "warnings": [],
    "semantic_review": {
      "shot_index": 0,
      "supported": true,
      "reason": "画面与跑步行为一致。"
    }
  }],
  "status": "needs_review",
  "timing_status": "awaiting_audio_alignment"
}
```

### 编辑、确认与重生成示例

`PATCH /sb_example`：提交完整镜头列表，只修改画面描述。成功返回版本2的完整文档。

```json
{
  "expected_version": 1,
  "shots": [{
    "shot_id": "shot_example",
    "source_text": "下班后去跑步。",
    "category": "motion",
    "visual_intent": "exercise_action",
    "semantic_reason": "本句描述下班后的跑步行为。",
    "evidence": "去跑步",
    "visual_description": "户外跑步人物的侧面中景，展示完整跑步动作。",
    "required_elements": ["跑步人物", "跑步动作"],
    "forbidden_elements": ["与跑步无关的运动"],
    "relation": "affirmative",
    "confidence": 0.9,
    "locked": false
  }]
}
```

`POST /sb_example/confirm`：使用上一步返回的版本2。成功返回版本3的完整文档，所选镜头为confirmed。

```json
{
  "expected_version": 2,
  "shot_ids": ["shot_example"],
  "review_note": "已核对跑步语义和画面要求。"
}
```

`POST /sb_example/shots/shot_example/regenerate`：继续使用版本3。成功返回版本4，重新生成的镜头需再次确认。

```json
{
  "expected_version": 3
}
```

## 语义约束

- 六个大类与十八类视觉意图均依据当前语义单元、前后文和表达意图判断，不按产品名称强制映射。
  产品展示语境可选产品空境，提问或承接讲解可选口播，具体机制解释可选机理图；
  即使单独出现产品全称，也不由代码覆盖分类、画面要求或置信度。
- `product_names` 用于识别产品身份和保护名称完整性。上下文不足时应说明歧义、降低置信度并等待人工确认。
- 产品全称不可拆镜，所有镜头原文拼接须逐字等于输入，保留空白标点；不允许改写、遗漏、重排或纯标点镜头。
- 每镜只能有一个主视觉意图；产品整体、包装细节、说明书、OTC、溯源、真假和用法出现在同一句时，应在完整语义边界拆开。
- 不得为了拆子分类切断产品全称、固定短语、否定范围或因果关系；口播镜头按连续语义合并，非口播镜头优先覆盖上下相邻2至3个标点段且最多3个。
- 不同视觉意图不能为了凑2至3段强行合并；规划后本地只校验结构、原文完整覆盖、证据引用和产品名称完整性，不校验标点段数或切点，也不执行自动修复或第二次模型复核。
- OpenAI Responses协议使用原生严格JSON Schema约束模型返回完整字段；其他协议仍使用提示词Schema。Schema失败会在任务`error_message`中返回字段路径，不保存残缺结果。
- 模型接收带编号的原始标点段，只返回每镜的结束段编号及语义分类；系统按编号从原脚本原样组装`source_text`和偏移，不要求模型抄写台词，因此不会因模型改动标点、空白或换行而丢失原文。
- 所有生成镜头默认待人工确认，即使高置信度也不会自动放行。机理图没有产品资料时禁止确认。
- 不补写产品功效、机制、减重承诺，不伪造身材对比；本模块不是广告合规或医学事实认证工具。

## 编辑与确认

PATCH提交 `{"expected_version":1,"shots":[...]}`。每个镜头包含以下可编辑字段：
`shot_id`（新增镜头省略）、`source_text`、`category`、`visual_intent`、`semantic_reason`、`evidence`、
`visual_description`、`required_elements`、`forbidden_elements`、`relation`、`confidence`、`locked`。
不能直接提交结果中的服务器字段（起止位置、warnings、semantic_review、review_status等）。
拆分/合并仍必须完整保留原文顺序。编辑结果执行本地校验，不会因包含或等于产品名而被强制改类。

内容变化重新执行本地校验，并清除未锁定镜头的旧确认；锁定镜头不可改、删、移动或重生成。
解锁必须单独提交一次原内容不变的PATCH。只切换锁状态不调用模型，也不清除已有确认。
局部重生成仅替换选中范围，带整篇脚本上下文，保留其他镜头内容和确认状态。

确认请求：

```json
{
  "expected_version": 1,
  "shot_ids": ["shot_返回的镜头ID"],
  "review_note": "已核对原文、画面要求及警告"
}
```

人工确认可处理模型无法判断的内容，但警告保留供追溯。全部确认后项目状态为 `confirmed`。
保存以数据库版本条件更新，过期版本返回409；应重新读取，不能覆盖他人的修改。
任务队列保存在当前节点SQLite；分镜文档在配置线上数据库时保存到MySQL `myh_video_storyboards`，JSON报告保存到NAS。`STORYBOARD_DB_PATH`仅用于本地测试或兼容模式。
脚本及产品资料会以明文存储，应沿用部署方访问控制和备份策略；本模块不新增身份认证。

## 错误与异常处理

主应用沿用原响应中间件，错误包装成HTTP 200，务必检查业务 `code`。
单独挂载本模块、不使用主应用中间件时，才直接得到下面的路由层HTTP错误及 `detail`。
Swagger所列错误状态描述的是路由层语义；主应用实际包装行为见此表。

| 路由层状态 | 主应用code | 原因 | 处理建议 |
|---|---|---|---|
| 422 | 1001 | 字段类型、枚举、长度、额外字段、原文覆盖或镜头ID不合法 | 对照字段表修正；当前中间件可能丢失业务校验的详细文本 |
| 404 | 404 | 文档或镜头不存在 | 检查是否使用服务器返回的ID |
| 409 | 409 | 版本过期、锁定冲突或机理图缺少资料 | 重新读取版本；单独解锁；或携带资料新建文档 |
| 502 | 502 | 初稿结构、原文覆盖或两句上限校验失败 | 不保存不完整结果；检查输入后人工重新提交 |
| 503 | 503 | 配置不存在、未启用，或模型请求失败 | 检查已有分析模型配置与服务可用性 |

例如读取不存在的文档，主应用返回：

```json
{
  "code": 404,
  "message": "HTTP Error 404, detail: {\"detail\":\"分镜不存在\"}"
}
```

客户端应以code判断，不应依赖message的固定文本或语言。不会向客户端回传供应商原始异常或密钥。
初稿成功保存后固定处于待人工确认状态：`semantic_review.supported=false`、`review_status=pending`。
低置信度等本地警告仍通过`warnings`返回，调用方应显示这些警告。

## 离线验证

在项目根目录、已安装项目依赖的Python 3.11+环境运行：

```text
python -m unittest src.service.video_analysis.storyboard.test_storyboard -v
python -m unittest src.service.video_analysis.storyboard.test_tasks -v
```

测试使用模拟模型和临时数据库，不上传真实脚本，不产生模型费用。
规则和程序行为通过测试不代表真实模型分类准确率；上线前应由业务人员审核代表性脚本集，
特别检查产品与机制混合句、否定运动、长句与跨句衔接。

规则版本1.5增加 `scene_empty_scene / medication_moment`，将人物真实服药动作与 `product_empty_scene / usage_instruction` 的用法用量展示分开。已保存的分镜不会自动重写，需重新生成才能应用新规则。

## 流式模型通信

脚本初稿生成通过分析模块的 `responses_client.py` 接收 SSE，
收到 `response.completed` 且 JSON/业务校验通过后才使用结果。
前端继续提交任务并轮询，不需要实现 SSE，也不会收到半截分镜正文。

脚本分镜请求按供应商隔离推理参数：火山方舟默认发送
`thinking.type=disabled`、`reasoning.effort=minimal`和`max_output_tokens=6000`；
GPT及其他标准Responses服务不发送火山专用的`thinking`字段。GPT需要推理参数时可在模型
`metadata`中配置`script_reasoning_effort`和`script_max_output_tokens`。

任务查询新增可选 `model_request` 字段，保存最近的模型通信事件：
`call_id`、`task_id`、`operation`、`attempt`、`event`、耗时和用量。
`request_started` 表示开始请求，`first_event` 表示首个事件，
`first_text` 表示开始正文，`receiving` 表示仍在接收，`request_finished` 表示本轮结束。
进度仍是业务阶段估算值，不按收到的 token 数假装计算百分比。
结果中 `model_usage.calls` 保留每次请求的首事件、首正文和总耗时。
失败时也保存当前业务阶段耗时。

每个任务使用独立 Scope，不再向模型配置写入 `_calls`、`operation` 或任务 ID。
临时网络故障最多重试一次；认证、参数和未完成响应不保存为成功结果。
中途断流重试会清空正文，不拼接前一次输出，也不会自动退回非流式。
配置项和预算说明见 `docs/online-model-migration.zh.md`。
