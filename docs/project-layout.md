# 项目文件一览

`README.md` 的「项目结构」小节给的是模块级概览；本文件是**逐文件全量清单**，每一行都带职责注释。

```text
pathology-supplement-report/
├── README.md                        # 中文产品说明（README 摘要版）
├── requirements.txt                 # 依赖清单：运行时零第三方，仅 pytest
├── pyproject.toml                   # 包元数据与 pytest 配置（testpaths / pythonpath）
├── conftest.py                      # 测试夹具入口：把项目根与 src/ 放进 sys.path
├── .env.example                     # 凭据位键名与说明，不含真实值
├── .gitignore                       # 排除 .env / 缓存 / 运行期数据 / round 元数据
├── qrcode.jpg                       # 公众号二维码
├── patho_supplement -> src/patho_supplement   # 导入软链，免安装即可直接运行
├── dictionaries/
│   ├── region-lexicon.json          # 部位同义词典（外置可编辑：规范词 + 别名 + 可选科室限定）
│   └── duty-roster.example.json     # 当班排班表样例（科室照此格式自己维护）
├── docs/
│   ├── concepts/
│   │   └── visual-target.md         # 结构化视觉目标卡（三个状态与视觉语言）
│   ├── screenshots/                 # 真实运行截图：守护会话 / doctor / replay / 挂起清单 / 草稿正文 / 出站实录
│   ├── cli.md                       # 运维命令全量参考（参数、默认值、退出码、真实 --help 输出）
│   ├── examples.md                  # 输出样例与真实运行实录（每条命令的完整终端输出）
│   ├── project-layout.md            # 本文件：逐文件全量清单
│   ├── inbound-hl7-spec.md          # 入站报文字段规范（字段序号 / 取值域 / 错误处理）
│   ├── benchmark-comparison.md      # 三方对照实验（口径、样本量、结果与判定）
│   └── scale-acceptance.md          # 规模与性能验收（口径、阈值、实测结果与复现方式）
├── src/patho_supplement/
│   ├── __init__.py                  # 包声明与版本号
│   ├── __main__.py                  # python -m patho_supplement 入口
│   ├── config.py                    # 环境变量配置面：.env 载入、缺项检查、路径规范化、阈值校验
│   ├── models.py                    # 领域模型：基线 / 部位 / 申请 / 结果 / 对齐依据 / 草稿
│   ├── db.py                        # SQLite 建表与幂等迁移（挂起项 / 版本链 / 队列 / 审计）
│   ├── logging_setup.py             # 结构化行日志与每轮运行摘要
│   ├── pipeline.py                  # 处理主路径的节点表与两道闸门推进
│   ├── daemon.py                    # 常驻主循环：目录监听 + 兜底扫描 + 单实例锁
│   ├── replay.py                    # 批处理重放：同一套主路径的离线跑法
│   ├── recovery.py                  # 中断恢复：重启后接回挂起项 / 幂等键 / 回写队列 / 待处理报文
│   ├── privacy.py                   # 脱敏：结果值遮蔽与患者可识别字段剔除
│   ├── timefmt.py                   # 时间格式化（统一 YYYY-MM-DD HH:mm）
│   ├── scale.py                     # 规模与性能验收（耗时 / 对齐率 / 内存 / 库体积四项指标与阈值）
│   ├── cli.py                       # 运维命令组：replay / daemon / doctor / pending / audit / benchmark / summary
│   ├── ingest/                      # 入站接入层
│   │   ├── er7.py                   # ER7 报文切分与段解析（错误带段号与行号）
│   │   ├── dirty.py                 # 脏行判据与 UTF-8→GBK 回退解码
│   │   ├── oru_parser.py            # ORU^R01 结果解析
│   │   ├── orm_parser.py            # ORM^O01 申请与控制动作解析
│   │   ├── zbr.py                   # 院内扩展段 ZBR（蜡块号 / 部位 / 部位序号）
│   │   ├── idempotency.py           # 幂等键、更正取代与拒收计数
│   │   ├── contract.py              # 字段登记表与「规范 / 登记表 / 解析器」三方一致性检查
│   │   └── inbox_watcher.py         # 目录监听与文件稳定性检查
│   ├── align/                       # 对齐引擎（核心）
│   │   ├── lexicon.py               # 部位同义词典的格式与加载
│   │   ├── normalizer.py            # 蜡块号与部位描述的规范化
│   │   ├── query_derivation.py      # 待查项派生与多组召回键派生
│   │   ├── scorer.py                # 中文词法打分与同义扩展
│   │   ├── filtering.py             # 硬过滤键构造与 REPLACE / MERGE 降级
│   │   ├── retriever.py             # 多路召回扇出（并发 + 每路失败隔离）
│   │   ├── merger.py                # 候选合并、去重与按分排序
│   │   ├── matcher.py               # 四级匹配与歧义拒绝
│   │   ├── order_guard.py           # 申请侧状态闸门（已取消 / 已终止的待查项不并入）
│   │   ├── rationale.py             # 对齐依据生成（命中方式 / 得分 / 召回键 / 来源）
│   │   ├── metrics.py               # 对账指标（对齐率 / 错绑定 / 误并入 / 挂起率）
│   │   ├── benchmark.py             # 基准集跑批（跑产品真路径，逐条对账）
│   │   ├── baselines.py             # 对照用的两种朴素做法（规范化精确匹配 / 词法最高分）
│   │   └── comparison.py            # 三方对照实验与判定（口径、结果表）
│   ├── pending/                     # 挂起与续跑（核心）
│   │   ├── store.py                 # 挂起项快照持久化与恢复
│   │   ├── reasons.py               # 挂起原因分类与缺失字段映射
│   │   ├── records.py               # 挂起项读写与原因历史
│   │   ├── fills.py                 # 补齐值的字段名校验与落库
│   │   ├── integrity.py             # 续跑前完整性校验（缺节点即报错）
│   │   ├── lock.py                  # 挂起行的行级互斥锁（并发补齐时一人续跑）
│   │   ├── resolve_keys.py          # 补齐幂等台账（同一组补齐值只生效一次）
│   │   ├── late_arrival.py          # 迟到结果自动接回挂起项
│   │   ├── export.py                # 待人工处理清单导出（CSV + 文本）
│   │   └── resume.py                # 人工补齐后从挂起处续跑
│   ├── compose/                     # 成稿与回写
│   │   ├── baseline.py              # 基线取回与部位列表解析
│   │   ├── draft_renderer.py        # 按部位分组的草稿渲染（含未到项占位）
│   │   ├── draft_store.py           # 草稿版本链落库（追加不覆盖）
│   │   ├── state_machine.py         # 草稿四态与检查项状态的迁移表（非法迁移拒绝）
│   │   ├── pathology_client.py      # 出站客户端：GET 基线 / POST 草稿
│   │   ├── writeback_queue.py       # 回写重试队列（退避 ≤5 次 + 超次升级）
│   │   └── conflict.py              # 版本冲突拒绝写回与转人工处置
│   ├── notify/                      # 通知
│   │   ├── base.py                  # 通知通道基类与错误码
│   │   ├── messages.py              # 通知正文模板（草稿待签发）
│   │   ├── dispatcher.py            # 多通道调度：失败重试与降级标注
│   │   ├── smtp_sender.py           # SMTP 通道
│   │   ├── wecom_sender.py          # 企业微信通道（可选降级）
│   │   └── router.py                # 当班医师路由（排班表 + 默认兜底）
│   └── audit/
│       ├── ledger.py                # 审计流水写入与读取
│       └── export.py                # 审计流水按病理号导出（CSV + 文本）
├── tools/                           # 开发辅助工具（不是产品运行期依赖）
│   ├── capture_screenshots.py       # 采集实测截图：起本地替身真跑一遍再渲染 PNG
│   ├── local_pathology_stub.py      # 本地病理系统替身（只换服务端，出站客户端走产品代码）
│   └── render_terminal_shot.py      # 终端输出 → PNG（等宽网格，中文按双字宽排版）
└── tests/
    ├── fixtures/                    # 脱敏样例（测试数据层）
    │   ├── generate_samples.py      # 样例合成脚本（固定种子可重跑，--scale 可放大）
    │   ├── inbox/                   # 常规回报样例（含基线快照）
    │   ├── edge/                    # 边界样例（无法获得 / 空值 / 时间倒挂 / 畸形报文）
    │   ├── cancelled/               # 申请侧取消样例
    │   ├── lexicon/                 # 外置同义词典样例
    │   └── benchmark/               # 基准集：基线 + 报文 + 手写标准答案
    └── test_*.py                    # 各模块单测、契约测试、对照实验与端到端回归
```

## 运行时生成的目录（不进仓库）

| 路径 | 内容 | 产生方式 |
|------|------|----------|
| `data/` | 状态库、守护产物、重放产物、导出件 | 运行时自动创建（`.gitignore` 已排除） |
| `logs/` | 行日志落盘文件 | 配了 `LOG_FILE` 才有 |
| `reports/` | 对照实验与规模验收报告 | `cli benchmark` 产出 |
| `__pycache__/`、`.pytest_cache/` | Python 缓存 | 运行时自动创建（发布前清理） |
