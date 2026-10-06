"""端到端测试：版本化执行、回放裁决、豁免绑定、固定分页、凭据、并发冲突。"""
from __future__ import annotations

import threading

import pytest

from .conftest import ORDERS, make_product, make_snapshot, standard_rules


# ---------------------------------------------------------- 1. 固定快照执行

def test_publish_records_sample_locations_and_rule_versions(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)

    r = client.post(f"/api/v1/products/{pid}/execute",
                    json={"data_snapshot_id": sid, "executor": "ci-bot"})
    assert r.status_code == 201, r.text
    result = r.json()
    assert result["kind"] == "publish"
    assert result["verdict"] == "fail"          # 存在 error 级失败
    assert result["frozen"] is True             # 发布即冻结
    assert result["total_rules"] == 5
    assert result["failure_count"] >= 4

    page = client.get(f"/api/v1/results/{result['id']}/findings?limit=50").json()
    assert page["snapshot_fixed"] is True
    items = page["items"]
    # 每条失败都带精确样本定位与规则版本
    for f in items:
        assert f["table_name"] == "orders"
        assert f["row_key"]  # 稳定业务主键 JSON
        assert f["rule_version"] == 1
        assert len(f["rule_digest"]) == 64

    # 具体失败样本定位正确
    locs = {(f["rule_code"], f["row_key"], f["column_name"]) for f in items}
    assert ("ORDER_CUSTOMER_NOT_NULL", '{"id":"O-3"}', "customer_id") in locs
    assert ("ORDER_AMOUNT_RANGE", '{"id":"O-2"}', "amount") in locs
    assert ("ORDER_STATUS_ENUM", '{"id":"O-3"}', "status") in locs


def test_warning_does_not_fail_publish(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    # 只有 warning 级邮箱规则会失败
    r = client.post(f"/api/v1/products/{pid}/rules", json={
        "rule_code": "ORDER_EMAIL_REGEX", "engine_code": "REGEX",
        "name": "邮箱", "rule_type": "field",
        "table_name": "orders",
        "params": {"column": "email", "pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"},
        "severity": "warning",
    })
    assert r.status_code == 201
    res = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()
    assert res["failure_count"] == 1
    assert res["verdict"] == "pass"


# ---------------------------------------------------------- 2. 发布冻结不可重判

def test_frozen_publish_cannot_be_reexecuted(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)
    res = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()

    again = client.post(f"/api/v1/products/{pid}/execute",
                        json={"data_snapshot_id": sid})
    assert again.status_code == 409
    assert "frozen" in again.json()["detail"]

    # 冻结幂等
    fr = client.post(f"/api/v1/products/{pid}/results/{res['id']}/freeze")
    assert fr.status_code == 200
    assert fr.json()["frozen"] is True


# ---------------------------------------------------------- 3. 规则升级回放只形成修订版

def test_rule_upgrade_replay_creates_revision_without_overwriting(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)

    pub = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()
    original_verdict, original_failures = pub["verdict"], pub["failure_count"]
    original_created = pub["created_at"]

    # 规则升级：金额上限放宽到 10000（v2，摘要改变）
    r2 = client.post(f"/api/v1/products/{pid}/rules", json={
        "rule_code": "ORDER_AMOUNT_RANGE", "engine_code": "RANGE", "name": "金额范围v2",
        "rule_type": "constraint", "table_name": "orders",
        "params": {"column": "amount", "min": 0, "max": 10000},
    }).json()
    assert r2["version"] == 2
    assert r2["rule_digest"] != client.get(
        f"/api/v1/products/{pid}/rules", params={"rule_code": "ORDER_AMOUNT_RANGE"}
    ).json()[0]["rule_digest"]

    replay = client.post(f"/api/v1/products/{pid}/replay", json={
        "data_snapshot_id": sid, "supersedes_id": pub["id"],
        "executor": "rule-upgrade-bot", "note": "放宽金额上限后回放",
    })
    assert replay.status_code == 201, replay.text
    rev = replay.json()
    assert rev["kind"] == "replay"
    assert rev["supersedes_id"] == pub["id"]
    assert rev["frozen"] is False
    assert rev["rule_set_sha256"] != pub["rule_set_sha256"]
    # O-3 金额 9999 在 v2(<=10000) 下不再失败；O-2 的 -5 仍低于 min
    range_findings = client.get(
        f"/api/v1/results/{rev['id']}/findings",
        params={"rule_code": "ORDER_AMOUNT_RANGE"}).json()["items"]
    assert {f["row_key"] for f in range_findings} == {'{"id":"O-2"}'}
    assert rev["failure_count"] == original_failures - 1
    assert all(f["rule_version"] == 2 for f in range_findings)

    # 原发布判断原封不动
    pub_after = client.get(f"/api/v1/results/{pub['id']}").json()
    assert pub_after["verdict"] == original_verdict
    assert pub_after["failure_count"] == original_failures
    assert pub_after["created_at"] == original_created
    assert pub_after["frozen"] is True

    # 裁决链：发布 -> 修订
    chain = client.get(f"/api/v1/products/{pid}/results/{rev['id']}/chain").json()
    assert [c["kind"] for c in chain] == ["publish", "replay"]


def test_replay_requires_existing_base(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)
    r = client.post(f"/api/v1/products/{pid}/replay", json={
        "data_snapshot_id": sid, "supersedes_id": 9999})
    assert r.status_code == 409


# ---------------------------------------------------------- 4. 豁免绑定精确样本+规则摘要

def test_exemption_must_bind_precise_sample_and_digest(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)
    pub = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()

    # 找到 O-2 金额越界的失败
    findings = client.get(
        f"/api/v1/results/{pub['id']}/findings",
        params={"rule_code": "ORDER_AMOUNT_RANGE"}).json()["items"]
    target = next(f for f in findings if f["row_key"] == '{"id":"O-2"}')

    # 缺样本坐标 -> 拒绝
    bad = client.post(f"/api/v1/products/{pid}/exemptions", json={
        "data_snapshot_id": sid, "rule_code": "ORDER_AMOUNT_RANGE",
        "rule_version": 1, "reason": "no sample"})
    assert bad.status_code == 400

    # 样本不存在 -> 拒绝
    ghost = client.post(f"/api/v1/products/{pid}/exemptions", json={
        "data_snapshot_id": sid, "table_name": "orders",
        "row_key": {"id": "O-999"}, "rule_code": "ORDER_AMOUNT_RANGE",
        "rule_version": 1, "reason": "ghost"})
    assert ghost.status_code == 422

    # 规则版本不存在 -> 拒绝
    norule = client.post(f"/api/v1/products/{pid}/exemptions", json={
        "data_snapshot_id": sid, "table_name": "orders",
        "row_key": {"id": "O-2"}, "rule_code": "ORDER_AMOUNT_RANGE",
        "rule_version": 99, "reason": "x"})
    assert norule.status_code == 404

    # 通过 finding_id 精确绑定 -> 成功
    ok = client.post(f"/api/v1/products/{pid}/exemptions", json={
        "finding_id": target["id"], "reason": "月末调账已知差异"})
    assert ok.status_code == 201, ok.text
    ex = ok.json()
    assert ex["row_key"] == '{"id":"O-2"}'
    assert len(ex["rule_digest"]) == 64

    # 重复豁免同一 精确样本×规则摘要 -> 冲突
    dup = client.post(f"/api/v1/products/{pid}/exemptions", json={
        "finding_id": target["id"], "reason": "again"})
    assert dup.status_code == 409


def test_exemption_does_not_mutate_prior_snapshot_but_applies_to_replay(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)
    pub = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()

    findings = client.get(
        f"/api/v1/results/{pub['id']}/findings",
        params={"rule_code": "ORDER_AMOUNT_RANGE"}).json()["items"]
    target = next(f for f in findings if f["row_key"] == '{"id":"O-2"}')
    assert target["exempt_at_execution"] is False

    client.post(f"/api/v1/products/{pid}/exemptions",
                json={"finding_id": target["id"], "reason": "approved"})

    # 固定结果快照不被改写：旧分页里仍然 outstanding
    old_page = client.get(f"/api/v1/results/{pub['id']}/findings",
                          params={"exempt": "outstanding"}).json()
    assert any(i["id"] == target["id"] for i in old_page["items"])

    # 回放时该失败在执行时刻被标记为豁免
    rev = client.post(f"/api/v1/products/{pid}/replay", json={
        "data_snapshot_id": sid, "supersedes_id": pub["id"]}).json()
    new_findings = client.get(
        f"/api/v1/results/{rev['id']}/findings",
        params={"rule_code": "ORDER_AMOUNT_RANGE"}).json()["items"]
    new_target = next(f for f in new_findings if f["row_key"] == '{"id":"O-2"}')
    assert new_target["exempt_at_execution"] is True

    exempted = client.get(f"/api/v1/results/{rev['id']}/findings",
                          params={"exempt": "exempted"}).json()
    assert any(i["id"] == new_target["id"] for i in exempted["items"])


def test_upgraded_rule_digest_is_not_covered_by_old_exemption(client):
    """规则升级后摘要变化：旧豁免不覆盖新规则版本对同一样本的失败。"""
    pid = make_product(client)
    # O-3 金额 9999 在 v1(<=1000) 失败
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)
    pub = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()
    f_v1 = next(f for f in client.get(
        f"/api/v1/results/{pub['id']}/findings",
        params={"rule_code": "ORDER_AMOUNT_RANGE"}).json()["items"]
        if f["row_key"] == '{"id":"O-3"}')
    client.post(f"/api/v1/products/{pid}/exemptions",
                json={"finding_id": f_v1["id"], "reason": "legacy tolerance"})

    # 升级为更严格 v2（max=5000，O-3 依旧失败但摘要是新的）
    client.post(f"/api/v1/products/{pid}/rules", json={
        "rule_code": "ORDER_AMOUNT_RANGE", "engine_code": "RANGE", "name": "金额v2",
        "rule_type": "constraint", "table_name": "orders",
        "params": {"column": "amount", "min": 0, "max": 5000}})
    rev = client.post(f"/api/v1/products/{pid}/replay", json={
        "data_snapshot_id": sid, "supersedes_id": pub["id"]}).json()
    f_v2 = next(f for f in client.get(
        f"/api/v1/results/{rev['id']}/findings",
        params={"rule_code": "ORDER_AMOUNT_RANGE"}).json()["items"]
        if f["row_key"] == '{"id":"O-3"}')
    assert f_v2["rule_version"] == 2
    assert f_v2["rule_digest"] != f_v1["rule_digest"]
    assert f_v2["exempt_at_execution"] is False  # 旧豁免不覆盖升级后的规则


def test_revoke_exemption(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)
    pub = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()
    fid = client.get(f"/api/v1/results/{pub['id']}/findings").json()["items"][0]["id"]
    ex = client.post(f"/api/v1/products/{pid}/exemptions",
                     json={"finding_id": fid, "reason": "temporary"}).json()
    r = client.post(f"/api/v1/products/{pid}/exemptions/{ex['id']}/revoke")
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"
    again = client.post(f"/api/v1/products/{pid}/exemptions/{ex['id']}/revoke")
    assert again.status_code == 409


# ---------------------------------------------------------- 5. 固定快照分页

def test_pagination_reads_fixed_snapshot(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)
    pub = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()

    p1 = client.get(f"/api/v1/results/{pub['id']}/findings",
                    params={"offset": 0, "limit": 2}).json()
    p2 = client.get(f"/api/v1/results/{pub['id']}/findings",
                    params={"offset": 2, "limit": 2}).json()
    assert p1["total"] == pub["failure_count"]
    ids1 = [i["id"] for i in p1["items"]]
    ids2 = [i["id"] for i in p2["items"]]
    assert not (set(ids1) & set(ids2))           # 页间不重不漏
    assert [i["seq"] for i in p1["items"]] == [0, 1]
    assert [i["seq"] for i in p2["items"]] == [2, 3]

    # 即使之后签发豁免/回放，旧分页 total 与内容不变
    fid = p1["items"][0]["id"]
    client.post(f"/api/v1/products/{pid}/exemptions",
                json={"finding_id": fid, "reason": "x"})
    client.post(f"/api/v1/products/{pid}/replay", json={
        "data_snapshot_id": sid, "supersedes_id": pub["id"]})
    p1_again = client.get(f"/api/v1/results/{pub['id']}/findings",
                          params={"offset": 0, "limit": 2}).json()
    assert [i["id"] for i in p1_again["items"]] == ids1
    assert p1_again["total"] == p1["total"]


def test_workbench_pages_and_annotates_current_exemptions(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)
    pub = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()
    wb = client.get(f"/api/v1/products/{pid}/workbench/{pub['id']}").json()
    assert wb["result"]["id"] == pub["id"]
    assert all(i["current_exemption_id"] is None for i in wb["items"])

    fid = wb["items"][0]["id"]
    ex = client.post(f"/api/v1/products/{pid}/exemptions",
                     json={"finding_id": fid, "reason": "ok"}).json()
    wb2 = client.get(f"/api/v1/products/{pid}/workbench/{pub['id']}").json()
    annotated = next(i for i in wb2["items"] if i["id"] == fid)
    assert annotated["current_exemption_id"] == ex["id"]
    # 执行时刻状态仍冻结
    assert annotated["exempt_at_execution"] is False


# ---------------------------------------------------------- 6. 数据修复派生新快照

def test_repair_derives_new_immutable_snapshot(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)
    pub = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()
    assert pub["verdict"] == "fail"

    # 修复：补客户、改正金额、修正状态、删除坏邮箱行不可能（email 是 warning）
    repaired = client.post(f"/api/v1/products/{pid}/snapshots/repair", json={
        "source_snapshot_id": sid,
        "new_snapshot_ref": "snap-1-fixed",
        "edits": {
            "orders": [
                {"id": "O-2", "customer_id": "C-2", "amount": 10,
                 "status": "paid", "email": "b@x.com"},
                {"id": "O-3", "customer_id": "C-1", "amount": 50,
                 "status": "paid", "email": "c@x.com"},
            ],
        },
        "note": "修复三条问题订单",
    })
    assert repaired.status_code == 201, repaired.text
    new_sid = repaired.json()["id"]
    assert repaired.json()["origin"] == "repair"
    assert repaired.json()["parent_id"] == sid

    # 旧快照内容不变
    old_rows = client.get(f"/api/v1/snapshots/{sid}/rows",
                          params={"table": "orders"}).json()["orders"]
    o3_old = next(r[1] for r in old_rows if r[0] == '{"id":"O-3"}')
    assert o3_old["status"] == "weird"

    # 对修复快照执行 -> 通过（warning 不否决）
    pub2 = client.post(f"/api/v1/products/{pid}/execute",
                       json={"data_snapshot_id": new_sid}).json()
    assert pub2["verdict"] == "pass"
    assert pub2["failure_count"] == 0


def test_repair_with_delete(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    rep = client.post(f"/api/v1/products/{pid}/snapshots/repair", json={
        "source_snapshot_id": sid, "new_snapshot_ref": "snap-del",
        "deletes": {"orders": [{"id": "O-3"}]},
    }).json()
    rows = client.get(f"/api/v1/snapshots/{rep['id']}/rows",
                      params={"table": "orders"}).json()["orders"]
    assert {r[0] for r in rows} == {'{"id":"O-1"}', '{"id":"O-2"}'}


# ---------------------------------------------------------- 7. 质量凭据

def test_credential_issue_download_verify_and_tamper_detection(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)
    pub = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()

    issue = client.post(
        f"/api/v1/products/{pid}/results/{pub['id']}/credential")
    assert issue.status_code == 201, issue.text
    cred = issue.json()
    assert cred["credential_ref"].startswith("QC-")
    assert "file_path" not in cred

    dl = client.get(cred["download_url"])
    assert dl.status_code == 200
    text = dl.text
    assert "DATA QUALITY CREDENTIAL" in text
    assert "Verdict:          FAIL" in text
    assert "ORDER_CUSTOMER_NOT_NULL v1" in text
    assert "Seal SHA-256:" in text

    verify = client.get(
        f"/api/v1/results/{pub['id']}/credential/verify").json()
    assert verify["valid"] is True

    # 篡改磁盘文件 -> 验真失败
    from app import config
    path = config.CREDENTIAL_DIR / f"{cred['credential_ref']}.txt"
    path.write_text(text.replace("FAIL", "PASS"), encoding="utf-8")
    bad = client.get(f"/api/v1/results/{pub['id']}/credential/verify").json()
    assert bad["valid"] is False

    # 幂等签发：同一结果快照凭据唯一
    again = client.post(
        f"/api/v1/products/{pid}/results/{pub['id']}/credential").json()
    assert again["credential_ref"] == cred["credential_ref"]


def test_replay_credential_marked_as_revision(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)
    pub = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()
    rev = client.post(f"/api/v1/products/{pid}/replay", json={
        "data_snapshot_id": sid, "supersedes_id": pub["id"]}).json()
    client.post(f"/api/v1/products/{pid}/results/{rev['id']}/credential")
    text = client.get(
        f"/api/v1/results/{rev['id']}/credential/download").text
    assert "kind=replay" in text
    assert "revision; original judgment unchanged" in text


# ---------------------------------------------------------- 8. 跨表与 SQL 断言

def test_referential_integrity_and_sql_assertion(client):
    pid = make_product(client)
    # O-9 引用不存在的客户
    bad_orders = ORDERS + [
        {"id": "O-9", "customer_id": "C-X", "amount": 1,
         "status": "paid", "email": "x@x.com"}]
    sid = make_snapshot(client, pid, orders=bad_orders)
    standard_rules(client, pid)
    r = client.post(f"/api/v1/products/{pid}/rules", json={
        "rule_code": "HIGH_VALUE_PAIR", "engine_code": "SQL_ASSERTION",
        "name": "高额单须金级客户",
        "rule_type": "cross_table", "table_name": "orders",
        "params": {"sql": (
            "SELECT o.__row_key AS row_key, 'orders' AS table_name,"
            " 'amount' AS column_name, o.amount AS observed_value,"
            " 'high value order needs gold customer' AS message "
            "FROM orders o JOIN customers c ON o.customer_id = c.id "
            "WHERE o.amount > 50 AND c.tier <> 'gold'")},
    })
    assert r.status_code == 201, r.text
    res = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()
    items = client.get(f"/api/v1/results/{res['id']}/findings").json()["items"]
    fk = [f for f in items if f["rule_code"] == "ORDER_FK_CUSTOMER"]
    assert any(f["row_key"] == '{"id":"O-9"}' for f in fk)
    sql_f = [f for f in items if f["rule_code"] == "HIGH_VALUE_PAIR"]
    # O-2(-5 不算), O-1(100/C-1 gold 不算) -> O-3 customer 为空 join 不上 -> 0 行
    assert sql_f == []


def test_invalid_rule_definitions_rejected(client):
    pid = make_product(client)
    for bad in [
        {"rule_code": "R1", "engine_code": "NOPE", "name": "x",
         "rule_type": "field", "table_name": "orders", "params": {}},
        {"rule_code": "R2", "engine_code": "REGEX", "name": "x",
         "rule_type": "field", "table_name": "orders",
         "params": {"column": "email", "pattern": "("}},
        {"rule_code": "R3", "engine_code": "ENUM", "name": "x",
         "rule_type": "field", "table_name": "orders",
         "params": {"column": "status", "values": []}},
        {"rule_code": "R4", "engine_code": "REFERENTIAL_INTEGRITY", "name": "x",
         "rule_type": "cross_table", "table_name": "orders",
         "params": {"child_columns": ["a"], "parent_table": "customers"}},
    ]:
        r = client.post(f"/api/v1/products/{pid}/rules", json=bad)
        assert r.status_code == 422, bad


# ---------------------------------------------------------- 9. 并发裁决

def test_concurrent_repair_replay_freeze_exemption_serialize(client):
    """四类操作并发：全部成功或明确冲突，绝不出现脏写/双发布。"""
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)
    pub = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()
    fid = client.get(f"/api/v1/results/{pub['id']}/findings").json()["items"][0]["id"]

    errors: list[Exception] = []

    def do_repair():
        try:
            r = client.post(f"/api/v1/products/{pid}/snapshots/repair", json={
                "source_snapshot_id": sid,
                "new_snapshot_ref": f"concurrent-repair-{threading.get_ident()}",
                "edits": {"orders": [{"id": "O-2", "customer_id": "C-2",
                                      "amount": 10, "status": "paid",
                                      "email": "b@x.com"}]}})
            assert r.status_code in (201, 409), r.text
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def do_replay():
        try:
            r = client.post(f"/api/v1/products/{pid}/replay", json={
                "data_snapshot_id": sid, "supersedes_id": pub["id"],
                "note": f"replay-{threading.get_ident()}"})
            assert r.status_code in (201, 409), r.text
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def do_freeze():
        try:
            r = client.post(f"/api/v1/products/{pid}/results/{pub['id']}/freeze")
            assert r.status_code in (200, 409), r.text
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def do_exempt():
        try:
            r = client.post(f"/api/v1/products/{pid}/exemptions", json={
                "finding_id": fid,
                "reason": f"concurrent-{threading.get_ident()}"})
            assert r.status_code in (201, 409), r.text
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=t) for t in
               (do_repair, do_replay, do_freeze, do_exempt)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # 不变量：发布结果仍只有一份且冻结
    results = client.get(f"/api/v1/products/{pid}/results",
                         params={"data_snapshot_id": sid}).json()
    publishes = [r for r in results if r["kind"] == "publish"]
    assert len(publishes) == 1
    assert publishes[0]["frozen"] is True


def test_concurrent_duplicate_exemption_single_winner(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)
    pub = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()
    fid = client.get(f"/api/v1/results/{pub['id']}/findings").json()["items"][0]["id"]

    outcomes: list[int] = []

    def grant():
        r = client.post(f"/api/v1/products/{pid}/exemptions",
                        json={"finding_id": fid, "reason": "race"})
        outcomes.append(r.status_code)

    threads = [threading.Thread(target=grant) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count(201) == 1
    assert outcomes.count(409) == 3


# ---------------------------------------------------------- 10. 规则版本生命周期

def test_rule_versions_and_deprecation(client):
    pid = make_product(client)
    sid = make_snapshot(client, pid)
    standard_rules(client, pid)
    # 弃用金额规则后执行，该规则不参与
    client.post(f"/api/v1/products/{pid}/rules/ORDER_AMOUNT_RANGE/deprecate")
    pub = client.post(f"/api/v1/products/{pid}/execute",
                      json={"data_snapshot_id": sid}).json()
    items = client.get(f"/api/v1/results/{pub['id']}/findings").json()["items"]
    assert all(f["rule_code"] != "ORDER_AMOUNT_RANGE" for f in items)

    versions = client.get(
        f"/api/v1/products/{pid}/rules",
        params={"rule_code": "ORDER_AMOUNT_RANGE"}).json()
    assert [v["version"] for v in versions] == [1]
    assert versions[0]["status"] == "deprecated"


def test_duplicate_snapshot_ref_conflict(client):
    pid = make_product(client)
    make_snapshot(client, pid, ref="same")
    r = client.post(f"/api/v1/products/{pid}/snapshots", json={
        "snapshot_ref": "same",
        "tables": {"orders": [{"id": "O-1"}]},
        "key_columns": {"orders": ["id"]}})
    assert r.status_code == 409
