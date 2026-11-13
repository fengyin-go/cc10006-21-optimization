"""数据检索接口：维护检索索引与「索引影响视图」。

静态路径（概览、命中摘要、检索、版本追溯、导出）必须写在 ``/{entry_id}`` 之前，
否则 FastAPI 会把 ``overview`` 当成整数编号而返回 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, SearchPageResult
from app.services.search import SearchService, WeightRangeError, WEIGHT_LEVELS

router = APIRouter(prefix="/api/search", tags=["数据检索"])

service = SearchService()

LIST_FIELDS = ["索引编号", "索引名称", "覆盖表名", "索引字段", "分词策略", "排序权重", "刷新频率", "索引大小"]
STATUSES = ["草稿", "待构建", "已生效", "已失效"]


@router.get("", response_model=SearchPageResult)
def list_entries(
    keyword: str | None = Query(default=None, description="按索引编号、名称、覆盖表名或字段检索"),
    status: str | None = Query(default=None, description="草稿、待构建、已生效、已失效"),
    table: str | None = Query(default=None, description="按覆盖表名筛选"),
    field: str | None = Query(default=None, description="按索引字段筛选"),
    metadataVersion: int | None = Query(default=None, description="只在指定元数据版本下查看命中"),
    page: int = 1,
    size: int = 20,
) -> SearchPageResult:
    """索引列表 + 同一口径的命中摘要：筛选条件一变，两者一起重算。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")

    items, total, summary = service.view_page(
        keyword=keyword, status=status, table=table, field=field,
        metadata_version=metadataVersion, page=page, size=size,
    )
    return SearchPageResult(
        items=items,
        total=total,
        page=page,
        size=size,
        hitSummary=summary,
        metadataVersionTrace=service.version_trace(5),
        metadataVersion=summary["元数据版本"],
    )


@router.get("/overview", response_model=dict)
def overview(metadataVersion: int | None = Query(default=None)) -> dict[str, Any]:
    """索引影响概览：覆盖表名、索引字段、排序权重并列呈现，概览与列表同一口径。"""
    return service.impact_view(metadata_version=metadataVersion)


@router.get("/impact", response_model=dict)
def impact(
    keyword: str | None = Query(default=None, description="命中字段/表名/索引名称"),
    status: str | None = Query(default=None),
    table: str | None = Query(default=None),
    field: str | None = Query(default=None),
    metadataVersion: int | None = Query(default=None),
) -> dict[str, Any]:
    """命中摘要：与列表概览、索引详情共用同一份计算逻辑。"""
    return service.impact_view(
        keyword=keyword, status=status, table=table, field=field,
        metadata_version=metadataVersion,
    )


@router.get("/hit", response_model=dict)
def hit(
    q: str = Query(..., description="检索关键词，例如 客户编号"),
    metadataVersion: int | None = Query(default=None),
) -> dict[str, Any]:
    """按关键词检索：返回命中表、权重理由，以及可跳转的目录、血缘、质量对象。"""
    try:
        return service.search(q, metadata_version=metadataVersion)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/version-trace", response_model=dict)
def version_trace(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    """元数据版本追溯：记录每次刷新、改权重与视图读取生效的版本。"""
    return {
        "currentVersion": service.current_metadata_version(),
        "items": service.version_trace(limit),
    }


@router.get("/export")
def export_entries(
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    table: str | None = Query(default=None),
    field: str | None = Query(default=None),
) -> dict[str, Any]:
    """导出数据检索清单：导出内容与页面上当前筛选条件保持同一口径。"""
    items, total = service.list_entries(
        keyword=keyword, status=status, table=table, field=field, page=1, size=10000
    )
    return {"module": "search", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条索引明细，附带同一口径的命中摘要；不存在时给出可读说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"检索索引 {entry_id} 不存在或已归档")
    detail = dict(entry)
    detail["命中摘要"] = service.impact_view(field=None, table=str(entry.get("覆盖表名") or ""))
    return detail


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条检索索引，缺字段或权重越界时说明原因而不是静默丢弃。"""
    try:
        entry, missing = service.create_entry(payload.values)
    except WeightRangeError as error:
        return ActionResult(ok=False, message=str(error))
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="检索索引已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """提交构建、启用索引、标记失效；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(
        ok=True,
        message=message,
        entry=entry,
        hitSummary=service.impact_view(),
    )


@router.post("/{entry_id}/weights", response_model=ActionResult)
def update_weights(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改排序权重：非法范围直接拒绝；更新后旧排序立即可失效。"""
    entry, message = service.update_weights(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(
        ok=True,
        message=message,
        entry=entry,
        hitSummary=service.impact_view(),
        data={
            "排序权重": entry.get("排序权重"),
            "排序版本": entry.get("排序版本"),
            "生效元数据版本": entry.get("生效元数据版本"),
            "允许权重": WEIGHT_LEVELS,
        },
    )


@router.post("/{entry_id}/refresh", response_model=ActionResult)
def refresh_index(entry_id: int, payload: EntryPayload) -> ActionResult:
    """刷新索引：清空旧排序、按最后生效版本重建命中摘要，并推进元数据版本。"""
    entry, message = service.refresh_index(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(
        ok=True,
        message=message,
        entry=entry,
        hitSummary=service.impact_view(),
        data={
            "生效版本": entry.get("生效版本"),
            "排序版本": entry.get("排序版本"),
            "生效元数据版本": entry.get("生效元数据版本"),
            "刷新时间": entry.get("刷新时间"),
        },
    )
