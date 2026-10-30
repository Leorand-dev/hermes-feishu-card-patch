# hermes-feishu-card-patch

给 Hermes Agent 的飞书（Feishu/Lark）频道做一个**可安装、按上游构建版本锁定**的补丁：
把一轮回复里散落的一堆气泡，收敛成**一张有界、持续更新的交互卡片**——正文流式写入卡片，
工具调用进度与中途叙述折叠进紧凑的状态区，结束后同一张卡片只保留答案 + 灰色页脚统计，
状态区删除、不留空档。

> Presentation parity reference: [`Leorand-dev/openclaw-feishu-patch`](https://github.com/Leorand-dev/openclaw-feishu-patch)
> （OpenClaw 飞书插件的同款表现形式）。本仓库只做 Hermes 侧的移植与安装器，不包含任何凭据。
>
> 中文简明说明：[README.zh-CN.md](README.zh-CN.md)。

---

## 它解决什么

Hermes 上游的飞书适配器目前只有「逐条发送 + 逐条编辑」的 transport：

- 每个工具边界都会结束当前消息，下一段正文另起一条；
- 工具进度、旁白叙述各自 `send()` 成独立气泡；
- 一轮带十几个工具的长任务，聊天里会出现几十张「处理中…」卡片/气泡。

打过本补丁后，一轮对话在飞书里只有 **1 张卡片**：

| 阶段 | 卡片内容 |
| --- | --- |
| 回复开始（prewarm） | 立刻出现卡片：状态区 `⏳ 思考中…`（不再有 1–8 秒的空白等待） |
| 工具执行 | 状态区：`⏳ 已执行 N 个工具 · 最后：terminal · 12s`，15 秒心跳刷新耗时 |
| 中途旁白 | 状态区第二行滚动 ticker：`💬 {最新一句，≤80 字符}`（不再冒泡） |
| 正文开始 | 状态区切到 `✅ 已执行 N 个工具 · 生成回复中…`；正文流式写入同一个卡片 |
| 结束 | 同一张卡片：正文定稿、状态区删除（无空档）+ 灰色页脚 `model · context% · cache% · tok/s` |
| 静默/空回合 | 删除预热的空卡片，不留占位 |

同时保留的卡片生命周期约束（来自参考实现）：

- **一轮一卡**：工具/段落边界不新建消息；
- **卡片持有到 final**：中途不提前关闭、不重复建卡；
- **累积文本合并**：重复/乱序的块更新不会产生重复前缀；
- **final 去重**：流式 final 已投递时，网关的兜底 final 不再重复发送；
- **非流式 final 也渲染成完成态卡片**（streaming 关闭或媒体/错误兜底时仍是卡片表现形式）。

---

## 支持的上游构建

| item | value |
| --- | --- |
| upstream repo | `https://github.com/NousResearch/hermes-agent` |
| pinned commit (`HEAD`) | `ca069e12239a2c8d13887ac9f983557d00bdcfd2` |
| 补丁文件 | `patches/ca069e1223/hermes-feishu-card.patch` |
| 补丁 hunk 数 | 50 |
| 修改文件 | 8 个既有文件 + 2 个新文件（`card_stream.py`、`cardkit_v2.py`） |
| patch sha256 | `330eef8902549636505943b3da18913b034d05fff0b51e7254f2387677e33a17` |
| 已安装态（patched）文件哈希 | 见 `patches/ca069e1223/MANIFEST.json` |
| 测试 | 29 个卡片状态机单元测试 + 24 个适配器/流式消费者集成测试 |

**三台目标主机现在都使用 v0.22.5 构建**（Hermes
`bf0203a794a07aac3ff764972ab9087baa4e4f4e`），对应构建 `patches/bf0203a7/`
（10 文件、59 hunks，patch sha256
`b1b5af857387082acfac3d5abef05b8ac1e37a59991613be951410b27ce3875b`）。安装/校验/回滚时
加 `--version bf0203a7`；安装器仍会校验该构建的 HEAD 与逐文件哈希。

其它变体：`patches/ccc6956/` 是旧上游（`ccc69563`）的 41-hunk 构建；
`patches/ca069e1223/` 是旧的 pinned 构建（仍可用于同版本主机）。

安装器会校验 **HEAD commit + 每个目标文件的 SHA-256**：未知构建、已被其它手写补丁改过的文件
一律**拒绝**，绝不做模糊匹配（与参考仓库同一原则）。旧的 hand port 可用 `--force` 先备份再恢复
到 HEAD 后安装（见下文「替换旧的手写补丁」）。

---

## 依赖 / 前提

- Hermes Agent 飞书适配器可正常收发（`FEISHU_APP_ID` / `FEISHU_APP_SECRET` 已配置），
  gateway 以 user service 方式运行（`hermes-gateway.service`）。
- `lark-oapi >= 1.6.8`：卡片原地更新必须用 `PATCH /open-apis/im/v1/messages/:id`
  （`client.im.v1.message.patch`）；`PUT message.update` 只支持 text/post，对 interactive 返回
  `230001 invalid msg_type`。Hermes 的 `requirements` 已满足（宿主实测 1.6.8）。
- 飞书 app 权限：`im:message`、`im:message:send_as_bot`（发卡片与补丁所需；审批按钮等原有权限不变）。
- 建议开启流式：配置 `streaming.enabled: true`（默认 edit transport，0.8s 节流）。
  若关闭 streaming，补丁仍把**最终回复**渲染为一张完成态卡片（含页脚），但过程状态不再更新。

---

## 快速安装

```sh
git clone <this-repo> hermes-feishu-card-patch
cd hermes-feishu-card-patch

# 1) 检查（不改任何东西）
./install.sh --dry-run

# 2) 安装：校验 → 备份 → 打补丁 → 编译校验 → 激活配置 → 重启 gateway
./install.sh

# 其它上游构建（v0.22.5 / 旧 ccc69563）：
./install.sh --version bf0203a7

# 常用选项
./install.sh --no-restart        # 只落盘，稍后自己重启
./install.sh --skip-config       # 不动 config.yaml（需手动设置 card_replies）
./install.sh --no-footer         # 不在卡片里显示统计页脚
./install.sh --force             # 目标文件被旧补丁改过：先备份并 git restore 到 HEAD 再打
```

安装完成后 gateway 重启，飞书的下一轮回复即呈现「一轮一张卡片」。
安装器输出备份路径与回滚命令。退出码：`0` 成功、`2` 校验/版本拒绝、其它为执行失败。

### 配置激活（安装器自动完成，可手写）

```yaml
platforms:
  feishu:
    card_replies: true          # 启用流式卡片 transport
display:
  platforms:
    feishu:
      runtime_footer:
        enabled: true           # 卡片底部灰色统计 note（全局 display.runtime_footer.enabled 亦可）
        fields: [model, context_pct, cache, tok_s]
```

> `display.runtime_footer.enabled` 为全局开关；per-platform 段的 `enabled: true` 会覆盖它。
> 只有 footer 开启时才会写入卡片 note；字段缺失会自动跳过，不会显示 `?%`。

---

## 验证

```sh
./tools/verify_patch.py                                   # 补丁文件/manifest 完整性
./tools/verify_patch.py --check-dir ~/.hermes/hermes-agent # 已安装态哈希
./tools/verify_patch.py --check-pristine /path/to/clean    # 干净树哈希（回滚后）

# 单元测试（纯标准库；无需 pytest）
python3 tests/test_card_stream.py

# 适配器 + 流式消费者集成测试（需要 checkout 的 .venv；在宿主上运行）
cd ~/.hermes/hermes-agent
PYTHONPATH=. .venv/bin/python /path/to/repo/tests/test_card_integration.py

# 线上卡片联通性探针（真实凭据；发送→原地补丁→页脚→撤回，不留痕）
PYTHONPATH=. .venv/bin/python /path/to/repo/tools/live_card_probe.py
```

> 若 `.venv/bin/python` 是符号链接且在本机会被重新解析到 Hermes 自带的 tools
> interpreter（`sys.prefix` 变成 tools python、`PYTHONPATH` 被丢弃，导致 `ruamel`/
> `lark_oapi` 不可用），诊断脚本改用解析后的解释器并显式带上 venv site-packages：
> `PYTHONPATH="$PWD:$PWD/.venv/lib/python3.14/site-packages" ~/.hermes/tools/python-3.14.7+20260901-linux-x64/bin/python3 tools/live_card_probe.py`
> （gateway 自身不受影响；详见 VERIFICATION.md §7）。

`VERIFICATION.md` 记录本构建在目标主机上的实测结果（API、安装、单元/集成测试、
重启后 gateway 检查、线上探针），以及与参考实现的差异。

---

## 卡片布局与状态机

卡片是飞书经典 card JSON 1.0（`config.update_multi: true` 必须出现在每次发送/补丁中），
每帧整卡重建后 `PATCH`；元素：

```
# 流式阶段
markdown(status)                        # 顶部单行工作状态
markdown(body)                          # 答案正文（>4000 字按行拆成多个 markdown 元素）
note(🧾 运行详情 · …)                    # 紧凑灰色工具/错误计数行

# final 阶段（重新排版，避免信息拥挤）
markdown(body)                          # 纯答案正文
hr                                      # 分隔线
markdown(> ✅ 完成 · N 个工具 · [M 个错误])  # 完成摘要（不含耗时）
div(🧾 运行详情)                         # 仅 ≥3 个工具或有错误时显示：主要工具 / 错误
hr + note(footer)                       # 灰色页脚统计（耗时只在 footer latency 出现一次）
```

- header：`title = 机器人显示名 + 状态胶囊`（`· 运行中` / `· 完成` / `· 出错`）；
  工作态 `turquoise`，完成态 `blue`，错误态 `red`。
- 状态行只有**一行**：工具等待/`✅ 已执行…生成回复中…`/旁白 ticker/`✍️ 正在生成回复…`/
  `⏳ 思考中…` 互斥，不再叠加第二行旁白。
- 流式阶段的 `🧾 运行详情` 是一行灰色 note（工具计数/错误）；final 阶段只有 **≥3 个工具或有错误**
  才显示 `div` 字段块，内容只保留 `**主要工具**`（前 8 种 + `其他×N`）和 `**⚠️ 错误**`。不再重复
  工具总数和耗时；工具错误会带**实际错误原因**（工具 detail 的首行摘要，≤90 字）。
- final 正文后先 `hr` 再完成摘要 `> ✅ 完成 · N 个工具 · [M 个错误]`；只有工具的回合没有正文，
  摘要直接作为第一块。
- **Markdown 表格自动入卡**：正文里的连续 `| … |` 表格会转成 `column_set`（≤4 列）或
  “表头 + 列表”（更宽的表格），不再把 `|` 原文吐在卡片里；代码块内的表格不受影响。
- **超长答案续卡**：>8000 字时第一张卡显示卡片头和省略提示，剩余正文由 stream consumer
  拆成 `<title> · 续 N/M` 的**续卡**（每张 ≤6000 字），不再落到普通文本气泡。
- **临时诊断入卡**：供应商重试/恢复、运行重定向等 `DiagnosticText` 状态写入当前卡的状态行
  （`⏳ Provider temporarily unavailable — retrying…`），一次故障不再刷 4 条文本气泡；
  正常工具/旁白状态会清掉该提示。
- **clarify 多选按钮**：多选 clarify 卡支持 ✅/○ 切换 + `✅ 提交（N）`，提交时把所选标签
  以 JSON 数组回给 `clarify_gateway`（也保留“直接回复编号”文字路径）。
- **确认类卡片按钮**：`send_slash_confirm` 用 Approve Once / Always Approve / Cancel 三按钮
  （`/new`、`/clear` 等破坏性命令），点击调用 `tools.slash_confirm.resolve()`。
- **final 兜底入卡**：finalize 失败或回合失败时，普通 final send 会折进仍打开的 live card；
  错误正文（`⚠️`/`❌`）用红色 header，不产生“半成品卡 + 错误气泡”双份展示。
- **final 重试**：final 帧按 0 / 1.5 / 3 / 6s 重试（含 230020），失败时保留卡让 normal-final
  折叠/回收，不再先删卡再发文本。
- **耗时只出现一次**：footer 的 `latency` 字段；完成摘要和运行详情不再重复耗时，避免同一条卡片里
  出现 2–3 个相差 1 秒的时间。
- **推理内容完全不进卡片**（流式和 final 都不渲染），也不再保留相关捕获/常量。
- **可观测性**：卡片 PATCH/发送/限流/折叠/final 失败写入
  `~/.hermes/logs/feishu_card_metrics.jsonl`（无聊天内容/凭据），用 `tools/card_health.py`
  汇总；`tools/derive_build.py` 可在新上游提交上自动 worktree + patch/fuzz 派生变体。
- `config.summary` 用于聊天列表预览：正文摘要（≤50 字，去 markdown/代码，表格分隔行会被清掉），
  空回复回退 `✅ 已回复`。
- **版式限制**：参考实现用 CardKit 2.0 `collapsible_panel` 做可折叠详情；Hermes 这里走经典
  interactive card + 整卡 PATCH，实测该元素在飞书客户端会显示「请升级至最新版本客户端」占位，
  因此改为同内容的紧凑灰色 note（信息一致，但不可折叠）。要真正折叠需要迁移到 CardKit 2.0
  元素级 API（未包含在本补丁中）。
- 空闲卡片（静默/空回合）直接删除，不留占位。
- 长答案 > `CARD_JSON_MAX_BYTES`(28 KB) 时该帧自动降级为普通文本，避免飞书 30 KB 限制报错。
- 卡片 PATCH 有 1.0 s 硬下限（原先 0.25s 在 v0.22.5 上实测会撞飞书 230020 限流）；
  230020 会指数退避重试（1.2s/2.4s/4.8s，final 帧再补一次 3s 重试）而不是立即降级；
  工具事件突发会被合并，被延迟的状态变更在下一个 tick 补发；final 帧不受下限限制。
- v0.22.5 实时状态气泡折叠：卡片活跃时 routine status 和 diagnostic status 都折进卡片
  （诊断写在状态行，不再单独发警告气泡），长回合保持「一张卡」。
- final 帧若因限流彻底失败，会先重试再**删除半成品卡片**，避免「旧卡片 + 另一条完整文本」
  的双份展示。
- 页脚实测字段：`model · context_pct · cache · tok_s · latency`（latency = 回合耗时）。

| 状态 | 字符串（参考 2026.10.6 版式） |
| --- | --- |
| prewarm | `⏳ 思考中…` |
| 工具等待 | `⏳ 已执行 {N} 个工具 · 最后：{tool} · {12s/1m05s}`（无标签时 `，等待中…`） |
| 工具完成 | `✅ 已执行 {N} 个工具 · 生成回复中…` |
| 无工具生成中 | `✍️ 正在生成回复…` |
| 旁白 ticker | `💬 {最新一句，≤80}`（工具等待时让位于工具状态行） |
| 仅工具回合 final | 正文 = `> ✅ 完成 · {N} 个工具 · {elapsed}` |

---

## 替换旧的手写补丁（hand port）

若目标 checkout 的 8 个文件已被旧的手写卡片补丁/其它补丁改过（`git status` 可见），
安装器默认拒绝。使用：

```sh
./install.sh --force
```

它会先把这些文件的**当前内容 + `git diff`** 存到
`~/.hermes/backups/pre-hermes-feishu-card-<ts>/`，然后 `git restore --source=HEAD` 到锁定构建，
再打本补丁；`uninstall.sh --backup <dir>` 可完整还原到安装前状态。

## 回滚

```sh
./uninstall.sh                       # 还原到上一次安装前的状态（最近备份）
./uninstall.sh --to-pristine         # 彻底移除补丁：目标文件从 git HEAD 还原、删除新模块
./uninstall.sh --backup ~/.hermes/backups/pre-hermes-feishu-card-<timestamp>
```

回滚会恢复文件与 `config.yaml`、删除 `card_stream.py`、清理相关 `.pyc`、校验哈希，并（默认）重启 gateway。
多次重装后「最近备份」里是上一版**已打补丁**的文件；要回到未打补丁的上游构建请用 `--to-pristine`。
安装/回滚/`--force`/`--to-pristine` 路径都在一次性 clone 上做过完整回归（见 `AUDIT.md`）。

## 升级到新的 Hermes 构建

本补丁**不做模糊适配**。上游更新后：

1. 在干净 checkout 上 `git checkout <new-commit>`；
2. 用 `work/` 覆盖层（仓库内保留 patched 版本）与 `pristine/`（锁定版）重新生成：
   `python tools/make_patch.py --version <new-short-sha> --upstream-commit <full-sha>`；
3. 复核 diff（尤其 `adapter.py` 的 send/edit、`stream_consumer.py` 的 card transport、
   `run_turn_persistence.py` 的 footer 调用点），跑两套测试，再在 canary 主机验证。

## 仓库结构

```
README.md                      本文件
VERIFICATION.md                本构建的验证证据（宿主实测）
AUDIT.md                       第二轮审计：发现的问题、修复、残余风险
AGENTS.md                      给自动化 agent 的简明操作说明
install.py / install.sh        按构建锁定的安装器（校验/备份/打补丁/配置/重启）
uninstall.py / uninstall.sh    回滚
patches/
  ca069e1223/
    hermes-feishu-card.patch   49-hunk 统一 diff（pinned 旧构建）
    MANIFEST.json              upstream commit + 每文件前后 SHA-256
  ccc6956/
    hermes-feishu-card.patch   41-hunk 变体（旧上游 ccc69563）
    MANIFEST.json
  bf0203a7/
    hermes-feishu-card.patch   59-hunk 变体（v0.22.5，P0/P1/P2）
    MANIFEST.json
work/                          补丁后的完整文件（维护/再生成用）
pristine/                      锁定构建的原始文件（生成 diff 用）
tests/                         card_stream 单元测试 + 适配器/消费者集成测试
tools/
  make_patch.py                由 pristine/work 生成 patch+manifest
  make_patch_variant.py        由任意 pristine/patched 覆盖层生成其它上游构建的变体
  derive_build.py              新上游提交：worktree + patch/fuzz 自动派生变体
  make_sums.py                 重新生成 SHA256SUMS
  verify_patch.py              补丁/已安装态/干净态校验
  live_card_probe.py           真实飞书凭据的发送→补丁→页脚→撤回探针
  card_health.py               CardKit/卡片 PATCH 指标 JSONL 汇总
  chat_audit.py                飞书聊天卡片/气泡数量审计（before/after）
SHA256SUMS                     仓库内所有文件的 SHA-256
```

CardKit 2.0 模块：`work/plugins/platforms/feishu/cardkit_v2.py`（schema 2.0 构造器）；
传输在适配器里通过 `platforms.feishu.cardkit_streaming` / `FEISHU_CARDKIT_STREAMING=1`
开启（**默认关闭**：本机 Feishu 客户端对 2.0 卡仍显示“请升级客户端”占位，经典卡继续作为默认）。

---

## 与参考实现的差异（已知、已限定）

参考实现走飞书 **CardKit 2.0 streaming card**（element 级更新、可折叠 panel、
`streaming_mode`）。本补丁默认仍是**经典 card JSON 1.0 + 整卡 PATCH**（宿主客户端实测可用），
同时提供一个**可选的 CardKit 2.0 传输**（`cardkit_streaming` 开关）：

- 2.0 传输：创建 card entity → 发送 card_id → `card_element.content` 流式更新
  status/body → 关闭 streaming settings → final 全卡 swap（原生 `table` +
  `collapsible_panel` + 状态 `text_tag`）；
- 本机 Feishu 客户端对 2.0 卡仍显示“请升级客户端”占位，所以默认关闭；
  升级到 ≥ 7.20 后可用 `FEISHU_CARDKIT_STREAMING=1` 开启，失败自动落回经典卡；
- 经典卡上的已知差异：

- 可折叠 `collapsible_panel` 在经典卡片里被客户端渲染成“请升级客户端”占位，因此流式阶段用
  灰色 note、final 阶段用 **div 两列字段**呈现 `🧾 运行详情`（信息一致，只是不能展开/收起）；
- `config.summary` 已用于聊天列表预览；
- final 帧整卡重建时移除顶部状态元素（等价于参考的 DELETE status，无空档）；
- 编辑节奏沿用 Hermes 的 0.8s + 自适应 flood backoff（参考内部 160ms 节流不适用）；
- 推理内容不进入卡片（参考的可折叠思考面板在经典卡片里不可折叠，塞进灰色 note 会挤爆）；
- 工具进度的前提是 Hermes 侧 `tool_progress` 回调开启（飞书默认 `new`，无需额外配置）；
  `tool_progress: off` 时卡片仍工作，但状态区只剩 prewarm/生成状态。

---

## License

[MIT](LICENSE) © 2026 Hermes Feishu Card Patch contributors
