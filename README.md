# hermes-feishu-card-patch

给 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的飞书（Feishu/Lark）频道提供
一个**按上游构建版本锁定、可安装、可回滚**的补丁：把一轮回复里散落的气泡收敛成
**一张有界、持续更新的交互卡片**。

> 表现形式参考 [`Leorand-dev/openclaw-feishu-patch`](https://github.com/Leorand-dev/openclaw-feishu-patch)；
> 本仓库只包含 Hermes 侧移植、安装器、测试和通用文档，不包含任何凭据、聊天内容或部署日志。
> 中文简明说明见 [README.zh-CN.md](README.zh-CN.md)。

---

## 它解决什么

Hermes 上游飞书适配器采用“逐条发送 + 逐条编辑”：

- 每个工具边界结束当前消息，下一段正文另起一条；
- 工具进度、旁白各自变成独立气泡；
- 一轮长任务会在聊天里留下大量“处理中…”消息。

本补丁把一轮对话收敛成一张卡片：

| 阶段 | 卡片内容 |
| --- | --- |
| 回复开始 | 立即出现卡片，状态行 `⏳ 思考中…` |
| 工具执行 | 单行状态：`⏳ 已执行 N 个工具 · 最后：terminal · 12s` |
| 中途旁白 | 同状态区滚动最新一句（≤80 字符） |
| 正文生成 | 正文流式写入同一张卡片，状态切到 `✅ 已执行 N 个工具 · 生成回复中…` |
| 完成 | 删除状态区、正文定稿、`> ✅ 完成 · N 个工具`、结构化 `🧾 运行详情`、灰色页脚 |
| 静默/空回合 | 删除预热的空卡片，不留占位 |

## 功能

- **一轮一卡**：工具/段落边界不新建消息；流式 final 已投递时不再重复发送。
- **状态胶囊**：header 显示 `Hermes · 运行中 / · 完成 / · 出错`，错误态红色 header。
- **运行详情**：流式一行灰色摘要；final 只有 ≥3 个工具或出现错误时才展开
  `主要工具 / ⚠️ 错误`，错误带实际原因。
- **Markdown 表格入卡**：管道表格自动转 `column_set` 或“表头 + 列表”。
- **超长回答续卡**：>8000 字拆成 `<标题> · 续 N/M` 的后续卡片。
- **临时诊断入卡**：供应商重试/运行重定向写进当前卡状态行，而不是刷多条气泡。
- **final 兜底**：finalize 失败或回合失败时折进仍打开的卡片；错误正文用红色 header。
- **clarify 按钮**：单选/多选交互卡片；多选支持 ✅/○ 切换 + `✅ 提交（N）`。
- **确认类按钮**：`/new`、`/clear` 等显示 Approve Once / Always / Cancel。
- **可选 CardKit 2.0**：card entity + element 级更新 + 原生 table / collapsible_panel。
  默认关闭；客户端 ≥ 7.20 时可开 `FEISHU_CARDKIT_STREAMING=1`，失败自动回退经典卡。
- **可观测性**：`~/.hermes/logs/feishu_card_metrics.jsonl` + `tools/card_health.py`。
  只记录事件/错误码，不记录聊天内容或凭据。

## 前置条件

- 目标机器已安装 Hermes Agent，检出位于 `~/.hermes/hermes-agent`；
- `lark-oapi >= 1.6.8`（包含 `im.v1.message.patch`）；
- 飞书应用凭据已写入 `~/.hermes/.env`（`FEISHU_APP_ID` / `FEISHU_APP_SECRET`）；
- 目标 checkout 必须是补丁 manifest 中锁定的上游 commit；安装器会校验 HEAD 与逐文件 SHA-256。

## 安装

```bash
cd <本仓库>
./install.sh --dry-run          # 只检查

# 按目标 Hermes 构建选择版本：
./install.sh                    # pinned ca069e1223
./install.sh --version bf0203a7 # Hermes v0.22.5
./install.sh --version ccc6956  # 旧 ccc69563 构建
```

安装器会：备份原文件与 `config.yaml` → 应用 patch → 校验 10 个目标文件哈希 →
`py_compile` → 打开 `platforms.feishu.card_replies` 与飞书 runtime footer →
重启 gateway。`--skip-config` / `--no-footer` 可关闭配置改动。

## 验证

```bash
./tools/verify_patch.py --check-dir ~/.hermes/hermes-agent
python3 tests/test_card_stream.py

cd ~/.hermes/hermes-agent
PYTHONPATH=. .venv/bin/python <repo>/tests/test_card_integration.py
PYTHONPATH=. .venv/bin/python <repo>/tools/live_card_probe.py   # 需要真实飞书凭据
```

当前测试：**29/29** 单元 + **24/24** 集成。实卡探针会发送一张临时卡、验证状态/详情/表格/
页脚后撤回。

## 回滚

```bash
./uninstall.sh                # 恢复上一次安装前的状态（最新备份）
./uninstall.sh --to-pristine  # 完全移除补丁，恢复到 git HEAD
```

会恢复文件与 `config.yaml`、删除补丁新增模块、清理 `.pyc`、校验哈希并重启 gateway。

## 目录结构

```text
install.py / install.sh        安装
uninstall.py / uninstall.sh    回滚
patches/<build>/               按上游 commit 锁定的 patch + MANIFEST
work/                          补丁后文件（开发/再生成 patch 用）
pristine/                      锁定构建的原始文件（开发用）
tests/                         单元测试 + 适配器/消费者集成测试
tools/
  verify_patch.py              补丁/安装态/干净态校验
  live_card_probe.py           真实飞书实卡探针
  derive_build.py              新上游构建自动派生
  card_health.py               卡片指标汇总
  make_patch.py / make_patch_variant.py  生成 patch + manifest
LICENSE                        MIT
```

## 构建版本与升级

补丁按上游 commit 锁定。升级 Hermes 后：

1. 用 `tools/derive_build.py` 在新 checkout 上自动 worktree + patch/fuzz，生成变体；
2. 运行 `tools/verify_patch.py --version <new>` 与实际探针；
3. 用 `install.sh --version <new>` 安装。

冲突会在 `.rej` 文件中列出，需要人工解决后重新生成。

## 已知限制

- 经典卡（默认）不能真正折叠详情、不能渲染原生 table；使用灰色摘要/`column_set` 近似。
- CardKit 2.0 需要飞书客户端 ≥ 7.20；旧客户端会显示“请升级客户端”，因此默认关闭。
- 审批（exec approval）卡片保持上游原样；本补丁只改流式回复、clarify 与 slash-confirm。
- CardKit 2.0 额外需要飞书应用权限 `cardkit:card:write`。

## 安全

仓库不包含 `.env`、token、app secret、聊天内容或部署凭据。安装器只读取目标机器上的
`~/.hermes/.env` 连接飞书，不会把它复制进仓库。

## License

[MIT](LICENSE) © 2026 Hermes Feishu Card Patch contributors
