# Hermes 飞书流式卡片补丁 — 中文说明

给 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的飞书（Feishu/Lark）频道
提供一个**按上游构建版本锁定、可安装/可回滚**的补丁：把一轮回复里散落的几十个气泡，
收敛成**一张会自己长大的交互卡片**。

> 完整技术说明见 [README.md](README.md)。本仓库只含代码、安装器、测试和通用文档，
> 不包含凭据、聊天内容、内网地址或部署日志。

## 效果

| 阶段 | 卡片上看到的内容 |
| --- | --- |
| 回复开始 | 立即出现卡片，顶部 `⏳ 思考中…` |
| 工具执行 | 顶部单行状态：`⏳ 已执行 N 个工具 · 最后：terminal · 12s` |
| 中途旁白 | 同状态区滚动显示最新一句，不再单独冒泡 |
| 正文生成 | 正文流式写入同一张卡片，状态切到 `✅ 已执行 N 个工具 · 生成回复中…` |
| 完成 | 状态区移除、正文定稿；`> ✅ 完成 · N 个工具`；`🧾 运行详情`；灰色页脚 |
| 静默回合 | 删除预热的空卡片，不留占位 |

## 功能

- **一轮一卡**：工具/段落边界不新建消息；流式 final 已投递时不再重复发送。
- **状态胶囊**：`Hermes · 运行中 / · 完成 / · 出错`，错误态红色 header。
- **运行详情**：流式一行灰色摘要；final 只有 ≥3 个工具或出现错误时才展开
  `主要工具 / ⚠️ 错误`，错误带实际原因。
- **Markdown 表格入卡**：管道表格自动转 `column_set` 或“表头 + 列表”。
- **超长回答续卡**：>8000 字拆成 `<标题> · 续 N/M` 的后续卡片。
- **临时诊断入卡**：供应商重试/运行重定向写进当前卡状态行，不再刷多条气泡。
- **final 兜底**：finalize 失败或回合失败时折进仍打开的卡片；错误正文用红色 header。
- **clarify 按钮**：单选/多选卡片；多选支持 ✅/○ 切换 + `✅ 提交（N）`。
- **确认类按钮**：`/new`、`/clear` 等显示 Approve Once / Always / Cancel。
- **可选 CardKit 2.0**：card entity + element 级更新 + 原生 table / collapsible_panel。
  默认关闭；客户端 ≥ 7.20 时可开 `FEISHU_CARDKIT_STREAMING=1`，失败自动回退经典卡。
- **可观测性**：`~/.hermes/logs/feishu_card_metrics.jsonl` + `tools/card_health.py`；
  只记录事件/错误码，不含聊天内容或凭据。

## 快速开始

前置：目标机器有 `~/.hermes/hermes-agent` 检出，已安装 `lark-oapi >= 1.6.8`，
飞书凭据在 `~/.hermes/.env`；目标 checkout 必须是 manifest 锁定 commit。

```bash
git clone <本仓库 URL> hermes-feishu-card-patch
cd hermes-feishu-card-patch

./install.sh --dry-run

# 按目标 Hermes 构建选择版本：
./install.sh                    # pinned ca069e1223
./install.sh --version bf0203a7 # Hermes v0.22.5
./install.sh --version ccc6956  # 旧 ccc69563 构建
```

安装器：备份原文件与 `config.yaml` → 应用 patch → 校验 10 个目标文件哈希 →
`py_compile` → 打开 `card_replies` 与飞书 runtime footer → 重启 gateway。

## 验证

```bash
./tools/verify_patch.py --check-dir ~/.hermes/hermes-agent
python3 tests/test_card_stream.py

cd ~/.hermes/hermes-agent
PYTHONPATH=. .venv/bin/python <repo>/tests/test_card_integration.py
PYTHONPATH=. .venv/bin/python <repo>/tools/live_card_probe.py   # 需要真实飞书凭据
```

当前：**29/29** 单元 + **24/24** 集成；实卡探针验证状态/详情/表格/页脚后撤回。

## 回滚

```bash
./uninstall.sh                # 恢复上一次安装前状态（最新备份）
./uninstall.sh --to-pristine  # 完全移除补丁，恢复到 git HEAD
```

## 目录结构

```text
install.sh / install.py          安装
uninstall.sh / uninstall.py      回滚
patches/<build>/                 按上游 commit 锁定的 patch + MANIFEST
work/ pristine/                  开发/再生成 patch 用的源码快照
tests/                           单元 + 集成测试
tools/                           校验、实卡探针、派生构建、指标、生成 patch
LICENSE                          MIT
```

## 已知限制

- 经典卡（默认）不能真正折叠详情、不能渲染原生 table；使用灰色摘要/`column_set` 近似。
- CardKit 2.0 需要飞书客户端 ≥ 7.20；旧客户端显示“请升级客户端”，因此默认关闭。
- 审批（exec approval）卡片保持上游原样；本补丁只改流式回复、clarify 与 slash-confirm。

## 安全与许可

仓库不含 `.env`、token、app secret、聊天内容或部署凭据；安装器只读取目标机器上的
`~/.hermes/.env`，不会复制进仓库。使用 [MIT License](LICENSE)。
