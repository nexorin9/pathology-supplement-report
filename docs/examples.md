# 输出样例与真实运行实录

本文件是 `README.md`「输出样例」小节的完整版：把每条命令的**真实终端输出**整段留档，
供院内信息科核对格式、供开发复现。

## 采集方式

下面每一段都是本机真跑出来的，不是手写的样例：

```bash
# 起一个本地病理系统替身（只换服务端，出站客户端走产品代码）+ 喂脱敏样例
python3 - <<'PY'
import os, shutil, subprocess, sys
from pathlib import Path
ROOT = Path.cwd()
sys.path.insert(0, str(ROOT / "tools"))
from local_pathology_stub import LocalPathologyStub

baseline = {"reports": [
    {"accession_no": "ACC-EXAMPLE-0001", "base_version": "base-v3", "blocks": [
        {"region_seq": 1, "region_desc": "子宫体", "block_no": "B1",
         "order_no": "ORD-EXAMPLE-0001", "ordered_items": ["IHC-CD3", "IHC-CD20"]},
        {"region_seq": 2, "region_desc": "宫颈", "block_no": "B2",
         "order_no": "ORD-EXAMPLE-0002", "ordered_items": ["IHC-CD20", "IHC-CK7"]}]},
    {"accession_no": "ACC-EXAMPLE-0002", "base_version": "base-v1", "blocks": [
        {"region_seq": 1, "region_desc": "乳腺", "block_no": "B5",
         "order_no": "ORD-EXAMPLE-0003", "ordered_items": ["IHC-ER", "IHC-PR"]}]},
]}
work = ROOT / "data" / "examples"
shutil.rmtree(work, ignore_errors=True)
work.mkdir(parents=True, exist_ok=True)
stub = LocalPathologyStub(baseline)
os.environ.update({
    "PATHOLOGY_BASE_URL": stub.base_url,
    "PATHOLOGY_API_TOKEN": "local-example-token",
    "PATHOLOGY_RESULT_INBOX": str(work / "inbox"),
    "PATHOLOGY_STATE_DB": str(work / "state.db"),
    "NOTIFY_RECIPIENTS": "pathologist-day@example.invalid",
})
shutil.copytree(ROOT / "tests/fixtures/inbox", work / "inbox", dirs_exist_ok=True)
subprocess.run([sys.executable, "-m", "patho_supplement.cli", "daemon", "--once",
                "--inbox", str(work / "inbox"), "--state-db", str(work / "state.db"),
                "--out", str(work / "daemon")], env=dict(os.environ))
stub.close()
PY
```

说明：

- **替身只在本机存在**：`tools/local_pathology_stub.py` 只替换服务端，出站客户端的请求路径、
  `Authorization: Bearer` 头、请求体字段与回执解析走的都是产品代码；正式对接时把
  `PATHOLOGY_BASE_URL` 换成院内病理系统即可，调用路径与凭据位不变。
- **样例是脱敏占位**：病理号用 `ACC-EXAMPLE-0001` 这类类目化占位，部位用「子宫体 / 宫颈 / 乳腺」，
  不含姓名、身份证、联系方式等患者可识别明细。
- **本机路径已缩写成相对路径**：实录里的绝对路径统一缩到项目目录内，便于阅读。

## 命令与退出码速查

| 退出码 | 含义 |
|--------|------|
| `0` | 成功 |
| `1` | 失败（凭据缺失、状态库不存在、接口不可达、指标不达标） |
| `2` | 参数用错（子命令缺参数、字段名不认识、补齐值格式不对） |
| `3` | 补齐后仍对不上，挂起项保持 pending（等人再补） |
| `4` | 挂起行正被另一个人处理中（已登记的补齐值不丢，可重试） |
| `5` | 已有另一个守护进程在运行（锁文件被占） |

## 产物文件

| 产物 | 位置 | 内容 |
|------|------|------|
| 补充报告草稿 | `<out>/drafts/{病理号}.txt` | 按取材部位分组、逐项标来源与回报时间 |
| 补充报告草稿（守护形态） | `<out>/drafts/{病理号}-v{版本}.txt` | 带版本号的落盘件 |
| 待人工处理清单 | `<out>/pending_list.txt` | 谁没对上、缺哪个字段、候选是谁、怎么补 |
| 待人工处理清单（可带走） | `<out>/pending_list.csv` + `pending_list.txt` | CSV 带 BOM，Excel 可直接打开排序筛选 |
| 运行摘要 | `<out>/summary.json` / `<out>/round_summary.json` | 入账 / 并入 / 挂起 / 回写 / 耗时 |
| 对照实验结果 | `reports/benchmark/comparison.json` + `comparison.txt` | 三方指标与逐条判定 |
| 规模验收报告 | `reports/scale.json` | 耗时 / 对齐率 / 内存 / 库体积四项指标与阈值判定 |
| 审计流水 | `<out>/audit_trail.csv` + `audit_trail.txt` | 对齐依据 / 挂起依据 / 状态迁移 / 回写回执 |

## 完整运行实录

### 守护进程跑一轮（取基线 → 对齐 → 成稿 → 回写 → 通知）

```text
$ python3 -m patho_supplement.cli daemon --once --inbox data/examples/inbox --state-db data/examples/state.db --out data/examples/daemon
[退出码 0]
2026-10-28 21:03:22 | INFO  | event=daemon_recovered pending_items=0 writeback_open=0 idempotency_keys=0 leftover_files=9 stale_locks_released=0
2026-10-28 21:03:23 | INFO  | event=round_start started_at="2026-10-28 21:03:23"
2026-10-28 21:03:23 | INFO  | event=fanout_done routes_total=12 routes_ok=12 routes_failed=0 routes_timed_out=0 kept=3
2026-10-28 21:03:23 | INFO  | event=draft_written accession_no=ACC-EXAMPLE-0001 draft_version=4 accepted_version=draft-v2 entries=3 pending=1
2026-10-28 21:03:24 | INFO  | event=fanout_done routes_total=6 routes_ok=6 routes_failed=0 routes_timed_out=0 kept=2
2026-10-28 21:03:24 | INFO  | event=draft_written accession_no=ACC-EXAMPLE-0002 draft_version=2 accepted_version=draft-v2 entries=2 pending=0
2026-10-28 21:03:25 | INFO  | event=round_summary processed=7 aligned=5 pending=1 writeback_ok=2 writeback_failed=0 elapsed_seconds=2.076 files_seen=9 duplicates=1 malformed=0 conflicts=0 pending_escalated=0 notify_sent=0 notify_failed=2 placeholder_count=1
[2026-10-28 21:03:22] 启动恢复｜挂起项 0 条（转人工 0 条）｜回写队列 待重试 0 条、已升级 0 条｜已落定结果 0 条｜结果目录待处理 9 个文件
[2026-10-28 21:03:23] 回写成功｜病理号 ACC-EXAMPLE-0001｜草稿 v4｜接受版本 draft-v2｜条目 3 条
通知：SMTP 邮件失败（企业微信通道未配置；已降级为只走 SMTP 邮件）
[2026-10-28 21:03:24] 回写成功｜病理号 ACC-EXAMPLE-0002｜草稿 v2｜接受版本 draft-v2｜条目 2 条
通知：SMTP 邮件失败（企业微信通道未配置；已降级为只走 SMTP 邮件）
本轮：文件 9 个 → 入账 7 条（重复 1、更正取代 1）｜草稿回写成功 2 份、失败 0 份、冲突 0 份；挂起 1 条｜待查项 6 项（在等结果 6、取消 0、暂停 0、终止 0）｜申请侧事件 1 个（解析出申请 1 条，拒收 0 条）｜对齐判定 L1 蜡块号精确 5 条、L4 挂起 1 条｜多路召回 6 路（成功 6、失败 0、超时 0），命中 2 条｜对齐依据 5 条（挂起 1 条）｜更正生效 1 条、倒挂未生效 0 条｜待回报占位 1 项（已申请、这一版还没回来）｜收件人路由：降级到科室默认收件人（未配置排班表（NOTIFY_DUTY_ROSTER_PATH）；已改用科室默认收件人（1 个））｜通知 发出 0 条、失败 2 条（收件人路由：降级到科室默认收件人（未配置排班表（NOTIFY_DUTY_ROSTER_PATH）；已改用科室默认收件人（1 个））；企业微信通道未配置；已降级为只走 SMTP 邮件）｜耗时 2.08s
```

### 运维自检：凭据位、对外接口清单与出站连通性

```text
$ python3 -m patho_supplement.cli doctor --accession ACC-EXAMPLE-0001
[退出码 0]
== 病理补充报告 · 运维自检 ==

【凭据位】
  PATHOLOGY_BASE_URL        已配置
  PATHOLOGY_API_TOKEN       已配置
  PATHOLOGY_RESULT_INBOX    已配置
  SMTP_HOST                 已配置
  SMTP_USER                 已配置
  SMTP_PASSWORD             已配置
  SMTP_FROM                 已配置
  NOTIFY_RECIPIENTS         已配置
  WECOM_WEBHOOK_URL         未配置

【对外接口清单】
  1. [入站] ORU^R01 结果事件流
     契约：结果投递目录下的落盘文件（免疫组化室 / 分子实验室投递）
     凭据：PATHOLOGY_RESULT_INBOX
  2. [入站] ORM^O01 申请事件流
     契约：同上（决定哪些检查项在等结果）
     凭据：PATHOLOGY_RESULT_INBOX
  3. [出站] GET 病理报告基线
     契约：GET {PATHOLOGY_BASE_URL}/api/v1/pathology-reports/{病理号}
     凭据：PATHOLOGY_BASE_URL + PATHOLOGY_API_TOKEN
  4. [出站] POST 补充报告草稿
     契约：POST {PATHOLOGY_BASE_URL}/api/v1/pathology-reports/{病理号}/supplemental-drafts
     凭据：PATHOLOGY_BASE_URL + PATHOLOGY_API_TOKEN
  5. [推送] SMTP / 企业微信通知
     契约：成稿后通知当班医师（未开通时可降级，不阻塞成稿）
     凭据：SMTP_* + WECOM_WEBHOOK_URL

【出站连通性】
  TCP 连通：可达（127.0.0.1:51669 可连接）
  基线取数：可用（基线可取（ACC-EXAMPLE-0001，2 个取材部位））

【生效阈值与运行参数】
  参数        环境变量                  当前值    缺省值    合法范围
  词法命中阈值 BM25_THRESHOLD            0.32      0.32      0–1 之间的数值
  领先幅度阈值 LEAD_MARGIN_THRESHOLD     0.05      0.05      0–1 之间的数值
  硬过滤策略  FILTER_POLICY             MERGE     MERGE     REPLACE 或 MERGE
  召回并发数  FANOUT_MAX_WORKERS        4         4         1–32 的整数
  单路召回超时 FANOUT_ROUTE_TIMEOUT      5.0       5.0       0.1–600 秒
  挂起尝试上限 PENDING_MAX_ATTEMPTS      3         3         1–50 的整数
  挂起行锁有效期 PENDING_LOCK_TTL          300.0     300.0     1–3600 秒
  回写重试上限 WRITEBACK_MAX_RETRIES     5         5         0–50 的整数
  回写退避基数 WRITEBACK_RETRY_BASE_SECONDS 5.0       5.0       0.1–3600 秒
  回写退避封顶 WRITEBACK_RETRY_MAX_SECONDS 900.0     900.0     1–86400 秒
  出站超时    HTTP_TIMEOUT              10.0      10.0      0.1–600 秒
  目录轮询间隔 WATCH_POLL_INTERVAL       2.0       2.0       0.1–3600 秒
  落稳等待    WATCH_SETTLE_SECONDS      0.5       0.5       0–600 秒
  兜底扫描间隔 WATCH_FALLBACK_INTERVAL   60.0      60.0      1–86400 秒
  单实例锁文件 PATHOLOGY_LOCK_FILE       （未配置） （未配置） 可写路径；不配置则放在状态库同级
  通知重试次数 NOTIFY_MAX_ATTEMPTS       2         2         1–10 的整数
  通知重试间隔 NOTIFY_RETRY_DELAY_SECONDS 1.0       1.0       0–60 秒
  通知超时    NOTIFY_TIMEOUT            10.0      10.0      0.1–600 秒
  日志级别    LOG_LEVEL                 INFO      INFO      DEBUG / INFO / WARNING / ERROR
  日志文件    LOG_FILE                  （未配置，写标准错误） （未配置） 可写路径；不配置则输出到标准错误
  结果值入日志 LOG_RESULT_VALUES         否        否        true / false；默认不把判读结果写进日志
  规模验收耗时上限 SCALE_MAX_ELAPSED_SECONDS 60.0      60.0      0.1–86400 秒
  规模验收对齐率下限 SCALE_MIN_ALIGNMENT_RATE  0.6       0.6       0–1 之间的数值
  规模验收挂起率上限 SCALE_MAX_PENDING_RATE    0.4       0.4       0–1 之间的数值（与对齐率互补）
  规模验收内存上限 SCALE_MAX_PEAK_MEMORY_MB  256.0     256.0     1–1048576 MB
  规模验收库体积上限 SCALE_MAX_STATE_DB_MB     64.0      64.0      0.1–1048576 MB

【通知通道】
  SMTP 邮件       已配置  SMTP 127.0.0.1:1（默认收件人 1 个）
  企业微信群机器人      未配置  企业微信通道未配置

【当班收件人路由】
  排班表                   未配置（NOTIFY_DUTY_ROSTER_PATH）
  排班表版本                 （无）
  班次数                   0
  告警：未配置排班表（NOTIFY_DUTY_ROSTER_PATH）
  当前收件人                 pathologist-day@example.invalid
  路由依据                  科室默认收件人｜未配置排班表（NOTIFY_DUTY_ROSTER_PATH）；已改用科室默认收件人（1 个）

【部位同义词典】
  版本                    builtin-2026.10
  条目数                   24（别名 60）
  来源                    内置最小词表

自检通过：凭据位齐备。
```

### 批处理重放

```text
$ python3 -m patho_supplement.cli replay --inbox data/examples/inbox --out data/examples/replay
[退出码 0]
2026-10-28 21:03:25 | INFO  | event=replay_summary processed=0 aligned=0 pending=0 writeback_ok=0 writeback_failed=0 elapsed_seconds=0.002 files_scanned=0 duplicates=0 malformed=0 drafts=0 placeholder_count=0
== 病理补充报告 · 批处理重放 ==
结果目录：data/examples/inbox
基线来源：data/examples/inbox/baseline.json
报文文件：0 个（结果 0 / 申请 0）
回报结果：0 条 → 入账 0 条（重复投递 0 条，更正取代 0 条）
草稿：0 份，共 0 条；待人工处理：0 条
对齐判定：L1 蜡块号精确 0 条、L2 部位描述精确 0 条、L3 词法模糊 0 条、L4 挂起 0 条
对齐依据：0 条（挂起 0 条）
待查项：6 项（在等结果 6、取消 0、暂停 0、终止 0）；检索键 18 把
多路召回：18 路（成功 18、失败 0、超时 0），命中 0 条（并发上限 4、单路超时 5.0 秒）
候选合并：合并 0 条 → 去重后 0 条（去掉重复 0 条）
产物：
  待处理  data/examples/replay/pending_list.txt
部位同义词典：版本 builtin-2026.10，条目 24（别名 60），来源 内置最小词表
```

### 待人工处理清单（挂起项列表）

```text
$ python3 -m patho_supplement.cli pending list --state-db data/examples/state.db
[退出码 0]
== 待人工处理清单 ==
状态库：data/examples/state.db
  行ID 病理号            项目        原因          待补字段          候选部位                  尝试 快照时间           状态
  1    ACC-EXAMPLE-0001  IHC-KI67    缺蜡块号      蜡块号            子宫体/B1；宫颈/B2        1   2026-10-28 21:03:23 等人补齐
合计：1 条待人工处理
```

### 导出可带走的待人工处理清单

```text
$ python3 -m patho_supplement.cli pending export --state-db data/examples/state.db --out data/examples/export
[退出码 0]
== 挂起清单导出 ==
状态库：data/examples/state.db
CSV：data/examples/export/pending_list.csv
文本：data/examples/export/pending_list.txt
条目数：1 条
原因分布：缺蜡块号 1 条
```

### 运行摘要

```text
$ python3 -m patho_supplement.cli summary --dir data/examples/daemon --state-db data/examples/state.db
[退出码 0]
== 病理补充报告 · 运行摘要 ==
产物目录：data/examples/daemon
摘要文件：data/examples/daemon/round_summary.json
本轮开始：2026-10-28 21:03:23

【本轮口径】
  口径                  数值
  入账结果              7
  并入草稿              5
  挂起待人工            1
  写回成功              2
  写回失败              0

【明细】
  本轮耗时（秒）         2.076
  看到文件            9
  重复投递            1
  畸形拒收            0
  待回报占位           1
  版本冲突            0
  转人工             0
  通知发出            0
  通知失败            2

【本轮一行摘要】本轮处理 7 条｜对齐 5 条｜挂起 1 条｜写回成功 2 份、失败 0 份｜耗时 2.08 秒

== 状态库概览 ==
状态库：data/examples/state.db
  草稿条目 5 条（不含待回报占位）
  草稿状态：
    written_back 2
  挂起项状态：
    pending 1
  回写队列状态：（无）
  版本冲突状态：（无）
  检查项状态：（无）
  审计分类：
    align    6
    ingest   12
    notify   6
    runtime  1
    state    4
    writeback 2
  审计流水合计 31 条
```

### 三方对照（基准集）

```text
$ python3 -m patho_supplement.cli benchmark --out data/examples/benchmark
[退出码 0]
== 病理补充报告 · 基准集跑批 ==
基准集：tests/fixtures/benchmark（版本 bench-2026.10）
标准答案：21 条
指标文件：data/examples/benchmark/metrics.json

基准集样本：21 条
对齐率：11/21 = 52.4%
错绑定数：0
误并入数：0
挂起率：10/21 = 47.6%
同分多候选挂起率：2/2 = 100.0%
无第三去向：0
漏对齐：0｜原因不符：0
缺样本：0｜多出样本：0

指标达标：错绑定 0、误并入 0、无第三去向 0、同分多候选挂起率 100%。

== 三方对照（同一份输入、同一套指标） ==
样本量：21 条

  做法                                    并入   并入率  对齐率  错绑定  误并入  挂起率
  本产品｜四级级联 + 唯一领先 + 状态闸门  11/21  52.4%   52.4%   0       0       47.6%
  基线 A｜仅规范化精确匹配                11/21  52.4%   33.3%   0       4       47.6%
  基线 B｜仅取词法最高分                  17/21  81.0%   52.4%   0       6       19.0%

口径：三方共用同一份输入：去重与更正取代后的生效结果条目 + 同一份基线快照；无法获得 / 值为空 / 回报时间倒挂的前置判定三方一致（与归属无关）；差异只在「结果归到哪一块」这一步。对齐率 = 归到正确块的条数 ÷ 样本条数；并入率 = 并入草稿的条数 ÷ 样本条数（含归错的）；错绑定 = 并入但归到了另一块；误并入 = 标准答案要求挂起却被并入；挂起率 = 进了待人工处理清单的条数 ÷ 样本条数。「原因不符」不计入对照口径 —— 挂起原因的中文标签是产品词汇，朴素做法没有这套标签。

判定：
  [通过] 本产品错绑定数 = 0：错绑定 0 条（基线 A 0 条、基线 B 0 条）
  [通过] 本产品误并入数 = 0：误并入 0 条（基线 A 4 条、基线 B 6 条）
  [通过] 本产品对齐率不低于基线 A：本产品 52.4%（11/21） vs 基线 A 33.3%（7/21）
  [通过] 本产品对齐率不低于基线 B：本产品 52.4%（11/21） vs 基线 B 52.4%（11/21）
  [通过] 本产品无第三去向 = 0：无第三去向 0 条（结果既没进草稿也没进清单）
对照结果文件：data/examples/benchmark/comparison.json
```

