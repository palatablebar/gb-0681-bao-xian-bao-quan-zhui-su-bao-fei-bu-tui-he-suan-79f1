# 保险保全追溯与保费补退核算

面向保险保全数据复核员，处理采信、判定、核算、复核与归档。

## 启动

```bash
docker compose up --build -d
```

访问 http://localhost:18250 。初次启动自动运行脏样例，可切换 clean.csv 与 legacy.csv。顶部“运行批次”会重新计算，同源输入复用批次。

## 业务模块

- 保单采信：`/m1_ingest`、`/api/m1_ingest`
- 责任映射：`/m2_mapping`、`/api/m2_mapping`
- 保全选版：`/m3_versions`、`/api/m3_versions`
- 承保切片：`/m4_segments`、`/api/m4_segments`
- 保费重算：`/m5_calculate`、`/api/m5_calculate`
- 补退对账：`/m6_reconcile`、`/api/m6_reconcile`
- 结案归档：`/m7_archive`、`/api/m7_archive`

## 核心规则

按保全有效日拆分责任区间，撤销回滚指向原变更；实收补退按日精确分配尾差。完整数值、边界和异常定义见 `docs/DATA-DICTIONARY.md`。

## 验证

```bash
docker compose exec -T app pytest -q
docker compose config --quiet
```

原始样例在 `sample_data/`。测试在 `tests/`。封存文件是单份CSV，含运行清单、计算证据、隔离原因、对账与修订台账。复核需提交当前版本，陈旧复核返回409。

## 隔离补正（修订链）

裁决见 `docs/CORRECTION-RULING.md`：**补正不覆盖原批次**，原批次冻结为 `superseded`，补正生成后继批次（`active`），同一修订链只允许一个可发布结论。

- 发起：`POST /api/runs/<run_id>/corrections`，台账逐条覆盖父批次隔离目标（冲突按整组），`CORRECTED` 须带补正后取值，任何处置都须填写依据。
- 幂等：同源重跑沿用原 `run_id`；同一父批次+同一台账指纹返回同一后继批次，不新增修订。
- 追溯：`GET /api/runs/<run_id>/lineage`；后继批次的修订台账含原值（before）、补正后值（after）、原因码与依据，并入结案 CSV 与校验和。
- 约束：已取代批次只读，复核与导出均返回 409；落库与链头切换同事务，失败回滚保留旧版本。

## 数据与限制

SQLite保存在records命名卷，普通重启与构建不会清除。确认可丢弃本项目数据后，运行 `docker compose down -v` 再启动可重置。单文件上限2MB；不接入外部业务系统；历史规则由本项目样例定义，不代表实际行业标准。
