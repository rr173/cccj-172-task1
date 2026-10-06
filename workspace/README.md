# 数据质量规则版本化执行与回溯裁决平台

数据产品以**固定快照**发布字段、约束与跨表规则；执行器为每条失败记录**精确样本定位 + 规则版本**。
规则升级可以对历史快照**回放**，但新结果只能形成**修订版（replay）**，绝不覆盖当时的发布判断。
数据修复、规则回放、发布冻结、异常豁免四类操作并发时串行裁决；豁免必须绑定**精确样本 × 规则摘要**；
分页只读取一个**固定结果快照**。平台提供规则 API、异常工作台和可下载、可验真的**质量凭据**。

## 核心不变量

1. **数据快照不可变**：修复（upsert/delete 行）从旧快照派生新快照，旧快照原样保留（修复链 `parent_id`）。
2. **发布判断执行即冻结**：每个数据快照至多一份 `publish` 结果（部分唯一索引 + 事务保证），冻结后再次执行返回 `409`。
3. **回放只产生修订版**：`replay` 结果 `supersedes_id` 指向被修订结果，`frozen=false`，绝不改写原结果；裁决链完整可溯。
4. **失败样本精确定位**：每条 finding 记录 `表名 + 稳定业务行键 JSON + 列 + 观测值 + 规则编码/版本/语义摘要`。
5. **豁免双绑定**：豁免唯一键为 `数据快照 + 表 + 行键 + 列 + rule_digest`。
   规则升级后参数变化 → 摘要变化 → 旧豁免不会误覆盖新规则；样本必须真实存在于该快照。
6. **固定快照分页**：分页只查某一结果快照的 `findings`，行序由 `seq` 固定；
   `exempt_at_execution` 是执行时刻冻结的状态，之后豁免变化不改写它（工作台另以 `current_exemption_id` 注释当前状态）。
7. **质量凭据防篡改**：凭据为带封条 SHA-256 的文本，下载即用；验真同时校验内嵌封条自洽与整文件哈希与库存一致。
8. **并发串行裁决**：四类写操作全部经过应用级 RLock + SQLite `BEGIN IMMEDIATE`，
   结果确定：要么成功、要么明确冲突（409/422），绝不脏写或双发布。

## 架构

```
app/
  config.py        路径与分页上限
  db.py            SQLite 连接、BEGIN IMMEDIATE 串行事务、schema
  engine.py        规则摘要(SHA-256)、7 类算子执行器（内存库回放）
  service.py       领域服务：快照/修复、规则版本、执行/回放/冻结、豁免、工作台、凭据
  schemas.py       Pydantic 请求模型
  routers/
    rules.py       规则 API、数据快照、执行、回放、冻结、裁决链
    workbench.py   固定分页、异常工作台、豁免授予/撤销
    credentials.py 凭据签发/下载/验真
  main.py          FastAPI 入口
tests/             21 个端到端测试（含四操作并发、重复豁免竞态）
scripts/smoke.sh   真实 HTTP 端到端冒烟脚本
```

### 规则模型

- `rule_code`：稳定业务规则标识（如 `ORDER_AMOUNT_RANGE`），豁免与跨版本追溯都引用它。
- `engine_code`：引擎算子，7 种：
  `NOT_NULL`（字段）、`REGEX`（字段）、`RANGE`（约束）、`ENUM`（约束）、`UNIQUE`（约束）、
  `REFERENTIAL_INTEGRITY`（跨表）、`SQL_ASSERTION`（跨表 SQL，须 `select __row_key AS row_key ...`）。
- 同 `rule_code` 每登记一次自增 `version`，新版本自动把旧生效版本置为 `superseded`（行永久保留）；
  也可人工 `deprecate`。`severity=warning` 只记录不否决发布，`error` 才使裁决为 `fail`。
- `rule_digest = SHA256(canonical(rule_code, engine_code, rule_type, table_name, params))`。

## 快速开始

```bash
pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# Swagger: http://127.0.0.1:8000/docs
```

环境变量：`DQDB_PATH`（SQLite 文件）、`DQCRED_DIR`（凭据目录）。

```bash
pytest -q                     # 21 passed
bash scripts/smoke.sh         # 需先在 :8077 启动服务（脚本内端口可改）
```

## API 摘要（前缀 `/api/v1`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/products` | 创建数据产品 |
| POST | `/products/{p}/snapshots` | 登记不可变数据快照（表行 + 业务主键列） |
| POST | `/products/{p}/snapshots/repair` | 数据修复，派生新快照 |
| GET  | `/snapshots/{id}/rows?table=` | 读取快照行（不可变） |
| POST | `/products/{p}/rules` | 登记规则下一版本（自动取代旧版） |
| GET  | `/products/{p}/rules?rule_code=` | 规则版本链 |
| POST | `/products/{p}/rules/{code}/deprecate` | 弃用规则 |
| POST | `/products/{p}/execute` | 发布执行（形成并冻结发布判断） |
| POST | `/products/{p}/replay` | 历史快照回放，只形成修订版 |
| POST | `/products/{p}/results/{r}/freeze` | 发布冻结（幂等） |
| GET  | `/products/{p}/results` / `/results/{r}` | 结果快照 |
| GET  | `/products/{p}/results/{r}/chain` | 发布判断 → 修订版裁决链 |
| GET  | `/results/{r}/findings?offset&limit&rule_code&exempt=` | **固定结果快照**分页 |
| GET  | `/products/{p}/workbench/{r}` | 异常工作台（固定分页 + 当前豁免标注） |
| POST | `/products/{p}/exemptions` | 授予豁免（finding_id 或精确坐标 + 规则版本） |
| POST | `/products/{p}/exemptions/{e}/revoke` | 撤销豁免 |
| POST | `/products/{p}/results/{r}/credential` | 签发质量凭据（幂等） |
| GET  | `/results/{r}/credential/download` | 下载凭据（attachment） |
| GET  | `/results/{r}/credential/verify` | 封条 + 库存哈希验真 |

### 典型时序

```
publish snapshot ──► execute(publish, frozen=verdict@T0)
                         │  规则升级 v2（digest 改变）
                         ▼
                      replay ──► result kind=replay, supersedes=发布结果
                         │           （原发布判断一字不改）
修复数据 ──► 新数据快照 ──► 新的 publish 判断
豁免精确样本×规则摘要 ──► 仅对之后的执行/回放生效（exempt_at_execution）
```
