"""数据检索业务规则：索引影响视图、命中摘要、权重校验与刷新口径都收在这里。

设计要点：
- 三个入口（列表概览、命中摘要、索引详情）都调用同一个 ``impact_view``，
  因此覆盖表名、索引字段、排序权重与命中理由不会各说各话。
- 权重只认当前生效版本（按 ``排序版本`` 取最后生效的那份），刷新时先清空旧排序
  再按新权重重算，避免残留上一次的顺序。
- 每次刷新/改权重都会推进索引版本与元数据版本，影响摘要和版本追溯一起更新。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from app.store import store

MODULE = "search"
REQUIRED_FIELDS = ["索引编号", "索引名称", "覆盖表名"]
STATUS_ORDER = ["草稿", "待构建", "已生效", "已失效"]
ACTION_RULES = {"提交构建": "待构建", "启用索引": "已生效", "标记失效": "已失效"}
NEGATIVE_ACTIONS = ["标记失效"]

WEIGHT_LEVELS = ["高", "中", "低", "关闭"]
WEIGHT_SCORES = {"高": 3, "中": 2, "低": 1, "关闭": 0}
WEIGHT_LABELS = {"高": "高（+3）", "中": "中（+2）", "低": "低（+1）", "关闭": "关闭（0）"}
FIELD_GROUPS = ["业务域字段", "时间字段", "度量字段"]
TZ = timezone(timedelta(hours=8))


class WeightRangeError(ValueError):
    """权重取值不在允许范围时抛出：由调用方转成可读的拒绝说明。"""

    def __init__(self, field: str, level: str) -> None:
        self.field = field
        self.level = level
        super().__init__(
            f"字段「{field}」的排序权重「{level}」不在允许范围"
            f"（{'、'.join(WEIGHT_LEVELS)}）内"
        )


def _now() -> str:
    return datetime.now(TZ).replace(microsecond=0).isoformat()


def _as_version(value: Any, fallback: int = 0) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return fallback


def split_fields(value: Any) -> list[str]:
    """索引字段支持逗号、顿号、分号与空白分隔，统一拆成字段名列表。"""
    if isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        text = str(value or "")
        for sep in ["，", "、", ";", "；", "|"]:
            text = text.replace(sep, ",")
        parts = text.replace("\n", ",").split(",")
    return [part.strip() for part in parts if part.strip()]


def _infer_group(field: str) -> str:
    if "时间" in field or "日期" in field:
        return "时间字段"
    if "金额" in field or "数量" in field or "度量" in field:
        return "度量字段"
    return "业务域字段"


def _field_groups_for(index: dict[str, Any]) -> dict[str, str]:
    """读取索引的字段分类；老数据没写分类时退化为按字段名归类。"""
    raw = index.get("字段分类")
    if isinstance(raw, dict) and raw:
        return {str(field): str(group) for field, group in raw.items()}
    return {field: _infer_group(field) for field in split_fields(index.get("索引字段"))}


def _coerce_weight_map(raw: Any) -> dict[str, str]:
    """把 "业务域字段=高,时间字段=中" 或 dict 统一成 dict。"""
    if isinstance(raw, dict):
        return {str(key).strip(): str(level).strip() for key, level in raw.items()}
    if not isinstance(raw, str):
        raise WeightRangeError("排序权重", str(raw))
    parsed: dict[str, str] = {}
    for chunk in raw.replace("，", ",").split(","):
        if not chunk.strip():
            continue
        key, _, value = chunk.partition("=")
        if not value:
            key, _, value = chunk.partition(":")
        if key.strip() and value.strip():
            parsed[key.strip()] = value.strip()
    return parsed


def normalize_weights(index: dict[str, Any]) -> dict[str, str]:
    """把索引上的排序权重收敛到「字段分类 -> 高/中/低/关闭」。

    兼容两种写法：按字段分类给权重（``{"业务域字段": "高"}``），或直接按字段给权重
    （``{"客户编号": "高"}``）。非法权重不会被静默当成默认值，而是抛出可读的错误。
    """
    raw = index.get("排序权重")
    if raw is None or raw == "":
        return {group: "低" for group in FIELD_GROUPS}
    if isinstance(raw, str) and not any(sep in raw for sep in ["=", ":", "："]):
        # 只给了一个级别时，视为业务域字段的权重
        raw = {"业务域字段": raw.strip()}
    weights = _coerce_weight_map(raw)

    groups = _field_groups_for(index)
    normalized: dict[str, str] = {group: "低" for group in FIELD_GROUPS}
    for key, level in weights.items():
        if level not in WEIGHT_LEVELS:
            raise WeightRangeError(key, level)
        group = key if key in FIELD_GROUPS else groups.get(key, "业务域字段")
        normalized[group] = level
    return normalized


def weight_breakdown(index: dict[str, Any]) -> list[dict[str, Any]]:
    """按字段分类展开权重：给出分值、显示文案与覆盖到的字段。"""
    groups = _field_groups_for(index)
    weights = normalize_weights(index)
    by_group: dict[str, list[str]] = {group: [] for group in FIELD_GROUPS}
    for field, group in groups.items():
        by_group.setdefault(group, []).append(field)
    breakdown: list[dict[str, Any]] = []
    for group in FIELD_GROUPS:
        level = weights.get(group, "低")
        breakdown.append({
            "字段分类": group,
            "权重": level,
            "权重分值": WEIGHT_SCORES[level],
            "权重说明": WEIGHT_LABELS[level],
            "字段": sorted(by_group.get(group, [])),
        })
    return breakdown


SNAPSHOT_FIELDS = (
    "排序权重",
    "字段分类",
    "排序版本",
    "生效版本",
    "刷新时间",
    "生效元数据版本",
)


def snapshot(index: dict[str, Any]) -> dict[str, Any]:
    """给索引当前生效状态留一份带元数据版本的快照，供版本回溯。"""
    return {
        field: deepcopy(index.get(field))
        for field in SNAPSHOT_FIELDS
    }


def row_at_version(index: dict[str, Any], version: int | None) -> dict[str, Any]:
    """返回索引在指定元数据版本下生效的配置。

    有版本历史时按 ``生效元数据版本 <= version`` 取最后一份；
    版本早于全部历史则取最早一份，保证老版本也能看到当时的排序。
    """
    if version is None:
        return index
    history = index.get("版本历史")
    if not isinstance(history, list) or not history:
        return index
    ordered = sorted(
        history,
        key=lambda item: _as_version(item.get("生效元数据版本"), 1),
    )
    effective = ordered[0]
    for item in ordered:
        if _as_version(item.get("生效元数据版本"), 1) <= version:
            effective = item
        else:
            break
    resolved = dict(index)
    resolved.update(effective)
    return resolved


class SearchService:
    # ---------- 筛选与读取 ----------

    def view_page(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        table: str | None = None,
        field: str | None = None,
        metadata_version: int | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
        """一次算出列表与命中摘要，保证两者使用同一批行、同一份排序。"""
        rows = self._filtered_rows(
            keyword=keyword,
            status=status,
            table=table,
            field=field,
            metadata_version=metadata_version,
        )
        if keyword or field:
            ranked, _, duplicates = self._ranked_hits(
                rows, keyword=keyword, field=field, metadata_version=metadata_version
            )
        else:
            ranked = self._overview_hits(rows, metadata_version)
            duplicates = self._effective_hits(
                [self._hit_detail(row, keyword=None, searched_field=None) for row in rows]
            )[1]
        # 列表条目按命中摘要的同一份排序输出，页面上看到的名次与卡片一致
        by_id = {row.get("id"): row for row in rows}
        ordered = [by_id[hit["id"]] for hit in ranked if hit["id"] in by_id]
        start = max(page - 1, 0) * size
        items = ordered[start:start + size]
        self._record_metadata_version(metadata_version)
        summary = self._summary(ranked, rows, keyword, metadata_version)
        summary["重复索引"] = duplicates
        return items, len(ordered), summary

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        table: str | None = None,
        field: str | None = None,
        metadata_version: int | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        items, total, _ = self.view_page(
            keyword=keyword,
            status=status,
            table=table,
            field=field,
            metadata_version=metadata_version,
            page=page,
            size=size,
        )
        return items, total

    def _filtered_rows(
        self,
        *,
        keyword: str | None,
        status: str | None,
        table: str | None,
        field: str | None,
        metadata_version: int | None = None,
    ) -> list[dict[str, Any]]:
        """列表与命中摘要共用同一套过滤口径，避免两处结果对不上。

        指定 ``metadata_version`` 时，每行先还原成该版本当时生效的权重配置再参与计算，
        因此版本变化后的摘要不会混用新权重。
        """
        rows = self._visible_rows()
        if metadata_version is not None:
            rows = [row_at_version(row, metadata_version) for row in rows]
        if keyword:
            rows = [row for row in rows if self._matches_keyword(row, keyword)]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if table:
            rows = [row for row in rows if table in str(row.get("覆盖表名", ""))]
        if field:
            rows = [row for row in rows if any(field in f for f in split_fields(row.get("索引字段")))]
        return rows

    def _visible_rows(self) -> list[dict[str, Any]]:
        """已失效的索引不再参与命中和排序，避免旧版本继续影响结果。"""
        return [row for row in store.rows(MODULE) if row.get("status") != "已失效"]

    def _all_rows(self) -> list[dict[str, Any]]:
        return store.rows(MODULE)

    def _matches_keyword(self, index: dict[str, Any], keyword: str) -> bool:
        if keyword in str(index.get("索引编号", "")) or keyword in str(index.get("索引名称", "")):
            return True
        if keyword in str(index.get("覆盖表名", "")):
            return True
        return any(keyword in field for field in split_fields(index.get("索引字段")))

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    # ---------- 索引影响视图 ----------

    def impact_view(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        table: str | None = None,
        field: str | None = None,
        metadata_version: int | None = None,
    ) -> dict[str, Any]:
        """统一的命中摘要：概览、列表与命中接口都从这里取数，口径完全一致。"""
        _, _, summary = self.view_page(
            keyword=keyword,
            status=status,
            table=table,
            field=field,
            metadata_version=metadata_version,
            page=1,
            size=10000,
        )
        return summary

    def _overview_hits(
        self, rows: list[dict[str, Any]], metadata_version: int | None
    ) -> list[dict[str, Any]]:
        """没有筛选条件时的概览：按全字段权重计分，去重后仍按权重排序。"""
        hits = [
            self._hit_detail(row, keyword=None, searched_field=None)
            for row in rows
        ]
        effective, _ = self._effective_hits(hits)
        return self._cached_rank(effective, None, None, metadata_version)

    def _ranked_hits(
        self,
        rows: list[dict[str, Any]],
        *,
        keyword: str | None,
        field: str | None,
        metadata_version: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """把行变成命中明细，按最后生效版本去重后再按权重分排序。"""
        hits = [
            self._hit_detail(row, keyword=keyword, searched_field=field)
            for row in rows
        ]
        effective, duplicates = self._effective_hits(hits)
        ranked = self._cached_rank(effective, keyword, field, metadata_version)
        return ranked, effective, duplicates

    def _cached_rank(
        self,
        hits: list[dict[str, Any]],
        keyword: str | None,
        field: str | None,
        metadata_version: int | None,
    ) -> list[dict[str, Any]]:
        """排序缓存以「权重/版本/筛选」指纹为准。

        任一索引改了权重或刷新了版本，指纹就会变化并整体重算；
        因此刷新后不会残留上一次的排序，也不会返回旧版本的顺序。
        """
        fingerprint = self._rank_fingerprint(hits, keyword, field, metadata_version)
        state = store.state(MODULE)
        cache = state.get("sort_cache")
        if cache and cache.get("fingerprint") == fingerprint:
            order = {item["索引编号"]: item for item in hits}
            cached = [order[code] for code in cache["order"] if code in order]
            if len(cached) == len(hits):
                return cached
        ranked = sorted(hits, key=lambda item: (-item["命中分"], item["索引编号"]))
        state["sort_cache"] = {
            "fingerprint": fingerprint,
            "order": [item["索引编号"] for item in ranked],
        }
        return ranked

    def _rank_fingerprint(
        self,
        hits: list[dict[str, Any]],
        keyword: str | None,
        field: str | None,
        metadata_version: int | None,
    ) -> tuple[Any, ...]:
        return (
            keyword or "",
            field or "",
            metadata_version,
            self.current_metadata_version(metadata_version),
            tuple(
                (
                    hit["索引编号"],
                    hit["排序版本"],
                    hit["生效版本"],
                    tuple(sorted(hit["排序权重"].items())),
                    hit["命中分"],
                )
                for hit in sorted(hits, key=lambda item: str(item["索引编号"]))
            ),
        )

    def _refresh_versioned_rows(self, rows: list[dict[str, Any]]) -> None:
        """把每行的当前配置写进版本历史，保证回溯能取到「当时的权重」。"""
        for row in rows:
            history = row.get("版本历史")
            if not isinstance(history, list):
                history = []
            entry = snapshot(row)
            key = (
                _as_version(entry.get("生效元数据版本"), 1),
                _as_version(entry.get("排序版本"), 0),
            )
            if not any(
                (
                    _as_version(item.get("生效元数据版本"), 1),
                    _as_version(item.get("排序版本"), 0),
                ) == key
                for item in history
            ):
                history.append(entry)
            history.sort(
                key=lambda item: (
                    _as_version(item.get("生效元数据版本"), 1),
                    _as_version(item.get("排序版本"), 0),
                )
            )
            row["版本历史"] = history

    def _hit_detail(
        self,
        index: dict[str, Any],
        *,
        keyword: str | None,
        searched_field: str | None,
    ) -> dict[str, Any]:
        """一次命中的完整解释：命中的表、字段、权重与排序理由。"""
        groups = _field_groups_for(index)
        weights = normalize_weights(index)
        index_fields = split_fields(index.get("索引字段"))

        match_kind = "概览"
        matched_fields: list[str] = []
        if searched_field:
            matched_fields = [f for f in index_fields if searched_field in f]
            match_kind = "字段"
        elif keyword:
            matched_fields = [f for f in index_fields if keyword in f]
            if matched_fields:
                match_kind = "字段"
            elif self._matches_keyword(index, keyword):
                match_kind = "索引"
        if match_kind == "概览":
            matched_fields = list(index_fields)

        matched_groups: list[str] = []
        reasons: list[str] = []
        score = 0
        for field_name in matched_fields:
            group = groups.get(field_name, "业务域字段")
            if group not in matched_groups:
                matched_groups.append(group)
                level = weights.get(group, "低")
                score += WEIGHT_SCORES[level]
            reasons.append(
                f"字段「{field_name}」属于{group}，权重 {weights.get(group, '低')}"
                f"，贡献 {WEIGHT_SCORES[weights.get(group, '低')]} 分"
            )
        if match_kind == "索引":
            reasons.insert(0, "关键词命中索引名称/编号，未落到具体字段，不计字段权重分")

        return {
            "id": index.get("id"),
            "索引编号": index.get("索引编号"),
            "索引名称": index.get("索引名称"),
            "覆盖表名": index.get("覆盖表名"),
            "索引字段": index_fields,
            "命中字段": matched_fields,
            "命中分类": matched_groups,
            "命中方式": match_kind,
            "排序权重": weights,
            "权重明细": weight_breakdown(index),
            "权重理由": "；".join(reasons) if reasons else "未命中具体字段，按索引默认权重展示",
            "命中分": score,
            "命中说明": self._hit_sentence(index, matched_fields, matched_groups, weights, match_kind),
            "关联对象": self._objects_for({str(index.get("覆盖表名") or "")}),
            "生效版本": _as_version(index.get("生效版本"), 0),
            "生效元数据版本": _as_version(index.get("生效元数据版本"), 1),
            "排序版本": _as_version(index.get("排序版本"), 0),
            "刷新时间": index.get("刷新时间", ""),
            "状态": index.get("status"),
        }

    def _hit_sentence(
        self,
        index: dict[str, Any],
        matched_fields: list[str],
        matched_groups: list[str],
        weights: dict[str, str],
        match_kind: str,
    ) -> str:
        table = index.get("覆盖表名")
        if match_kind == "索引":
            return f"关键词命中 {index.get('索引名称')}（{index.get('索引编号')}），覆盖 {table}"
        if not matched_fields:
            return f"{index.get('索引名称')} 覆盖 {table}，未命中具体字段"
        field_text = "、".join(matched_fields)
        group_text = "、".join(matched_groups)
        weight_text = "，".join(
            "{}={}".format(group, weights.get(group, "低")) for group in matched_groups
        )
        return (
            f"命中 {table} 的 {field_text}（{group_text}），"
            f"按排序权重 {weight_text} 计分后排在前面"
        )

    def _summary(
        self,
        hits: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        keyword: str | None,
        metadata_version: int | None,
    ) -> dict[str, Any]:
        return {
            "命中总数": len(hits),
            "覆盖表数": len({hit["覆盖表名"] for hit in hits}),
            "命中列表": hits,
            "排序说明": [
                {
                    "序号": position + 1,
                    "索引编号": hit["索引编号"],
                    "索引名称": hit["索引名称"],
                    "覆盖表名": hit["覆盖表名"],
                    "命中字段": hit["命中字段"],
                    "排序权重": hit["排序权重"],
                    "命中分": hit["命中分"],
                    "命中说明": hit["命中说明"],
                }
                for position, hit in enumerate(hits)
            ],
            "权重口径": {
                "级别": WEIGHT_LEVELS,
                "分值": WEIGHT_SCORES,
                "排序规则": "命中分降序；同分按索引编号升序",
            },
            "筛选口径": {
                "关键词": keyword or "全部",
                "命中索引": "、".join(sorted({str(row.get("索引编号")) for row in rows}))
                or "无命中索引",
            },
            "生效版本": max(
                (_as_version(row.get("生效版本"), 0) for row in rows), default=0
            ),
            "元数据版本": self.current_metadata_version(metadata_version),
            "索引刷新时间": max(
                (str(row.get("刷新时间", "")) for row in rows), default=""
            ),
        }

    # ---------- 元数据版本与版本追溯 ----------

    def current_metadata_version(self, override: int | None = None) -> int:
        if override is not None:
            return override
        return store.metadata_version()

    def _record_metadata_version(self, metadata_version: int | None) -> None:
        """记录本次视图读取生效的元数据版本，便于追溯；连续相同读取只留一条。"""
        effective = self.current_metadata_version(metadata_version)
        store.append_trace(
            f"检索影响视图读取（元数据版本 {effective}）",
            dedupe=True,
        )

    def version_trace(self, limit: int = 20) -> list[dict[str, Any]]:
        return store.traces(limit)

    def _bump_metadata_version(
        self,
        values: dict[str, Any] | None = None,
        *,
        reason: str = "索引刷新",
    ) -> int:
        """刷新或权重变更时推进元数据版本，保证影响摘要跟着版本一起更新。"""
        requested = None
        if values and values.get("元数据版本") not in (None, ""):
            requested = _as_version(values.get("元数据版本"), 0) or None
        return store.bump_metadata_version(reason, requested=requested)

    # ---------- 登记与动作 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        payload = self._normalize_payload(values)
        rows = self._all_rows()
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update(payload)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["生效版本"] = 0
        entry["排序版本"] = 1
        entry["生效元数据版本"] = self.current_metadata_version()
        entry.setdefault("版本生效时间", "")
        entry.setdefault("刷新时间", "")
        self._refresh_versioned_rows([entry])
        rows.append(entry)
        return entry, []

    def _normalize_payload(self, values: dict[str, Any]) -> dict[str, Any]:
        payload = dict(values)
        for field in REQUIRED_FIELDS:
            payload[field] = str(values.get(field) or "").strip()
        fields = split_fields(values.get("索引字段"))
        groups = self._parse_field_groups(values.get("字段分类"), fields)
        payload["索引字段"] = ",".join(fields)
        payload["字段分类"] = groups
        payload["排序权重"] = self._parse_weights(values.get("排序权重"), groups)
        return payload

    def _parse_field_groups(self, raw: Any, fields: list[str]) -> dict[str, str]:
        if isinstance(raw, dict) and raw:
            return {str(key).strip(): str(value).strip() for key, value in raw.items()}
        if isinstance(raw, str) and raw.strip():
            groups = {}
            for chunk in raw.replace("，", ",").split(","):
                key, _, value = chunk.partition("=")
                if key.strip() and value.strip():
                    groups[key.strip()] = value.strip()
            if groups:
                return groups
        # 没给分类时按字段名归类，字段与分类始终对得上
        return {field: _infer_group(field) for field in fields}

    def _parse_weights(self, raw: Any, groups: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {group: "低" for group in FIELD_GROUPS}
        if raw in (None, ""):
            return normalized
        if isinstance(raw, str) and not any(sep in raw for sep in ["=", ":", "："]):
            raw = {"业务域字段": raw.strip()}
        weights = _coerce_weight_map(raw)
        for key, level in weights.items():
            if level not in WEIGHT_LEVELS:
                raise WeightRangeError(key, level)
            group = key if key in FIELD_GROUPS else groups.get(key, "业务域字段")
            normalized[group] = level
        return normalized

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"检索索引 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据检索可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if target == "已生效":
            entry["生效版本"] = _as_version(entry.get("生效版本"), 0) + 1
            entry["版本生效时间"] = _now()
            entry["刷新时间"] = entry["版本生效时间"]
            entry["生效元数据版本"] = self.current_metadata_version()
            self._refresh_versioned_rows([entry])
        self._sync_catalog_demand(entry)
        return entry, f"检索索引已{action}"

    def update_weights(
        self,
        entry_id: int,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """修改排序权重：校验范围、推进排序版本与元数据版本、清空旧排序缓存。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"检索索引 {entry_id} 不存在或已归档"
        fields = split_fields(values.get("索引字段") or entry.get("索引字段"))
        groups = self._parse_field_groups(
            values.get("字段分类") or entry.get("字段分类"), fields
        )
        try:
            weights = self._parse_weights(values.get("排序权重"), groups)
        except WeightRangeError as error:
            return None, str(error)
        if values.get("索引字段"):
            entry["索引字段"] = ",".join(fields)
        entry["字段分类"] = groups
        entry["排序权重"] = weights
        entry["排序版本"] = _as_version(entry.get("排序版本"), 0) + 1
        entry["生效元数据版本"] = self._bump_metadata_version(
            values, reason=f"权重变更（{entry.get('索引编号')} 排序版本 {entry['排序版本']}）"
        )
        entry["刷新时间"] = _now()
        # 排序缓存清空：下一次命中摘要一定按新权重从零重算，不会沿用旧顺序
        store.state(MODULE).pop("sort_cache", None)
        self._refresh_versioned_rows([entry])
        self._sync_catalog_demand(entry)
        return entry, "排序权重已更新，旧排序已清除"

    def refresh_index(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """刷新索引：先清空旧排序，再按最后生效版本重算摘要并推进版本。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"检索索引 {entry_id} 不存在或已归档"
        if values.get("排序权重") not in (None, ""):
            try:
                weights = self._parse_weights(
                    values.get("排序权重"), _field_groups_for(entry)
                )
            except WeightRangeError as error:
                return None, str(error)
            entry["排序权重"] = weights
            entry["排序版本"] = _as_version(entry.get("排序版本"), 0) + 1
        store.state(MODULE).pop("sort_cache", None)
        entry["生效版本"] = _as_version(entry.get("生效版本"), 0) + 1
        entry["版本生效时间"] = _now()
        entry["刷新时间"] = entry["版本生效时间"]
        entry["生效元数据版本"] = self._bump_metadata_version(
            values, reason=f"索引刷新（{entry.get('索引编号')} 生效版本 {entry['生效版本']}）"
        )
        self._refresh_versioned_rows([entry])
        self._sync_catalog_demand(entry)
        return entry, "索引已刷新，排序按本次生效版本重建"

    def _sync_catalog_demand(self, entry: dict[str, Any]) -> None:
        """索引刷新后，目录业务面的待办随版本一起更新。

        待办会同步到 ``pending``，因此运营概览的待处理量也会一起变化。
        """
        table = str(entry.get("覆盖表名") or "")
        version = _as_version(entry.get("生效版本"), 0)
        status = entry.get("status")
        for row in store.rows("catalog"):
            if str(row.get("关联表名") or "") != table:
                continue
            row["索引同步版本"] = version
            row["索引按需处理"] = version > 0
            if status == "已生效":
                row["待办事项"] = ""
            elif status == "已失效":
                row["待办事项"] = f"索引「{entry.get('索引名称')}」已失效，需重新构建"
            else:
                row["待办事项"] = f"索引「{entry.get('索引名称')}」待生效"
            row["pending"] = bool(row["待办事项"])

    # ---------- 检索命中 ----------

    def search(self, query: str, *, metadata_version: int | None = None) -> dict[str, Any]:
        """按关键词检索：返回命中摘要与可跳转的目录、血缘、质量对象。"""
        if not str(query or "").strip():
            raise ValueError("检索关键词不能为空")
        view = self.impact_view(keyword=query, metadata_version=metadata_version)
        return {
            "关键词": query,
            "命中摘要": view,
            "生效命中": view["命中列表"],
            "关联对象": self._objects_for(
                {hit["覆盖表名"] for hit in view["命中列表"]}
            ),
            "元数据版本": view["元数据版本"],
            "版本追溯": self.version_trace(5),
        }

    def _effective_hits(
        self, hits: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """同一覆盖表只保留最后生效的那份索引，避免重复索引互相覆盖权重。

        返回（生效命中, 被覆盖的重复索引说明），后者用于在视图里解释为什么某条没上榜。
        """
        best: dict[str, dict[str, Any]] = {}
        overridden: list[dict[str, Any]] = []
        for hit in hits:
            table = str(hit.get("覆盖表名"))
            current = best.get(table)
            if current is None:
                best[table] = hit
                continue
            challenger_key = (hit["排序版本"], hit["生效版本"], hit["命中分"])
            current_key = (current["排序版本"], current["生效版本"], current["命中分"])
            if challenger_key >= current_key:
                overridden.append(self._override_note(current, hit))
                best[table] = hit
            else:
                overridden.append(self._override_note(hit, current))
        return list(best.values()), overridden

    def _override_note(
        self, loser: dict[str, Any], winner: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "覆盖表名": winner["覆盖表名"],
            "保留索引": winner["索引编号"],
            "保留排序版本": winner["排序版本"],
            "被覆盖索引": loser["索引编号"],
            "被覆盖排序版本": loser["排序版本"],
            "说明": (
                f"{winner['索引编号']} 的排序版本 {winner['排序版本']} 最后生效，"
                f"覆盖 {loser['索引编号']} 的版本 {loser['排序版本']}"
            ),
        }

    def _objects_for(self, tables: set[str]) -> dict[str, list[dict[str, Any]]]:
        """从覆盖表名定位到目录、血缘与质量对象，供前端跳转。"""
        tables = {table for table in tables if table}
        catalog = [
            row for row in store.rows("catalog")
            if str(row.get("关联表名") or "") in tables
            or str(row.get("数据集名称") or "") in tables
        ]
        lineage = [
            row for row in store.rows("lineage")
            if str(row.get("关联表名") or "") in tables
            or str(row.get("节点名称") or "") in tables
        ]
        quality = [
            row for row in store.rows("quality")
            if str(row.get("关联表名") or "") in tables
            or str(row.get("监控对象") or "") in tables
        ]
        return {
            "目录": [
                {
                    "id": row.get("id"),
                    "编号": row.get("编目编号"),
                    "名称": row.get("数据集名称"),
                    "关联表名": row.get("关联表名"),
                    "跳转": f"/catalog?table={row.get('关联表名')}",
                }
                for row in catalog
            ],
            "血缘": [
                {
                    "id": row.get("id"),
                    "编号": row.get("节点编号"),
                    "名称": row.get("节点名称"),
                    "关联表名": row.get("关联表名"),
                    "跳转": f"/lineage?table={row.get('关联表名')}",
                }
                for row in lineage
            ],
            "质量": [
                {
                    "id": row.get("id"),
                    "编号": row.get("指标编号"),
                    "名称": row.get("监控对象"),
                    "关联表名": row.get("关联表名"),
                    "跳转": f"/quality?table={row.get('关联表名')}",
                }
                for row in quality
            ],
        }
