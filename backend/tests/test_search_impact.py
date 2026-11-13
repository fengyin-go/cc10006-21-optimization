"""检索索引影响视图的接口测试。

覆盖需求里的关键口径：
- 三个入口（列表、概览、命中）返回同一份命中摘要
- 覆盖表名、索引字段、排序权重并列呈现，权重理由可读
- 权重范围校验、重复索引以最后生效版本为准
- 改权重/刷新后不残留旧排序，元数据版本变化时摘要同步更新
- 目录业务面的待办随索引刷新更新，落表可跳转目录/血缘/质量
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import store


@pytest.fixture()
def client() -> TestClient:
    # 每个用例都从干净的示例数据开始，避免用例之间互相影响排序
    store.reset()
    return TestClient(app)


def hit_ids(summary: dict) -> list[tuple[str, int]]:
    return [(item["索引编号"], item["命中分"]) for item in summary["命中列表"]]


def test_three_entries_share_one_hit_summary(client: TestClient) -> None:
    page = client.get("/api/search", params={"keyword": "客户编号"}).json()
    overview = client.get("/api/search/overview").json()
    impact = client.get("/api/search/impact", params={"keyword": "客户编号"}).json()
    hit = client.get("/api/search/hit", params={"q": "客户编号"}).json()

    assert hit_ids(page["hitSummary"]) == hit_ids(impact)
    assert hit_ids(hit["命中摘要"]) == hit_ids(impact)
    # 概览不带关键词时按全字段权重计分，命中表与权重口径来自同一套计算
    overview_tables = [item["覆盖表名"] for item in overview["命中列表"]]
    assert overview_tables[0] == "order_silver"
    assert set(overview_tables) == {
        "order_silver",
        "customer_gold",
        "refund_silver",
        "product_silver",
    }

    first = impact["命中列表"][0]
    assert first["覆盖表名"] == "order_silver"
    assert first["排序权重"]["业务域字段"] == "高"
    assert first["排序权重"]["时间字段"] == "中"
    assert "客户编号" in first["命中字段"]
    assert "业务域字段" in first["权重理由"]


def test_invalid_weight_range_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/search/1/weights",
        json={"values": {"排序权重": {"时间字段": "极高"}}},
    ).json()
    assert response["ok"] is False
    assert "不在允许范围" in response["message"]


def test_weight_change_clears_old_order_and_bumps_version(client: TestClient) -> None:
    before = client.get("/api/search", params={"keyword": "客户编号"}).json()["hitSummary"]
    assert [item["索引编号"] for item in before["命中列表"]] == ["SEAR-0001", "SEAR-0004"]
    assert before["元数据版本"] == 1

    result = client.post(
        "/api/search/4/weights",
        json={"values": {"排序权重": {"业务域字段": "高"}}},
    ).json()
    assert result["ok"] is True
    assert result["data"]["排序版本"] == 2
    assert result["data"]["生效元数据版本"] == 2

    after = client.get("/api/search", params={"keyword": "客户编号"}).json()["hitSummary"]
    order = {item["索引编号"]: item["命中分"] for item in after["命中列表"]}
    assert order["SEAR-0004"] == 3  # 新的高权重立刻生效
    assert after["元数据版本"] == 2

    # 旧版本仍能追溯当时的排序，不被新权重污染
    old = client.get(
        "/api/search/hit", params={"q": "客户编号", "metadataVersion": 1}
    ).json()
    old_scores = {item["索引编号"]: item["命中分"] for item in old["命中摘要"]["命中列表"]}
    assert old_scores["SEAR-0004"] == 2


def test_refresh_rebuilds_order_and_updates_catalog_demand(client: TestClient) -> None:
    result = client.post("/api/search/1/refresh", json={"values": {}}).json()
    assert result["ok"] is True
    assert result["data"]["生效版本"] == 3
    assert result["data"]["生效元数据版本"] >= 2

    summary = client.get("/api/search", params={"keyword": "客户编号"}).json()["hitSummary"]
    assert summary["元数据版本"] == result["data"]["生效元数据版本"]
    assert {item["生效版本"] for item in summary["命中列表"] if item["索引编号"] == "SEAR-0001"} == {3}

    catalog = client.get("/api/catalog", params={"table": "order_silver"}).json()
    row = catalog["items"][0]
    assert row["索引同步版本"] == 3
    assert row["待办事项"] == ""
    assert row["pending"] is False


def test_duplicate_index_uses_last_effective_version(client: TestClient) -> None:
    # SEAR-0002 覆盖同一张 order_silver，排序版本更低，应被 SEAR-0001 覆盖
    summary = client.get("/api/search", params={"keyword": "客户编号"}).json()["hitSummary"]
    tables = [item["覆盖表名"] for item in summary["命中列表"]]
    assert tables.count("order_silver") == 1
    kept = [item for item in summary["命中列表"] if item["覆盖表名"] == "order_silver"][0]
    assert kept["索引编号"] == "SEAR-0001"
    assert any(note["被覆盖索引"] == "SEAR-0002" for note in summary["重复索引"])


def test_hit_exposes_links_to_catalog_lineage_quality(client: TestClient) -> None:
    hit = client.get("/api/search/hit", params={"q": "客户编号"}).json()
    order = hit["生效命中"][0]
    links = order["关联对象"]
    assert [item["关联表名"] for item in links["目录"]] == ["order_silver"]
    assert [item["关联表名"] for item in links["血缘"]] == ["order_silver"]
    assert [item["关联表名"] for item in links["质量"]] == ["order_silver"]
    assert links["目录"][0]["跳转"].startswith("/catalog?table=order_silver")
