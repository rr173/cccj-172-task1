#!/usr/bin/env bash
# 端到端冒烟：覆盖发布执行 -> 规则升级回放 -> 豁免 -> 工作台分页 -> 凭据下载验真
set -euo pipefail
B=http://127.0.0.1:8077/api/v1
j() { python3 -c "import sys,json;_=json.load(sys.stdin);print($1)"; }

PID=$(curl -s -XPOST $B/products -H 'Content-Type: application/json' \
  -d '{"name":"orders-daily","description":"每日订单数据产品"}' | j "_['id']")
echo "product=$PID"

SID=$(curl -s -XPOST $B/products/$PID/snapshots -H 'Content-Type: application/json' -d '{
  "snapshot_ref":"2026-10-06-daily",
  "tables":{"orders":[
    {"id":"O-1","customer_id":"C-1","amount":100,"status":"paid","email":"a@x.com"},
    {"id":"O-2","customer_id":"C-2","amount":-5,"status":"paid","email":"bad"},
    {"id":"O-3","customer_id":null,"amount":9999,"status":"weird","email":"c@x.com"}],
   "customers":[
    {"id":"C-1","name":"Alice","tier":"gold"},
    {"id":"C-2","name":"Bob","tier":"silver"}]},
  "key_columns":{"orders":["id"],"customers":["id"]}}' | j "_['id']")
echo "snapshot=$SID"

mkrule() { curl -s -XPOST $B/products/$PID/rules -H 'Content-Type: application/json' -d "$1" >/dev/null; }
mkrule '{"rule_code":"CUST_NOT_NULL","engine_code":"NOT_NULL","name":"客户非空","rule_type":"field","table_name":"orders","params":{"column":"customer_id"}}'
mkrule '{"rule_code":"AMOUNT_RANGE","engine_code":"RANGE","name":"金额0-1000","rule_type":"constraint","table_name":"orders","params":{"column":"amount","min":0,"max":1000}}'
mkrule '{"rule_code":"STATUS_ENUM","engine_code":"ENUM","name":"状态枚举","rule_type":"constraint","table_name":"orders","params":{"column":"status","values":["paid","pending","refunded"]}}'
mkrule '{"rule_code":"FK_CUSTOMER","engine_code":"REFERENTIAL_INTEGRITY","name":"客户引用","rule_type":"cross_table","table_name":"orders","params":{"child_columns":["customer_id"],"parent_table":"customers","parent_columns":["id"]}}'
echo "rules v1 created"

PUB=$(curl -s -XPOST $B/products/$PID/execute -H 'Content-Type: application/json' \
  -d "{\"data_snapshot_id\":$SID,\"executor\":\"ci\",\"note\":\"2026-10-06 发布执行\"}")
PUB_ID=$(echo "$PUB" | j "_['id']")
echo "publish id=$PUB_ID verdict=$(echo "$PUB" | j "_['verdict']") frozen=$(echo "$PUB" | j "_['frozen']") failures=$(echo "$PUB" | j "_['failure_count']")"

# 重复发布必须被冻结拦截
echo "republish -> $(curl -s -o /dev/null -w '%{http_code}' -XPOST $B/products/$PID/execute -H 'Content-Type: application/json' -d "{\"data_snapshot_id\":$SID}")"

# 分页（固定结果快照）
curl -s "$B/results/$PUB_ID/findings?offset=0&limit=2" \
 | python3 -c "import sys,json;d=json.load(sys.stdin);print('page1 total=%d seq=%s'%(d['total'],[i['seq'] for i in d['items']]))"

# 规则升级 v2：金额上限 10000
curl -s -XPOST $B/products/$PID/rules -H 'Content-Type: application/json' -d '{"rule_code":"AMOUNT_RANGE","engine_code":"RANGE","name":"金额0-10000","rule_type":"constraint","table_name":"orders","params":{"column":"amount","min":0,"max":10000}}' >/dev/null
REV=$(curl -s -XPOST $B/products/$PID/replay -H 'Content-Type: application/json' \
  -d "{\"data_snapshot_id\":$SID,\"supersedes_id\":$PUB_ID,\"executor\":\"rule-upgrade\",\"note\":\"放宽金额上限回放\"}")
REV_ID=$(echo "$REV" | j "_['id']")
echo "replay id=$REV_ID kind=$(echo "$REV" | j "_['kind']") failures=$(echo "$REV" | j "_['failure_count']")"
echo "chain kinds: $(curl -s $B/products/$PID/results/$REV_ID/chain | j "[x['kind'] for x in _]")"

# 豁免：用 finding_id 绑定精确样本+规则摘要
FID=$(curl -s "$B/results/$REV_ID/findings?rule_code=AMOUNT_RANGE" | j "_['items'][0]['id']")
EX=$(curl -s -XPOST $B/products/$PID/exemptions -H 'Content-Type: application/json' \
  -d "{\"finding_id\":$FID,\"reason\":\"月末调账已知差异\",\"created_by\":\"steward-li\"}")
echo "exemption id=$(echo "$EX" | j "_['id']") digest=$(echo "$EX" | j "_['rule_digest'][:12]")..."

# 工作台
curl -s "$B/products/$PID/workbench/$REV_ID?limit=2" \
 | python3 -c "import sys,json;d=json.load(sys.stdin);print('workbench result#%s items=%d fixed=%s'%(d['result']['id'],len(d['items']),d['snapshot_fixed']))"

# 凭据：签发、下载、验真、篡改检测
CRED=$(curl -s -XPOST $B/products/$PID/results/$PUB_ID/credential)
REF=$(echo "$CRED" | j "_['credential_ref']")
echo "credential: $REF"
curl -s "$B/results/$PUB_ID/credential/download" | sed 's/^/  /' | head -14
echo "verify: $(curl -s $B/results/$PUB_ID/credential/verify | j "{'valid':_['valid'],'self_consistent':_['seal_self_consistent']}")"

echo
echo "=== 原发布判断回放后仍未改变 ==="
curl -s $B/results/$PUB_ID | j "{'id':_['id'],'kind':_['kind'],'verdict':_['verdict'],'frozen':_['frozen'],'failure_count':_['failure_count']}"
