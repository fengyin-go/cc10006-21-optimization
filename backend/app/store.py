"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.seed import SEED_ROWS

TZ = timezone(timedelta(hours=8))


def _clone(value: Any) -> Any:
    """示例数据里有嵌套 dict/list，浅拷贝会让用例之间互相污染。"""
    if isinstance(value, dict):
        return {key: _clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone(item) for item in value]
    return value


class Store:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """把示例数据、运行期状态与版本追溯恢复到初始状态（测试与重启共用）。"""
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [_clone(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 各模块自己的运行期状态（例如检索模块的排序缓存）
        self._state: dict[str, dict[str, Any]] = {}
        # 元数据版本变化与视图读取的追溯记录
        self._traces: list[dict[str, Any]] = []
        # 平台元数据版本：示例数据初始为第 1 版
        self._metadata_version: int = 1

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    # ---------- 运行期状态 ----------

    def state(self, module: str) -> dict[str, Any]:
        return self._state.setdefault(module, {})

    # ---------- 元数据版本 ----------

    def metadata_version(self) -> int:
        return self._metadata_version

    def bump_metadata_version(
        self,
        reason: str,
        *,
        requested: int | None = None,
    ) -> int:
        """推进元数据版本；同一版本不重复推进，保证版本号单调递增。"""
        if requested is not None and requested > self._metadata_version:
            self._metadata_version = requested
        else:
            self._metadata_version += 1
        self.append_trace(reason)
        return self._metadata_version

    # ---------- 追溯记录 ----------

    def append_trace(self, reason: str, *, dedupe: bool = False) -> dict[str, Any]:
        record = {
            "元数据版本": self._metadata_version,
            "记录时间": datetime.now(TZ).replace(microsecond=0).isoformat(),
            "来源": reason,
        }
        if dedupe and self._traces:
            last = self._traces[-1]
            if (
                last["元数据版本"] == record["元数据版本"]
                and last["来源"] == record["来源"]
            ):
                return last
        self._traces.append(record)
        # 只保留最近一段，避免无界增长
        del self._traces[:-100]
        return record

    def traces(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._traces)[-limit:]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
