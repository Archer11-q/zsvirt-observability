"""Two final fixes: the missing `Callable` import, and credential validation
before reading (so an unconfigured channel reports "not configured" instead of
the misleading "no samples in window").
"""

from __future__ import annotations

import pathlib

WATCH = pathlib.Path(
    '/home/archer/workspace/zsvirt-observability/backend/app/zsvirt/watch.py'
)


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'[{label}] expected 1 match, found {count}. Refusing to patch.')
    return text.replace(old, new, 1)


OLD_IMPORT = '''from datetime import UTC, datetime, timedelta
from typing import Any, Protocol'''
NEW_IMPORT = '''from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol'''


def main() -> int:
    text = WATCH.read_text(encoding='utf-8')
    text = replace_once(text, OLD_IMPORT, NEW_IMPORT, label='callable-import')

    # Provider: validate credentials before attempting any read.
    OLD_READ = '''    def read(self, *, at: datetime | None = None) -> GpuMetricReading:
        """读一次指标。取不到样本时**抛错**，不返回 0。"""
        snapshot = self._pick_snapshot(at=at)'''
    NEW_READ = '''    def read(self, *, at: datetime | None = None) -> GpuMetricReading:
        """读一次指标。取不到样本时**抛错**，不返回 0。"""
        self._ensure_configured()
        snapshot = self._pick_snapshot(at=at)'''
    text = replace_once(text, OLD_READ, NEW_READ, label='read-precondition')

    OLD_SNAPSHOT = '''    def _snapshot_list(self) -> list[CardSnapshot]:'''
    NEW_SNAPSHOT = '''    def _ensure_configured(self) -> None:
        """凭据不齐时给出**准确**的失败原因。

        不这么做的话，未配置的渠道会走到"窗口内没有样本"，把"没配"报成
        "环境没数据" —— 排查方向会被整个带偏。
        """
        if not self.client.session.configured:
            raise GpuMetricsUnavailable(
                "ZWatch 渠道凭据未配置：请设置 ZSVIRT_AUTH_TOKEN，"
                "或 ZSVIRT_ACCESS_KEY + ZSVIRT_SECRET_KEY（见 docs/DEPLOYMENT.md §4）"
            )

    def _snapshot_list(self) -> list[CardSnapshot]:'''
    text = replace_once(text, OLD_SNAPSHOT, NEW_SNAPSHOT, label='ensure-configured')

    WATCH.write_text(text, encoding='utf-8')
    print('patched app/zsvirt/watch.py')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
