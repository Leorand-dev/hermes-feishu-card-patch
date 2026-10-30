# Hermes 飞书流式卡片补丁 — 中文说明

给 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的飞书（Feishu/Lark）频道
提供一个**按上游构建版本锁定、可安装/可回滚**的补丁：把一轮回复里散落的几十个气泡，
收敛成**一张会自己长大的交互卡片**。

> 表现形式参考 [`Leorand-dev/openclaw-feishu-patch`](https://github.com/Leorand-dev/openclaw-feishu-patch)，
> 本仓库只做 Hermes 侧移植、安装器和验证工具，不含飞书凭据、聊天内容或任何密钥。
> 更完整的实现/审计记录见 [README.md](README.md)。

---

## 它解决什么问题

Hermes 上游的飞书适配器是“逐条发送 + 逐条编辑”：

- 每个工具边界会结束当前消息，下一段正文另起一条；
- 工具进度、旁白各自变成独立气泡；
- 一轮长任务会在聊天里留下几十条“处理中…”消息。

打上本补丁后，**一轮对话只保留一张卡片**：

| 阶段 | 卡片上看到的内容 |
| --- | --- |
| 回复开始 | 立即出现卡片，顶部 `⏳ 思考中…` |
| 工具执行 | 顶部单行状态：`⏳ 已执行 N 个工具 · 最后：terminal · 12s` |
| 中途旁白 | 同状态区滚动显示最新一句（不再单独冒泡） |
| 正文生成 | 正文流式写入同一张卡片，状态切到 `✅ 已执行 N 个工具 · 生成回复中…` |
| 完成 | 状态区移除、正文定稿；`> ✅ 完成 · N 个工具`；结构化 `🧾 运行详情`；灰色页脚统计 |
| 静默回合 | 删除预热的空卡片，不留占位 |

## 主要功能

- **一轮一卡**：工具/段落边界不新建消息；流式 final 已投递时不再重复发最终消息。
- **状态行 + 状态胶囊**：header 显示 `Hermes · 运行中 / · 完成 / · 出错`，出错时红色 header。
- **紧凑运行详情**：流式阶段一行灰色摘要；final 只有 ≥3 个工具或出现错误时才展开
  `主要工具 / ⚠️ 错误`，工具错误会带实际原因。
- **Markdown 表格入卡**：管道表格自动转成 `column_set` 或“表头 + 列表”，不再显示原始 `|`。
- **超长回答续卡**：>8000 字拆成 `<标题> · 续 N/M` 的后续卡片，不落到卡片外。
- **临时诊断入卡**：供应商重试/运行重定向写进当前卡状态行，不再一次故障刷 4 条气泡。
- **final 兜底**：finalize 失败或回合失败时折进仍打开的卡片；错误正文用红色 header，不会出现
  “半成品卡 + 错误气泡”。
- **clarify 按钮**：单选/多选都走交互卡片；多选支持 ✅/○ 切换 + `✅ 提交（N）`。
- **确认类按钮**：`/new`、`/clear` 等破坏性命令显示 Approve Once / Always / Cancel。
- **可选 CardKit 2.0**：card entity + element 级更新 + 原生 table / collapsible_panel。
  **默认关闭**：较旧的飞书客户端会把 2.0 卡显示成“请升级客户端”；客户端 ≥ 7.20 后可用
  `FEISHU_CARDKIT_STREAMING=1` 开启，失败自动回退经典卡。
- **可观测性**：`~/.hermes/logs/feishu_card_metrics.jsonl` 记录发送/补丁/限流/final 失败等事件，
  `tools/card_health.py` 汇总；不记录聊天内容或凭据。

## 快速开始

前置：目标机器上有 `~/.hermes/hermes-agent` 检出，并已安装 `lark-oapi >= 1.6.8`。
安装器会校验上游 commit 与每个目标文件的 SHA-256，**不要把它应用到未知构建**。

```bash
git clone <本仓库 URL> hermes-feishu-card-patch
cd hermes-feishu-card-patch

# 只检查，不改文件
./install.sh --dry-run

# 按宿主构建选择版本：
./install.sh                      # pinned ca069e1223
./install.sh --version bf0203a7   # Hermes v0.22.5（host-a/host-b/host-c 当前构建）
./install.sh --version ccc6956    # 旧 host-c ccc69563 构建
```

安装器会：备份原文件与 `config.yaml` → 应用 patch → 校验 10 个目标文件哈希 →
`py_compile` → 打开 `platforms.feishu.card_replies` 与飞书 runtime footer → 重启 gateway。

## 验证

```bash
./tools/verify_patch.py --check-dir ~/.hermes/hermes-agent
python3 tests/test_card_stream.py

cd ~/.hermes/hermes-agent
PYTHONPATH=. .venv/bin/python <repo>/tests/test_card_integration.py
PYTHONPATH=. .venv/bin/python <repo>/tools/live_card_probe.py   # 需要真实飞书凭据
```

预期：`RESULT: OK`、`29/29 tests passed`、`24/24 integration tests passed`；
实卡探针输出 `mid details/status: OK`、`final summary/details: OK` 并撤回探针卡。

## 回滚

```bash
./uninstall.sh                # 恢复上一次安装前的状态（最新备份）
./uninstall.sh --to-pristine  # 完全移除补丁，恢复到 git HEAD
```

会恢复文件与 `config.yaml`、删除补丁新增模块、清理 `.pyc`、校验哈希并重启 gateway。

## 目录结构

```text
install.sh / uninstall.py / install.py   安装、回滚
patches/                                  按上游构建锁定的 patch + MANIFEST
work/                                     补丁后的文件（维护/再生成用）
pristine/                                 锁定构建的原始文件
tests/                                    单元 + 集成测试
tools/
  verify_patch.py                         补丁/安装态/干净态校验
  live_card_probe.py                      真实飞书实卡探针
  derive_build.py                         新上游构建自动派生
  card_health.py                          卡片指标汇总
  make_patch.py / make_patch_variant.py   生成 patch + manifest
README.md                                 完整技术说明与审计记录
```

## 已知限制

- 补丁按上游构建锁定；换 Hermes 版本需先用 `tools/derive_build.py` 派生并验证。
- 经典卡（默认）不能真正折叠详情、不能渲染原生 table；用紧凑灰色行/`column_set` 近似。
- 飞书客户端 < 7.20 显示 CardKit 2.0 卡为升级占位，因此 2.0 传输默认关闭。
- 审批（exec approval）卡片保持上游原样，本补丁只改流式回复、clarify 与 slash-confirm。
- 需要飞书应用权限：发送消息 + `im:message` 相关；CardKit 2.0 额外需要
  `cardkit:card:write`。

## 安全

仓库不包含 `.env`、token、app secret 或聊天内容；安装器只读取目标机器上的
`~/.hermes/.env` 来连接飞书，不会把它复制进仓库。公开仓库使用 MIT License。

## License

[MIT](LICENSE) © 2026 Hermes Feishu Card Patch contributors
