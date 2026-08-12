# P1 一期：chat / responses 收敛为「共享管道 + 薄适配层」重构计划书

> 状态：定稿待执行 · 最后更新：2026-08-12
> 入口：本计划书是 P1 重构的唯一执行依据，与 `HANDOFF.md` 顶部指向一致。
> 原则：满足「行为零变化」；12 条不变量守死；parity 套件是唯一裁判。

---

## 1. 目标

把两条平行流水线 `/v1/chat/completions`（chat.py）与 `/v1/responses`（responses.py）
收敛为「共享管道 + 薄适配层」，行为零变化，测试兜底。

- 共享层：`turn_service.py`（新），零 HTTP 概念，只抛类型化异常。
- 适配层：chat.py / responses.py 各自保留协议专属逻辑与表达，变薄。
- companion.py 本轮不动（S5 后续立项复用 policy_service）。

---

## 2. 基线事实（实测，`wc -l` 总行口径）

| 文件 | wc -l | 目标 |
|---|---|---|
| `chat_api/routes/chat.py` | 1421 | → ~300（适配层 A） |
| `chat_api/routes/responses.py` | 1181 | → ~250（适配层 B） |
| `chat_api/routes/companion.py` | 1135 | 不动 |
| `chat_api/turn_coordinator.py` | 203 | turn_service 吸收（引用式，不搬实例） |
| `chat_api/turn_service.py` | 新建 | ~600 |

> 行数口径说明：历史计划书曾以「非空行」（chat 1319 / responses 1103 / companion 1049）
> 作目标，与 `wc -l`（含空行）相差 102 / 78 / 86 行。**执行统一以 `wc -l` 为准**，
> 与 `git diff --stat` / PR 展示口径一致，才能对比「瘦身到 x 行」。

---

## 3. 目标架构

```
turn_service.py（共享管道，~600 行，零 HTTP 概念）
├── prepare_image_shas()          # 图像保存 → shas
├── describe_images(注入模板)     # VLM 描图；注入格式参数化
│                                #   chat:      （对方发来一张图：…）
│                                #   responses: [图片X描述：…]
├── run_layer_a()                 # 检索 / 关系记忆 / life 并发
│   ├── 空结果 → 返回空对象（不抛）
│   └── 超时 / 后端错误 → raise RetrievalError（类型化异常）
├── decide_policy()               # response_policy + LLM refine（policy_service 雏形）
├── build_persona_card()          # 独立方法，对齐 companion 复用
├── build_messages()              # 独立方法
└── complete(extract_schedule=False, …)  # LLM 生成 + sanitize + 协议块抽取

chat.py（1421 → ~300）—— 适配层 A
├── turn_coordinator 编排（begin×2 / update×1 / _ack×6；就地持有 AppState 引用）
├── include_schedule_hint + schedule_tasks 提取（透传开关）
├── presence / frequency 参数（仅 chat）
├── catch RetrievalError → 504/503 raise HTTPException
└── SSE 流式（data: 格式 + 收尾 schedule_tasks / send_image / need_owner）

responses.py（1181 → ~250）—— 适配层 B
├── previous_response_id 继承（LRU）
├── responses_store 写回
├── catch RetrievalError → 降级空结果继续
└── SSE 流式（event: 格式 + _new_event_formatter + [DONE]）

companion.py（1135，不动）
```

---

## 4. 已闭合决策（10 项）

| # | 决策 | 说明 |
|---|---|---|
| 1 | 行数口径 = `wc -l` 总行 | 与 git diff/PR 一致，可对比瘦身 |
| 2 | turn_coordinator：AppState 原位持有 + 引用式吸收 | state.turn_coordinator 对外零变化，避免双编排实例状态分裂 |
| 3 | 检索错误：共享层抛 `RetrievalError`，空结果不抛 | chat 适配层 catch→504/503；responses 适配层 catch→降级 |
| 4 | parity 升级 | messages 全量指纹 + 副作用断言 + 异常注入矩阵 |
| 5 | S1（parity 安全网）单独先行 | 先有安全网再动代码；合后打 tag |
| 6 | S4/S5 本轮不做 | 观察期后单独立项 |
| 7 | v0.3.7 里程碑 | S6 合流 + 隐私守卫通过后打（重构落地标记） |
| 8 | DEBUG_PARITY_DIFF | 本轮只加 env 开关的 stage 指纹钩子；观察期做双实现对拍 |
| 9 | mock helper → conftest | PR1 前置步骤，提取共享 fixture |
| 10 | 4 项优化点 | coordinator 引用式 / parity 钩子 / schedule 透传 / build_persona_card+build_messages 两段式 |

---

## 5. 阶段与 PR

| 步骤 | 内容 | 验证关卡 | 提交信息 |
|---|---|---|---|
| S0 | 基线：ruff + mypy + 全量 pytest 全绿；开分支 `refactor/turn-service`；盘点 turn_coordinator | 全绿 | （No commit） |
| PR1 | mock helper → conftest；新增 `tests/integration/test_protocol_parity.py`：①三路由同输入→回复文本/`policy.should_reply·reply_mode`/trace_id 一致 ②silence 一致性 ③上游 messages 全量指纹 ④副作用断言（writeback/metrics/responses_store）⑤异常注入矩阵（LLM 500·超时·embedding失败 × chat+responses，chat=504/503 vs responses=降级）⑥参数化裸测（VLM 注入格式 / 检索失败两语义） | 新测试绿 | `test(chat): 协议一致性安全网（先行）` |
| — | PR1 合后打 tag `legacy-pre-refactor` | — | ✅ done |
| PR2 | S2 纯搬迁：turn_service 骨架 + 路由 re-export 保兼容；turn_coordinator 按决策 2 引用式吸收 | 全量 0 回归 | `refactor(chat): 共享编排层纯搬迁` |
| PR3 | S3 收敛：按定稿 API 实现；"空结果不抛/错误抛 RetrievalError"；chat/responses 变薄；守 12 条不变量；修正测试耦合点 | 全量 + parity 绿 | `refactor(chat): 收敛共享管道+适配层` ✅ done（a671b0f / a7bea03 / 8fc7dca 增量） |
| S6 | 全量 + parity 绿 → 隐私守卫 → push myrepo → 打 v0.3.7 | 全绿+守卫 | ✅ done（main@5776e9d + v0.3.7 + ff 合入 main） |

观察期（一周真实运行，零回归）→ 后续立项：
- S4：SSE 拆 `sse_chat.py` / `sse_responses.py`
- S5：companion 复用 policy_service
- `scripts/parity_diff.py`：`DEBUG_PARITY_DIFF=1` 时打印 stage 指纹，与 `legacy-pre-refactor` tag 旧实现只读对拍，不跑双实现

---

## 6. 12 条不变量（逐条，含行号证据已核实）

| # | 不变量 | 现状证据 |
|---|---|---|
| ① | LayerB（web search/fetch）仅 should_reply=True 后启动 | chat.py / responses.py 均确认 |
| ② | 检索失败：chat=504/503，responses=降级 | chat.py:324/338 raise；responses.py:175/183 降级 |
| ③ | include_schedule_hint 仅 chat | chat.py:587；responses 无 |
| ④ | silence 流式：chat 无哨兵 / responses 有 | 两文件流式分支确认 |
| ⑤ | chat 6 处 _ack_turn | chat.py:540/752/796/1061/1212 + 流式内 |
| ⑥ | metric 前缀与 stage 参数化 | 两文件各阶段确认 |
| ⑦ | VLM 预算：chat 逐张×张数 / responses 单超时 | chat.py:265-266；responses.py:123（**须保留差异**） |
| ⑧ | proactive 活动参数差异 | 两文件调用差异确认 |
| ⑨ | presence / frequency 仅 chat | chat.py:623-624 |
| ⑩ | responses_store 保留 | responses.py 全流程 |
| ⑪ | previous_response_id 继承在适配层 | responses.py:91-94 |
| ⑫ | companion 容器不经编排 | companion.py 独立 |

S3 若某条回归 → **立即回退该步提交，不掩盖**。

---

## 7. 交接清单 / 完成标记

- [x] PR1（parity 安全网）单提交 `142cc79`，已打 tag `legacy-pre-refactor`
- [x] PR2（turn_service 骨架）单提交 `00ab07f`
- [x] PR3（S3 收敛）3 个增量提交 `a671b0f` / `a7bea03` / `8fc7dca`（增量切换已拍板）+ 文档提交
- [x] S6 合流：隐私守卫通过 → push myrepo（分支+tag+main）→ 打 v0.3.7 → ff-only 合入 main（main@5776e9d）
- [ ] 观察期一周零回归后：S4（SSE 拆文件）/ S5（companion 复用 policy_service）/ DEBUG_PARITY_DIFF ↔ `legacy-pre-refactor` 对拍立项
- 每步提交遵循 Conventional Commits，且 ruff + mypy + pytest/parity 全绿才合
- PR1/PR2/PR3 提交可独立 cherry-pick / revert
- S6 隐私守卫已执行（QQ 号 / 密钥 / 聊天导入零命中；backend/.env gitignore 排除）
- 完成后更新本节状态，交接给下一个执行者

---

## 8. 未纳入本轮（防范围蔓延）

- ❌ SSE 双线抽象成一套（协议语义不同，PR3 各自保留，S4 才拆文件）
- ❌ 共享数据类迁到 `core/models.py`（chat_api 专属，收在 turn_service 内）
- ❌ 更细粒度分批 PR（PR1/PR2/PR3 已足够）
- ❌ P4/P5 工程标准（隐私守卫脚本化 + pre-commit + CI mypy + 覆盖率）：单独立项