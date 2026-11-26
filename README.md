# Relocheck · 关系库切换校对工具

两个本地关系库连接 → 按**声明的业务键**对齐 → **先分段定位、再展开行级证据** →
在库仍被持续写入时，诚实地区分「可给确定结论的键段」与「不可判定、需延期复核的键段」，
并从聚合层面保证：**延期段永远不会被误判为相同**。

零第三方依赖，Python 3.11 标准库 + 原生前端。内置一套确定性的双 SQLite 演示库和一个
可控制的持续写入器（模拟「先写旧库 A、延迟后回放新库 B」的复制延迟）。

---

## 30 秒跑起来

```bash
python3 -m relocheck serve --port 8765
# 打开 http://127.0.0.1:8765
```

首次启动会在 `./demo_data/` 自动创建两个 SQLite 库并开始持续写入。界面上可以：

1. 看顶部总结论与统计砖；
2. 在「① 分段定位」里点任意键段卡片；
3. 在「② 行级证据」里看业务键、A/B 类型化值、NULL/精度/同键多行的逐条证据，
   以及该段的**水位时间线**（哪一侧、哪个事件位点、何时观察到写入/延迟）；
4. 点 **⚡ 制造一次复制延迟并校对**：立刻产生一个琥珀色「不可判定」键段，
   卡片上标注「临时相同/有差异 · 不作数」；
5. 等约 3 秒 B 追平后，进入该段点 **↻ 延期复核此段**，它才会变成确定的 ✓/✕，
   并保留复核历史；
6. 点 **📷 冻结写入并做快照一致校对**：停止写入、排空 A→B 延迟、对齐共享事件位点后，
   以快照模式重跑。

命令行同样可用：

```bash
python3 -m relocheck init-demo --dir demo_data
python3 -m relocheck run --config demo_data/relochk.demo.json            # 分段报告
python3 -m relocheck run --config demo_data/relochk.demo.json --segment 1 # 行级证据
python3 -m unittest discover -s tests -v                                  # 43 个测试
```

CLI 退出码：`0` 全部确定一致；`1` 存在确定差异；`3` 存在延期不可判定段（不能当通过）；
`2` 运行错误。

---

## 需求 → 实现位置

| 要求 | 实现 |
|---|---|
| 输入两个本地库连接 | `ReconcileConfig.source_a/source_b`（DSN），`sources.py`。SQLite 开箱即用；`postgres://`、`mysql://` 等在装有对应 DB-API 驱动（psycopg2 / pymysql）时通过通用 DbApi 源连接 |
| 主键不稳定，按声明业务键对齐 | `TableConfig.business_key`。声明业务键后，物理主键（如自增 `id`）自动排除出比较；演示库两侧 `id` 相差 10000+，行仍按 `order_no` 对齐 |
| 同键多行 | 按键分组后做**多重集抵消**：完全相同的行先抵消，剩余行再做最小距离配对出列差异 / 仅 A / 仅 B。绝不「取第一行」凑相等。见 `engine.compare_key_group` |
| 空值不能靠字符串凑相等 | `values.TypedValue` 给 NULL 独立 kind：`NULL == NULL` 才相同；`NULL vs ''`、`NULL vs 'NULL'`、`NULL vs 0` 全部 `null_mismatch`。比较路径上不存在 `str(a)==str(b)` |
| 数值精度不能靠字符串凑相等 | 精确数值走 `decimal.Decimal` 比量级；`0.104 vs 0.096` 即使格式化成两位小数都是 `0.10` 仍判不同；`0.100000 vs 0.100001` 判不同；浮点 `0.1+0.2` 的二进制值与 `0.3` 如实判不同（完整 repr/hex 入证据），只有显式声明 `abs_tolerance` 才按容差判等 |
| 精度/标度本身可见 | `DECIMAL(14,2) → DECIMAL(14,4)` 报 schema precision_scale 漂移；同量级不同标度给 `equal_value_precision_drift` 软提示；配置 `strict_precision: true` 或界面勾选「严格数值精度」后标度不一致直接判差异 |
| 先分段定位，再展开行级证据 | 业务键类型化编码的 sha256 哈希模 N 得稳定键段（与物理位置无关）。运行先返回每段计数/指纹/判定，`GET /api/segment` 才取该段完整行证据 |
| 持续写入令某次对比不可判定 → 界面可见原因 | 每段记录双测水位、确认指纹、合并时间线与窗口内 `sync_events` 写入事件，UI 展示「为什么不可判定」 |
| 快照一致性 vs 不停写不可兼得 | 两种模式：`live`（稳定性/对齐协议，达不到证据标准就 `deferred`）与 `snapshot`（冻结+对齐共享位点后的快照结论）。聚合层保证有 `deferred` 时总评绝不可能是 `verified_equal` |
| 验证延期段不会被误判相同 | `tests/test_consistency.py` 专门构造「临时比较恰好相同但扫描期被写入」「A/B 位点延迟」等场景，断言 verdict=`deferred`、不计入 match、总评不放行；API 层有同样的端到端测试 |

---

## 可判定性协议（核心）

实时模式下，对每个键段严格按下列顺序执行（`consistency.scan_segment`）：

1. **读前双水位**：分别读 A/B 的该段共享事件位点 `MAX(seq) … WHERE segment=?`
   （以及可选的 `version_column`、`updated_at_column`）；
2. **读行 + 读后水位**：各取该段行集，立刻再读水位；
3. **比较**得到候选结论与类型化多重集指纹；
4. **括号确认读**：`水位 a → 重算指纹 → 水位 b` 夹住复核指纹；
5. 只有同时满足以下条件才给确定结论：
   - 读后水位 == 两侧括号水位，且复核指纹 == 实际比较行集的指纹
     （捕获「读完即被改写」，原因码 `verify_changed` / `write_during_scan`）；
   - A/B 的该段**共享事件位点相同**（证明两侧对应同一逻辑状态，否则 `event_lag`）。

任一不满足 → 该段判定 **`deferred`（不可判定，需延期复核）**。候选逐行比较结果保留为
`provisional_content = match | diff`，但 UI/API 都把它标为「不作数」，并且：

```
段 verdict ∈ {match, diff, deferred}     # deferred 永远不等于 match
run totals: match 只数字面 verdict=match
overall ∈ verified_equal | differences_found |
          incomplete_deferred | incomplete_with_diffs | running
# 只要 deferred>0，overall 不可能是 verified_equal
```

**延期复核**（`POST /api/recheck`）重跑同一协议；B 追平且稳定后转 `match/diff`，
若写入又开始则再次 `deferred`。每次复核的旧判定进入 `history`。

**快照模式**：调用方负责冻结写入；演示台的「快照一致校对」会停写入器、等待 A→B
排空、确认两侧全局事件位点等于同一 token，再以 `mode=snapshot&snapshot_token=N` 读取。
若 token 尚未在两侧应用，直接 `snapshot_not_aligned` 延期，不假装快照成立。

> 没有共享事件日志的通用库：可退到 `version_column` 两遍稳定 + `updated_at_column`
> 静默时间窗（`quiescent_window` 依据）；若只有两遍指纹稳定而无跨侧对齐证据，
> 默认仍判 `alignment_unproven` 延期，只有显式 `accept_weak: true` 才采纳弱结论并明确标注。

---

## 比较语义细则

- **类型标记先行**：每个单元格 → `TypedValue(kind, value, meta)`，kind 含
  `null/bool/int/decimal/float/text/bytes/date/time/datetime/json`。
- **业务键也是类型化复合键**：`Key` 的哈希/相等包含 kind，因此整数 `1` 与文本 `"1"`
  是两个键，不会合并；演示中的文本键 `'00421'` 与 `'421'` 会分别报「B 缺 / B 多」，
  而不是去前导零凑对齐。
- **多重集指纹**：逐单元格带 kind 与长度前缀的规范编码，排序后 sha256，与行序无关、
  防分隔符碰撞。指纹只用于稳定性确认与段级快速对照，**不参与「值是否相等」的判定**。
- **Decimal 标度**：SQLite 的 `DECIMAL(p,s)` 列有 NUMERIC affinity，读回可能是 float；
  工具依据 `PRAGMA table_info` 的声明标度做类型驱动解码（`Decimal(str(x)).quantize(...)`），
  这是按声明类型解码，不是比较时用字符串凑相等。
- **JSON/API 中的 Decimal 永远是字符串**：前端不会 `JSON.parse` 成 number，避免浏览器
  二次丢精度。

---

## 接入自己的库

复制 `examples/config.example.json`：

```json
{
  "source_a": {"name": "旧", "dsn": "sqlite:////abs/path/old.db"},
  "source_b": {"name": "新", "dsn": "sqlite:////abs/path/new.db"},
  "tables": [{
    "name": "orders",
    "business_key": ["order_no"],
    "segment_count": 16,
    "version_column": "version",
    "updated_at_column": "updated_at",
    "event_log": {"table": "sync_events", "seq_column": "seq", "segment_column": "segment"},
    "columns": {"ratio": {"abs_tolerance": "1e-9"}}
  }]
}
```

```bash
python3 -m relocheck run --config my.json
python3 -m relocheck run --config my.json --segment 3      # 展开某段行级证据
python3 -m relocheck run --config my.json --out report.json # 导出完整 JSON
python3 -m relocheck serve --config my.json --port 8765   # 只读 UI（无演示写入器）
```

不想写配置时，可用临时参数直接校对两个本地库：

```bash
# 静态文件 / 可接受两遍稳定指纹的弱依据时，加 --accept-weak 给出确定结论
python3 -m relocheck run \
  --a old.db --b new.db --table orders --business-key order_no \
  --accept-weak --out report.json

# 没有共享事件位点且不加 --accept-weak：所有段一律 alignment_unproven 延期，
# 即使两遍指纹稳定也不冒充强结论（退出码 3）。
```

建议在被切换的两库中各放一个共享序号的事件/变更日志（演示的 `sync_events`：
`seq, segment, order_no, op, at`），这是实时写入下拿到**强可判定结论**的关键。
关系库 DSN 只要环境里装了相应 DB-API 驱动即可扩展（见 `sources.DbApiSource`）。

---

## HTTP API

| 方法/路径 | 说明 |
|---|---|
| `GET /api/context` | 连接、表/业务键配置、写入器状态 |
| `POST /api/runs` | body：`{mode, snapshot_token?, strict_precision?, accept_weak?}`，返回 `run_id`，后台分段执行 |
| `GET /api/runs` / `GET /api/runs/{id}` | 运行列表 / 含每段摘要与聚合 totals |
| `GET /api/segment?run_id=&table=&segment=` | 单段完整行级证据 + 水位时间线 |
| `POST /api/recheck` | `{run_id, table, segment}` 延期复核 |
| `POST /api/writer` | 演示写入器：`start/stop/burst/prepare_snapshot` |
| `POST /api/demo/reset` | 重建演示库 |

---

## 目录与测试

```
relocheck/
  values.py       # TypedValue / Key / Decimal·NULL·浮点语义、规范编码
  config.py       # JSON 配置模型
  sources.py      # SQLite 源 + 通用 DB-API 源
  engine.py       # 业务键分桶、同键多行多重集、列差异、分段指纹（纯函数）
  consistency.py  # 水位/确认读/事件位点协议，match|diff|deferred 判定与时间线
  manager.py      # 运行编排、聚合不变量、延期复核历史、schema 漂移
  demo.py         # 确定性植入差异的双库 + 持续/突发写入器
  server.py       # stdlib HTTP API + 静态 UI
  cli.py          # init-demo / run / serve
web/              # 原生 HTML/CSS/JS 界面
tests/            # values / engine / consistency / HTTP API，共 43 例
```

测试覆盖的关键不变量（摘录）：

- `test_write_during_a_read_defers_even_when_provisionally_equal`：临时结论相同也必须延期；
- `test_deferred_segment_not_counted_as_match_in_run_totals`；
- `test_provisionally_equal_deferred_is_counted_by_aggregate`；
- `test_recheck_after_catchup_becomes_definitive_match_on_clean_seg`；
- `test_recheck_can_defer_again_when_writes_resume`；
- `test_lagging_side_defers_with_event_lag`；
- `test_snapshot_token_not_applied_on_b_defers`；
- `tests/test_api.py` 经真实 HTTP 端口复验以上全部。
