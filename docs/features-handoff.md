# 功能交接：表情包 / 隐私静默 / 去 AI 味 / 语音识别（STT）

> 承接 P1 重构（main@4b8df96 / v0.3.7）。本批为四项新功能的实现与接线说明。
> 合并方：P1 之后的功能分支；观测期未完成（静默/表情包需真机验证）。

## 一、表情包（sticker）

### 已有能力（P1 已含，未动）
- 后端 `StickerStore`：`backend/.data/stickers/index.json` + `sha.ext` 图文件；AI 输出 `[sticker:名字]`，后端校验 + 注入 persona 图库清单（top 30）。
- 路由 `GET /v1/stickers`、`POST /v1/stickers`、`DELETE /v1/stickers/{name}`、`GET /v1/stickers/{name}/image`。

### 本次新增
- **桥接接线** `afterglow-onebot-bridge/bridge/sticker.py`（新）：`strip_sticker_tokens`（剥离 token + 空白归一化）、`load_sticker_index`、`sticker_data_url`（按 sha 读文件拼 data-url）。
- **桥接发送** `bridge/main.py`：分条循环时先文本后图；`[sticker:名字]` → `data:` 图 → `send_private_image`；找不到名字静默跳过（不报错）；`history_texts` 记录清洗后文本。
- **配置**：`STICKER_SEND_ENABLED`（默认 true）、`STICKER_DATA_DIR`（默认自动指向 sibling `Afterglow/backend/.data/stickers`）。

### 入库脚本（backend，venv 运行）
- `python scripts/import_stickers_from_history.py [--qq-data-root ...] [--describe] [--dry-run]`
  - 解析 `imports/*.json`，收集全部图片资源（**阳 + 对方**，默认 `--self-uin 2903132650` 判阳）。
  - 在 QQ 数据目录 `nt_data/Pic`、`nt_data/Emoji`（含 Ori 原图）按文件名（忽略大小写）定位原图。
  - 文件名去重 + 内容 sha 去重；name=QQ 文件名前 16 位；tags=`历史/阳|对方[/表情包]`；description=`阳/对方在聊天里发的表情包|图片`。
  - `--describe` 用 VISION_MODEL 逐张生成语义描述（更利于 AI 挑选，慢）。当前 642 张未 describe（通用描述）。
  - 运行结果：引用 3807 → 去重 672 文件名（阳=425 / 对方=247）→ 入库 642（sha 全唯一）。
- `python scripts/import_stickers.py <图片目录> [--description ...] [--tags 手动] [--owner shared] [--dry-run]`
  - 手动精选导入；name=文件名（非法字符去掉），重复 name/sha 自动跳过。

## 二、隐私请求 → 强制静默 + 提醒本人

- 后端 `chat_pipeline.detect_privacy_request(user_text)`：命中语音（发语音/听声音/打电话）、本人照片/自拍、隐私信息（身份证/住址/银行卡/手机号/密码/验证码）等请求 → 返回提醒文本列表。
- `chat.py` 命中即**规则层强制静默**：不调 LLM，直接返回 `content=[silent]`、`finish_reason=silenced`、`need_owner=privacy_hints`；写历史、回写 proactive context、记 metric `chat.silenced.privacy`。**不发任何可见消息**（不是 AI 婉拒）。
- 桥接：收到 `finish_reason=="silenced"` 或 content==`SILENCE_SENTINEL`（默认 `[silent]`）→ **不发送**；`need_owner` 已提前转发给本人（提醒文案通用化：`⚠️ 需你本人处理：{who}：{item}`）。
- 注意：**测试图已删**，阳图库（`C:/Users/yang/阳图库`）现为空；隐私提醒的「本人」配置仍为 OWNER_UID。

## 三、去 AI 味

- `config.py` `VISION_DESCRIBE_PROMPT` 改为大白话描述（不再"客观描述"）。
- `persona/prompt.py` `_STYLE_GUARD` 新增【说话方式硬约束】：不列点/不用"首先其次"、不暴露 AI 身份/客服腔、不总道歉、不空泛正能量收尾。
- `_render_history_images` 表头改为「聊天里出现过的图」（保留"不是真人原话"护栏）。
- 注入格式未变（chat 仍 `（对方发来一张图：…）`），parity 锚点词（扮演/私聊/朋友/状态/回应）与注入格式断言保持通过，**无 parity 改动**。

## 四、语音识别（STT）

- 后端：`stt_client.SttClient`（OpenAI 兼容 `audio/transcriptions`，重试 3 次）+ 路由 `POST /v1/audio/transcriptions`（multipart `file`，503=未配置 / 400=空或超大 / 502=转写失败）。
- `ChatCompletionRequest.voice_text`（可选）：非空时后端把最后一条 user 消息框成 `（对方发来一段语音：…）`（已有文本则追加），再走检索/LLM/写历史/隐私检测。
- 桥接：`_record_segments` + `resolve_record_audio`（`get_record` 本地直读 → URL 下载兜底）+ `transcribe_voice`；转写成功 → `chat(voice_text=…)`；**后端 503（未配置）→ 保持旧行为（跳过，不回复）**；配置了但失败 → 回 `VOICE_FALLBACK_TEXT`（默认「语音我这边听不清 发文字哈」）。
- **silk → wav 转码**：QQ 语音是腾讯私有 silk，云端 ASR（含 SenseVoice）都不收。桥接 `_silk_to_wav_bytes` 用 `silk-python`（模块名 `pysilk`）解码 → 线性插值重采样到 **16kHz 单声道** → 包成 wav 再上传。依赖：`pip install silk-python`（py3.12 win 有预编译 wheel，pilk 在 py3.12 无 wheel 且需 MSVC，故弃用）。
- 历史回写 user 记录追加 ` [语音]` 标记。
- 配置：后端 `.env` `STT_API_URL/STT_API_KEY/STT_MODEL/STT_TIMEOUT_SECONDS/STT_MAX_BYTES`；桥接 `.env` `VOICE_TRANSCRIBE_ENABLED/VOICE_FALLBACK_TEXT/VOICE_MAX_BYTES`。
- 当前真机配置（本工作副本 `backend/.env` 已写）：
  ```
  STT_API_URL=https://api.siliconflow.cn/v1
  STT_API_KEY=<复用 vision 的 SiliconFlow sk>
  STT_MODEL=FunAudioLLM/SenseVoiceSmall   # 免费、中文好、自带标点
  STT_TIMEOUT_SECONDS=60
  STT_MAX_BYTES=52428800                   # 硅基上限 50MB
  ```
- 已验证：`SttClient` 直调 SiliconFlow 200 返回文本；silk→wav 往返（16kHz mono，帧数精确）；桥接 ruff + 导入 OK。

## 五、行为变更记录（提交时需注明）

- 新增 4 个接口/字段：`POST /v1/audio/transcriptions`、`ChatCompletionRequest.voice_text`（其余 sticker 接口 P1 已有）。
- 静默哨兵：新增 `finish_reason="silenced"` + `content=[silent]` 语义（桥接已适配）。
- 隐私：规则层强制静默，命中不发任何消息（可能让"发语音/发照片"请求得不到回应——设计如此，见 P1 红线说明）。
- 642 张历史表情包已入库（未 commit，`.data/` 被 ignore；部署时同步 backend/ 全目录）。

## 六、验证

- 后端：`ruff check` 全绿；`pytest` 单测 77 通过（含 test_privacy_detect、test_stt）；集成 parity 6 + chat_api 29 + chat_image 5 通过。
- 桥接：`ruff check` 全绿；`import main` OK；STT 链路冒烟测试通过（临时脚本已删）。

## 七、部署提醒（真机）

1. 同步 `backend/`（含 `.data/stickers` 642 张 + `.data/images`）与 `bridge/` 到真机，重启后端 + 桥接。
2. 真机后端 `.env` 补 STT（`STT_API_URL/STT_API_KEY/STT_MODEL`，见上节）；桥接 `.env` 确认 `STICKER_DATA_DIR`、`VOICE_*`。
3. 真机桥接环境安装 `silk-python`：`pip install silk-python`（silk→wav 必需，缺了语音链路自动降级为"无法识别"）。
3. 验证清单：索图（「发张照片/表情包」→ 发图）；主动发图（聊到美食/日常）；语音消息回复；「发你语音/你照片发我」→ 完全静默 + 本人收到提醒；语气观察。
4. 观察期：若 AI 选图不准，用 `--describe` 补描述或手动 `import_stickers.py` 精选 + WebUI 改名/写描述。