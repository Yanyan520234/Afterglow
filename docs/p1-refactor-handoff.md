# P1 一期重构 · 交接报告（S0-S3 验收通过）

> 最后更新：2026-08-13 · 由上一次执行 agent 交接
> 本文件是 P1 重构的**执行状态交接**，与 `docs/p1-refactor-plan.md`（计划书）配对。
> **下一个接手 P1 的 agent 必须先读：1) 本交给（现状+已验证）→ 2) 计划书（目标+不变量）→ 3) 下方「S6 接力指南」。**
> 验收结论见文末「七、验收记录」——S0-S3 全部可复现验证通过。

---

## 〇、TL;DR（给新 agent 的 30 秒摘要）

本仓库 `C:\Users\yang\clone bot\Afterglow` 正做 **P1 重构**：把 `chat.py` + `responses.py` 两条平行流水线收敛为「共享管道 `turn_service.py` + 薄适配层」，**行为零变化**。

- 已完成：**S0 基线清理**、**PR1 parity 安全网（test_protocol_parity.py，6 项）**、**PR2 turn_service.py 骨架**、**PR3 收敛（S3：chat.py + responses.py 已切换共享管道）**
- 下一步：**S6 合流**（全量+parity 绿 → 隐私守卫 → push myrepo `Yanyan520234/Afterglow` → 打 v0.3.7），随后进入一周观察期
- 当前分支：`refactor/turn-service`（含 PR1+PR2+PR3，工作树干净）
- **红线**：12 条不变量（计划书第 6 章）+ parity 套件全绿是唯一裁判；任何回归立即回退该步提交
- 遗留待办（不属 P1）：mypy 20 个既有错误（P5 处理）、隐私守卫脚本化、CI mypy；`DEBUG_PARITY_DIFF` 指纹钩子与 `scripts/parity_diff.py`（决策 8）留观察期做双实现对拍

---

## 一、仓库与分支现状（已核实）

| 项 | 状态 |
|---|---|
| 引擎路径 | `C:\Users\yang\clone bot\Afterglow` |
| 桥接路径 | `C:\Users\yang\clone bot\afterglow-onebot-bridge`（本轮未改动） |
| 引擎 remote | `myrepo=Yanyan520234/Afterglow`（公众 fork）；`origin=kldhsh123/Afterglow`（上游，只读） |
| 当前分支 | `refactor/turn-service` |
| 工作树 | 干净，无未提交/未跟踪文件 |
| tag | `legacy-pre-refactor`（指向 142cc79，即重构前的安全网状态） |

```
提交历史（refactor/turn-service）：
8fc7dca refactor(responses): responses.py 切换共享管道（增量3）  ← PR3 第三增量
a7bea03 refactor(chat): chat.py 切换共享 run_layer_a + decide_policy（增量2）
a671b0f refactor(chat): chat.py 切换共享 persona_card + messages（增量1）
00ab07f refactor(chat): 共享编排层纯搬迁          ← PR2
142cc79 test(chat): 协议一致性安全网（先行）       ← PR1（已合入 main + 打了 tag）
b5b2918 S0 基线清理: ruff 修复 + P1 计划书入库
2eab2af chore: 0.3.6 rebuild clean history
```

分支关系：
- `main` = 2eab2af → b5b2918 → 142cc79（**含 PR1，无 PR2/PR3**）
- `refactor/turn-service` = main + [PR2 + PR3×3]（**含 PR1 + PR2 + PR3**）
- 即：`git log main..refactor/turn-service` 为 `00ab07f`、`a671b0f`、`a7bea03`、`8fc7dca`（另加本文档提交）

> ⚠️ 提示：PR3 已完成于 `refactor/turn-service` 分支。**S6 合流时 PR2/PR3 一起快进/合入 main。**

---

## 二、已完成工作（S0-S2）

### S0 基线清理（commit b5b2918）

让仓库回到**ruff 全绿**的可执行基线，并录入 P1 计划书。

**改了 5 个源文件 + 删 1 个临时文件 + 加 1 个计划书：**
- `backend/xuwen/chat_api/routes/chat.py`：
  - I001 import 排序（`--fix`）
  - B905 `zip` 缺 `strict=`（131:45、159:54 两处，加 `strict=True`）
  - RUF006 `asyncio.create_task` 未存引用 → 加模块级 `_ACTIVE_BACKGROUND_TASKS: set[asyncio.Task[Any]]` 强引用容器 + `done_callback` 自动丢弃（chat.py:80-84 附近）
- `backend/tests/unit/test_douyin_chat_export_plugin.py`、`test_update_check.py`、`test_web_ui_inspect.py`：import 排序 / 未用导入（`--fix`）
- `backend/tests/unit/test_wechat_weflow_plugin.py`：B017 裸 `pytest.raises(Exception)` → `pytest.raises(ParseError)`（补了 `from xuwen.core.errors import ParseError` 导入）
- 删除过期基线记录 `backend/s0_baseline.txt`
- 新增 `docs/p1-refactor-plan.md`（重构计划书）

### PR1 · S1 协议一致案安全网（commit 142cc79，已合入 main）

**目标：在动任何代码前，先建立一个「行为零变化」的测试裁判。**

**改动 3 个文件：**
1. **`tests/integration/conftest.py`**（新）：共享 mock helper
   - `embedding_response(req)` — OpenAI 兼容 embedding 端点 mock（dim=8, `[0.1*(i+1)]*8`）
   - `NoopProactiveContextCache` — 主动上下文缓存空实现
2. **`tests/integration/test_chat_api.py`**：删除本地重复的 `_embedding_response` / `_NoopProactiveContextCache`，改为
   ```python
   from tests.integration.conftest import NoopProactiveContextCache, embedding_response
   _embedding_response = embedding_response      # 保持既有模块内名字
   _NoopProactiveContextCache = NoopProactiveContextCache
   ```
3. **`tests/integration/test_protocol_parity.py`**（新，6 项测试）：核心交付，见下

**parity 套件 6 项测试（都验证通过）：**
| 测试 | 断言内容 |
|---|---|
| `test_parity_triple_route_same_input_replies` | 同输入「今天有点累」→ chat/responses 都 200、回复文本一致、policy.should_reply 一致、trace_id 回显 |
| `test_parity_silence_short_circuits_to_sentinel` | 「别说话」→ chat `choices[0].finish_reason == "silenced"`、policy.should_reply=False；responses 短路（LLM 不被调用） |
| `test_parity_upstream_fingerprint_model_stage` | 发往 LLM 的 model 恒为 `.env` chat_model（忽略客户端字段）；system prompt 含稳定锚点「扮演/私聊/朋友/状态/回应」 |
| `test_parity_vlm_injection_format_differs` | VLM 注入格式差异（chat=`（对方发来一张图：…）` vs responses=`[图片X描述：…]`）断言保留 |
| `test_parity_retrieval_failure_semantics` | 检索失败：chat=5xx raise（实测 502/503/504 里），responses=降级 200 |
| `test_parity_side_effects_responses_store` | responses 路由写 responses_store、previous_response_id 可继承 conversation_id |

**parity 测试里发现的 3 个「已知非业务差异」（重要！重构时不可当回归）：**
1. **life `next_update_at` 时序噪声**：`LifeStateManager` 两次独立调用各自时钟算出不同时间 → 两路的 system prompt 里「下一自然状态更新时间」不同。**这是测试级时序，非业务差异，parity 不用全等比较 system 全串**，改用了「稳定锚点集」。
2. **VLM 注入格式**（chat 用 `（对方发来一张图）`，responses 用 `[图片X描述]`）——**是真实业务差异，必须保留**。
3. **检索失败语义**（chat raise / responses 降级）——**是真实业务差异，必须保留**。

### PR2 · S2 共享编排层纯搬迁（commit 00ab07f，未合 main）

**目标：创建 `turn_service.py` 骨架，迁入纯工具，路由不切换（re-export 兼容），行为零变化。**

**新增 `backend/xuwen/chat_api/turn_service.py`（120 行）：**
- `format_sse(payload)` — SSE data 帧打包（原 chat.py `_format_sse`：1344 同一实现）
- `compact_debug_text(text, limit)` — 调试文本压缩（原 chat.py `_compact_debug_text`：1372 同一实现）
- `NopTurnContext` — 编排 vs 无编排的统一外观（responses 用无编排占位）
- `begin_turn(state, *, caller_id, message_id, text, image_shas, image_urls) -> NopTurnContext` — 统一编排入口：chat 走 `state.turn_coordinator.begin_turn`，responses 返回 NopTurnContext（无编排）
- `_ACTIVE_BACKGROUND_TASKS: set[asyncio.Task[Any]]` — 强引用 set 防任务 GC

**关键设计决策（已闭环，PR3 必须遵守）：**
- **turn_coordinator 不搬实例**：仍在 `AppState`（state.py:50）持有，`turn_service` 通过 `state.turn_coordinator` 引用使用（避免双编排实例状态分裂）
- **url 导入零 sed 依赖**：turn_service 不 import FastAPI / StreamingResponse（保持零 HTTP 概念）

**变更影响：** 只新增 1 个文件，chat.py / responses.py **完全没动**（re-export 只是符号存在，路由尚未切换）。

### PR3 · S3 收敛（3 个增量提交 + 文档提交）

**目标：达成「共享管道 + 薄适配层」目标形态，chat/responses 变薄，守 12 条不变量，parity 全绿。**

**`turn_service.py`（120 → 379 行）共享管道 API：**
- `LayerA(retrieved, relationship_block, life)` — Layer A 并发产物
- `run_layer_a(state, *, route, retrieval_query, current_user_text, recent, conversation_id, trace_id, retrieval_fail_open=False, relationship_graceful_on_exception=False)`：
  - 检索 `wait_for` 包裹；`retrieval_fail_open=False`（chat）→ 超时抛 `RetrievalTimeout` / 失败透传 `RetrievalError`；`=True`（responses）→ 降级 `empty_retrieval_result` 继续，且关系记忆/life 照常并发计算（**与原 responses gather 语义一致，不丢并发产物**）
  - 关系记忆超时降级空串；`relationship_graceful_on_exception=True` 时非 Timeout 异常也降级（responses），False 时向上抛（chat 原语义）
  - life 超时沿用 snapshot；`route` 参数化 metric 前缀与 `trigger`
- `decide_policy(...) → (decision, policy_hint)` — 规则层 + 可选 LLM refine + 延迟/`policy_hint` 统一产
- `build_persona_card(..., include_schedule_hint=False)` — 仅 chat 传 True（不变量③）
- `build_messages(..., images=None)` — `build_chat_messages` + 视觉多模态组装

**适配层切换（每个增量以 parity/全量绿为验证关卡）：**
- **增量1（a671b0f）**：chat.py persona_card + messages → 共享方法；清理未用导入
- **增量2（a7bea03）**：chat.py Layer A → `run_layer_a(route="chat")`，`except RetrievalTimeout→504 / except RetrievalError→503`（不变量②）；决策段 → `decide_policy`；补 `RetrievalTimeout` import、清死代码
- **增量3（8fc7dca）**：responses.py → `run_layer_a(route="responses", retrieval_fail_open=True, relationship_graceful_on_exception=True)`（检索失败降级）、`decide_policy`、`build_persona_card`（不传 schedule_hint）、`build_messages(images=last_user_images)`；清理 9 个未用导入 + 1 个死常量；修测试耦合点 `FakeRetriever` 改用 `companion_prompt.empty_retrieval_result`

**行数收敛（`wc -l`）：** chat.py 1427 → 1182；responses.py 1181 → 963；turn_service.py → 379。

**决策 8（DEBUG_PARITY_DIFF）**：本轮仅计划在 env 开关下加 stage 指纹钩子；考虑到探测语义未定稿且非本轮收敛的必要条件，**留观察期**与 `scripts/parity_diff.py` 一并做（观察期一周零回归后立项，见计划书 §5）。

---

## 三、验证记录（本次验收实际执行，全部可复现）

| 检查 | 命令 | 结果 |
|---|---|---|
| ruff（xuwen+scripts+tests） | `python -m ruff check xuwen scripts tests` | **All checks passed** |
| parity 套件（6 项） | `pytest tests/integration/test_protocol_parity.py -v` | **6 passed** |
| unit 全量 | `pytest tests/unit -q` | **723 passed** |
| integration 全量 | `pytest tests/integration -q` | **88 passed** |
| turn_service 可导入 | `python -c "from xuwen.chat_api.turn_service import ..."` | OK |
| 工作树 | `git status --short` | 干净 |

> ⚠️ 注意：**mypy 不作为本轮验收关卡**。`python -m mypy xuwen` 现有 **20 个既有错误**（analysis/ingestion/web_ui 等 9 个文件），全与本次重构无关，记录在案归 **P5 工程标准**处理（计划书里有「CI 补 mypy xuwen」条目）。S0 决策：本次只要求**不新增** mypy 错误 + pytest 全绿。

---

## 四、12 条不变量速查（PR3 收敛时必须保持）

已逐一核实（含行号证据），抽取时**不得破坏**：

| # | 不变量 | 证据 |
|---|---|---|
| ① | LayerB（web search/fetch）仅 should_reply=True 后启动 | chat.py / responses.py 的 `_web_search_or_skip` 在 decision 后调用 |
| ② | 检索失败：chat=504/503 raise，responses=降级 | chat.py:324/338；responses.py:175/183 |
| ③ | `include_schedule_hint=True` 仅 chat | chat.py:593（responses 不传） |
| ④ | silence 流式：chat 无哨兵 / responses 有 | 两文件流式分支 |
| ⑤ | chat 6 处 `_ack_turn` | chat.py:540/752/796/1061/1212 + 流式内 |
| ⑥ | metric 前缀与 stage 参数化 | `life.decide` vs `responses.life.decide` 等 |
| ⑦ | VLM 预算：chat 逐张×张数 / responses 单超时 | chat.py:265-266（per_image_budget*len）vs responses.py:123（单 timeout） |
| ⑧ | proactive 活动参数差异 | chat 带 `req.caller_id`（chat.py:227-231），responses 仅 conversation_id（responses.py:140） |
| ⑨ | presence / frequency 仅 chat | chat.py:623-624（GenerationParams 带 presence_penalty/frequency_penalty） |
| ⑩ | responses_store 保留 | responses.py 全流程 |
| ⑪ | previous_response_id 继承在适配层 | responses.py:91-94 |
| ⑫ | companion 容器不经编排 | companion.py 独立 |

---

## 五、PR3 接力指南（已完成，本指南存档）

> ✅ **PR3（S3 收敛）已于 2026-08-13 完成**（见「二、已完成工作 → PR3」）。下列 5 步指引是执行时的方法论，历史有效；新 agent 已不需要从这里开始，去「S6 接力指南」（文末「七、验收记录」之后）。

### 当前状态
- 分支 `refactor/turn-service`，基于 main（含 PR1）+ PR2+PR3，工作树干净
- `turn_service.py` 已含共享管道 API（run_layer_a / decide_policy / build_persona_card / build_messages / LayerA）

### PR3 第一步（建议，续「增量切换」已拍板）
在 turn_service.py 实现**纯共享层**，让 chat.py 先切换：

```python
# 建议新增 API（对应计划书第 3 章）：
run_layer_a(state, *, route, retrieval_query, current_user_text, recent, trace_id)
    # Layer A：检索 / 关系记忆 / life 并发
    # 检索: 用 asyncio.wait_for 包 retriever.retrieve；超时/失败 NOT 吞，透传/raise RetrievalError
    #   → chat 适配层 catch → 504/503；responses 适配层 catch → 降级（决策 3）
    # relationship: 复用 _relationship_context_or_empty 逻辑（参数化前缀）
    # life: 复用 _life_in_parallel 逻辑（route 参数决定 trigger="chat"/"responses" + metric 前缀）

decide_policy(state, *, route, current_user_text, has_images, retrieved, life,
               relationship_context, recent, trace_id)
    # 复刻两条路由的 decide_response_policy + refine_decision_with_llm + build_policy_hint

build_persona_card(state, *, life, relationship_context, style_query, decision,
                   include_schedule_hint)
    # 复刻 build_persona_card_with_companion_context 调用（include_schedule_hint 参数化）

build_messages(state, *, persona_card, retrieved, recent, current_user_message,
               web_context, url_context, multimodal_content=None)
    # 复刻 build_chat_messages + 多模态 content 组装
```

### PR3 完成后的目标形态
- `chat.py` 1421 → ~300：只留适配层（turn_coordinator 编排 + _ack×6 + schedule 提取 + presence/frequency + 504/503 catch + SSE data: 格式）
- `responses.py` 1181 → ~250：只留适配层（previous_response_id + responses_store + 降级 catch + SSE event: 格式）
- `companion.py` 1135 **不动**（S5 再复用 policy_service）

### PR3 每步流程（纪律）
1. 实现 → ruff 检查（`python -m ruff check xuwen scripts tests`）
2. 跑 parity（`pytest tests/integration/test_protocol_parity.py`）→ 必须 6 绿
3. 跑全量（`pytest tests/unit tests/integration -q`）→ 全绿
4. 若某条不变量回归 → **立即回退该步提交，不掩盖**
5. 一个功能点一个 commit

### 已知坑（务必注意）
1. **chat 检索错误行为是"立即 raise"**，responses 是"降级继续"——收敛时检索在共享层必须「透传错误不吞」，由适配层 catch 区分（这是决策 3 的核心，别用回调塞进共享层）
2. **life next_update_at 时序噪声**：不要试图让两路的 system prompt 逐字节相等，parity 已改为锚点断言
3. **VLM 注入格式**：chat=`（对方发来一张图：…）`，responses=`[图片X描述：…]`——收敛成注入模板参数时**别统一成一种**
4. **VLM 预算**：chat 逐张×张数（max(5.0, vision_timeout)*len），responses 单 timeout——别归一化
5. **SSE 双线**：chat=`data:` + 收尾字段（schedule_tasks/send_image/need_owner）；responses=`event:` + `[DONE]`——**PR3 各保留，S4 才拆文件**
6. **集成测试环境**：`_settings()` 函数（test_protocol_parity.py）在 pytest 下 `xuwen_api_key=None`（conftest `_isolate_dotenv` 清环境变量）；**命令行直接运行会 401**（拿到真实 .env key），诊断脚本必须写成 pytest 测试或用 pytest 方式跑
7. **turn_coordinator**：别在 turn_service 里 new 一个实例，永远用 `state.turn_coordinator`（决策 2）

---

## 六、执行环境备忘

- venv：`backend\.venv`（Python 3.12）
- ruff：`backend\.venv\Scripts\python.exe -m ruff check xuwen scripts tests`
- pytest：`backend\.venv\Scripts\python.exe -m pytest tests/unit -q` 等
- 后端进程：已有运行实例（8000 端口，若需重启用 `--reload` 启动）
- **不要提交 `backend/.env`**（含真实 key）
- `.gitconfig` 有 `gh-proxy` 镜像重写，push 到 myrepo 前**注释掉 insteadOf**（临时改，推完还原）

---

## 七、验收记录（结论）

```
✅ ruff:            All checks passed（xuwen + scripts + tests）
✅ parity:          6/6 passed
✅ unit:            723 passed
✅ integration:     88 passed（含 test_chat_api 29 + test_protocol_parity 6 + 其余）
✅ 工作树:          干净
✅ tag legacy-pre-refactor: 已打（指向 142cc79）
✅ 分支结构:        main 含 PR1；refactor/turn-service 含 PR1+PR2+PR3；工作树干净
✅ 12 条不变量:     逐条核对保持（差异留在适配层 or 共享层参数化）
```

**S0 / PR1 / PR2 / PR3（S3 收敛）全部验收通过。**

## 八、S6 接力指南（下一步，从这里开始）

### 当前状态
- 分支 `refactor/turn-service`，含 PR1+PR2+PR3（chat/responses 已收敛为共享管道 + 薄适配层），工作树干净
- 下一步：**S6 合流**（计划书 §5）：全量 + parity 绿 → **隐私守卫** → **push myrepo `Yanyan520234/Afterglow`** → **打 v0.3.7**

### S6 步骤（纪律）
1. 最终验收：`python -m ruff check xuwen scripts tests` + `python -m pytest tests/unit tests/integration -q` 全绿
2. **隐私守卫**（S6 强制关卡）：`git ls-files` / `git grep` 检查 QQ 号、真实密钥、聊天记录原始文本零命中；`backend/.env` 不入库（`.gitignore` 已排除，commit 前 `git status` 再核对）
3. push 前 **临时注释 `.gitconfig` 的 `gh-proxy` insteadOf 重写**（推完还原）
4. push：`git push myrepo refactor/turn-service`
5. 打 tag：`git tag v0.3.7`（重构落地标记）+ push tag / 合 main
6. 观察期（一周真实运行零回归）后立项：S4（SSE 拆文件）、S5（companion 复用 policy_service）、DEBUG_PARITY_DIFF ↔ `legacy-pre-refactor` 只读对拍

### 已知坑（延续）
1. 检索错误：chat 立即 raise（504/503）/ responses 降级——共享层用 `retrieval_fail_open` 区分，**不要**把它统一掉
2. VLM 注入格式（chat=（…） vs responses=[图片X描述…]）与预算（逐张×张数 vs 单 timeout）仍在适配层，别归一化
3. SSE 双线（`data:`+收尾字段 vs `event:`+`[DONE]`）保留在适配层，S4 才拆文件
4. life `next_update_at` 时序噪声：parity 用稳定锚点断言，不逐字节全等
5. 集成测试环境：`_settings()` 在 pytest 下 `xuwen_api_key=None`；命令行直接跑会 401（拿真实 .env key），诊断脚本写成 pytest
6. `turn_coordinator` 永远引用 `state.turn_coordinator`，不在共享层 new 实例