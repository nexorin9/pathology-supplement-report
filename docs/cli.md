# 运维命令参考

`README.md` 里给的是速查表；本文件是全量参考（参数、默认值、退出码、产物位置）。
下面每段 `--help` 输出都是从产品代码里跑出来的真实文本。

```bash
# 总入口
python3 -m patho_supplement.cli --help

# 装成命令后也可以直接用
patho-supplement --help
```

## 子命令一览

| 子命令 | 用途 | 主要产物 |
|--------|------|----------|
| `daemon` | 常驻运行，盯着结果投递目录，回报到达即处理并回写病理系统 | 草稿文件 + 待处理清单 + 状态库 |
| `replay` | 批处理重放历史回报（对账、补跑用，不触碰真实草稿区） | 草稿文件 + 待处理清单 + `summary.json` |
| `doctor` | 自检：凭据位、对外接口清单、出站连通性、生效阈值 | 终端报告（退出码即结论） |
| `pending list` | 列出待人工处理的挂起项 | 终端表格 |
| `pending resolve` | 补齐缺失字段并从挂起的那一步续跑 | 新一版草稿 + 状态库 |
| `pending export` | 导出可带走的待人工处理清单（CSV + 文本） | `pending_list.csv` + `pending_list.txt` |
| `audit export` | 导出某病理号的审计流水（复核、内审、飞检核对用） | `audit_trail.csv` + `audit_trail.txt` |
| `benchmark` | 基准集跑批 + 三方对照 + 规模与性能验收 | `metrics.json` / `comparison.json` / `scale.json` |
| `summary` | 打印某一轮跑了多少（从产物目录或状态库读） | 终端报告 / `--json` 输出 |

## 退出码

| 退出码 | 含义 |
|--------|------|
| `0` | 成功 |
| `1` | 失败：凭据缺失、状态库不存在、接口不可达、指标不达标、阈值配错 |
| `2` | 参数用错：子命令缺参数、补齐字段名不认识、补齐值格式不对 |
| `3` | 补齐后仍对不上，挂起项保持 pending（等人再补） |
| `4` | 挂起行正被另一个人处理中（已登记的补齐值不丢，可重试） |
| `5` | 已有另一个守护进程在运行（锁文件被占） |

## cli replay

批处理重放：把结果目录里的历史回报跑成草稿与待人工处理清单。**不触碰真实草稿区**，
适合对账、补跑与演练。

```bash
python3 -m patho_supplement.cli replay --inbox tests/fixtures/inbox/ --out ./out
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--inbox` | 必填 | 结果投递目录 |
| `--baseline` | 目录下的 `baseline.json` | 病理报告基线快照；不给则从病理系统实时取（需凭据） |
| `--out` | `data/replay` | 产物输出目录 |
| `--state-db` | 无 | 给了则跨批次去重（守护进程用的就是这种模式） |
| `--lexicon` | `$PATHOLOGY_LEXICON_PATH` | 部位同义词典文件；缺省用内置词表 |

## cli daemon

常驻运行：结果回报一落盘就处理并回写病理系统草稿区。

```bash
python3 -m patho_supplement.cli daemon
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--inbox` | `$PATHOLOGY_RESULT_INBOX` | 结果投递目录 |
| `--state-db` | `$PATHOLOGY_STATE_DB` | 状态库路径 |
| `--out` | `data/daemon` | 产物输出目录 |
| `--lock-file` | 状态库同级 | 单实例锁文件路径；第二个实例启动即退出（码 `5`） |
| `--fallback-interval` | `$WATCH_FALLBACK_INTERVAL` | 定时兜底扫描间隔（秒） |
| `--once` | — | 只跑一轮就退出（演练与 CI 用） |
| `--cycles` | 一直跑到收到停止信号 | 最多跑几轮 |
| `--log-level` | `$LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `--log-file` | `$LOG_FILE` | 行日志落盘文件；不配置则写标准错误 |

## cli doctor

自检：打印凭据位、5 条对外接口清单、出站连通性与全部生效阈值。
**只回显「已配置 / 未配置」，不回显凭据值。** 缺必需凭据时以退出码 `1` 结束并列出缺失项。

```bash
python3 -m patho_supplement.cli doctor --accession ACC-EXAMPLE-0001
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--accession` | 无 | 指定病理号则真取一次基线，验证出站接口可用 |

## cli pending list

列出待人工处理的挂起项：行 ID、病理号、项目、原因、需补齐字段、候选部位、尝试次数、快照时间、状态。

```bash
python3 -m patho_supplement.cli pending list
python3 -m patho_supplement.cli pending list --accession ACC-EXAMPLE-0001
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--state-db` | `$PATHOLOGY_STATE_DB` | 状态库路径 |
| `--accession` | 无 | 只看某一个病理号 |

## cli pending resolve

补齐缺失字段并从挂起的那一步续跑。**只接受「挂起行 ID + 缺失字段值」**，不接受整条结果重投；
命中则并入草稿新一版，旧版本链保留可回查。

```bash
python3 -m patho_supplement.cli pending resolve --id 1 --set block_no=B1
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--id` | 必填 | 挂起行 ID（`pending list` 里那一行的编号） |
| `--set` | 无 | 补齐的字段与值，形如 `字段名=值`，可重复给出；只接受缺失字段 |
| `--state-db` | `$PATHOLOGY_STATE_DB` | 状态库路径 |
| `--baseline` | 从病理系统实时取 | 本地基线快照文件；不给则需要出站凭据 |
| `--out` | `data/pending` | 草稿文件输出目录 |

## cli pending export

导出可带走的待人工处理清单：CSV（能进 Excel 排序筛选）+ 文本（能直接打印）两种文件。
导出不带患者可识别明细。

```bash
python3 -m patho_supplement.cli pending export --out ./out
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--state-db` | `$PATHOLOGY_STATE_DB` | 状态库路径 |
| `--out` | `data/pending-export` | 导出目录 |
| `--accession` | 无 | 只导出某一个病理号的挂起项 |

## cli audit export

导出审计流水：件头写明行数口径（对齐依据 / 挂起依据 / 草稿份数 / 状态迁移次数），
明细逐行给出来源系统、回报时间、命中方式与置信等级。导出不带患者可识别明细、不带结果值。

```bash
python3 -m patho_supplement.cli audit export --accession ACC-EXAMPLE-0001 --out ./out
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--state-db` | `$PATHOLOGY_STATE_DB` | 状态库路径 |
| `--accession` | 无 | 只导出某一个病理号的（不给则导出全部） |
| `--out` | `data/audit-export` | 导出目录 |

## cli benchmark

把一份基准集跑过产品真路径，逐条对账后给出指标（错绑定数、误并入数、同分多候选挂起率、
无第三去向），并在同一份输入上跑两种院内常见做法做三方对照。**指标或判定不达标即以非 0 退出**，
可直接挂 CI 闸门。

```bash
# 基准集 + 三方对照
python3 -m patho_supplement.cli benchmark --dataset tests/fixtures/benchmark --out reports/benchmark

# 用结果目录当基准集根
python3 -m patho_supplement.cli benchmark --inbox tests/fixtures/inbox/ --out reports/benchmark

# 规模与性能验收
python3 -m patho_supplement.cli benchmark --inbox tests/fixtures/scale/ --scale-report reports/scale.json
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--dataset` | `tests/fixtures/benchmark` | 基准集目录（须含 `baseline.json` 与 `ground_truth.json`） |
| `--inbox` | 无 | 结果目录作基准集根（替代 `--dataset`） |
| `--baseline` | 基准集目录下的 `baseline.json` | 基线快照路径 |
| `--ground-truth` | 基准集目录下的 `ground_truth.json` | 手写标准答案路径 |
| `--out` | `reports/benchmark` | 指标与产物输出目录 |
| `--state-db` | 无 | 给了则审计流水也落库（核对「依据一条不落」） |
| `--scale-report` | 无 | 规模验收报告落盘路径；给了就不做标准答案对账 |
| `--scale-work-dir` | `data/scale` | 规模验收的产物与状态库目录 |

规模验收的四项阈值可用环境变量调：`SCALE_MAX_ELAPSED_SECONDS`、`SCALE_MIN_ALIGNMENT_RATE`、
`SCALE_MAX_PENDING_RATE`、`SCALE_MAX_PEAK_MEMORY_MB`、`SCALE_MAX_STATE_DB_MB`。

## cli summary

打印某一轮跑了多少：从运行产物目录读（`replay` / `daemon` 写出的 `summary.json`），
或从状态库汇总。两个来源都给就都打印。

```bash
python3 -m patho_supplement.cli summary --dir data/replay
python3 -m patho_supplement.cli summary --state-db data/patho_state.db --json
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--dir` | 无 | 运行产物目录（读该目录下的 `summary.json`） |
| `--state-db` | `$PATHOLOGY_STATE_DB` | 状态库路径 |
| `--json` | 关 | 以 JSON 输出（供其他程序读取） |
