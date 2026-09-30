"""Persist `durationMs` so the diagnostics list stops showing an empty column.

Four coordinated changes:

1. `Diagnosis` ORM model gains `duration_ms` (the migration is separate).
2. `run_diagnosis` writes the measured value instead of only returning it.
3. `auto._diagnose_cluster` stops discarding it (`row, _duration_ms = ...`).
4. `to_diagnosis` reads it from the row (it previously took `duration_ms` as a
   parameter supplied only on the single-diagnosis detail path — so the list
   endpoint could never populate it).
"""

from __future__ import annotations

import pathlib

BACKEND = pathlib.Path('/home/archer/workspace/zsvirt-observability/backend')
MODELS = BACKEND / 'app/models.py'
SERVICE = BACKEND / 'app/diagnosis/service.py'
AUTO = BACKEND / 'app/diagnosis/auto.py'
API = BACKEND / 'app/api/diagnoses.py'


def sub(text: str, old: str, new: str, *, label: str) -> str:
    if text.count(old) != 1:
        raise SystemExit(f'[{label}] expected 1 match, found {text.count(old)}')
    return text.replace(old, new, 1)


def main() -> int:
    # ---- 1. ORM column ----
    models = MODELS.read_text(encoding='utf-8')
    models = sub(
        models,
        '    rule_set_version: Mapped[str] = mapped_column(String(32), nullable=False)',
        '    rule_set_version: Mapped[str] = mapped_column(String(32), nullable=False)\n'
        '    #: 诊断耗时（毫秒）。**可空**：本次迁移之前写入的行没有测量值，\n'
        '    #: 回填一个猜测值等于伪造一个看起来像测量结果的数字。\n'
        '    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)',
        label='models-column',
    )
    MODELS.write_text(models, encoding='utf-8')
    print('patched app/models.py')

    # ---- 2. service writes it ----
    svc = SERVICE.read_text(encoding='utf-8')
    svc = sub(
        svc,
        '        rule_set_version=result.rule_set_version,\n'
        '        notes=list(result.notes),\n'
        '    )\n'
        '    session.add(row)\n'
        '    session.flush()\n'
        '\n'
        '    return row, duration_ms',
        '        rule_set_version=result.rule_set_version,\n'
        '        notes=list(result.notes),\n'
        '        duration_ms=duration_ms,\n'
        '    )\n'
        '    session.add(row)\n'
        '    session.flush()\n'
        '\n'
        '    return row, duration_ms',
        label='service-write',
    )
    SERVICE.write_text(svc, encoding='utf-8')
    print('patched app/diagnosis/service.py')

    # ---- 3. auto keeps it ----
    auto = AUTO.read_text(encoding='utf-8')
    auto = sub(
        auto,
        '    row, _duration_ms = diagnosis_service.run_diagnosis(',
        '    # 耗时已落库（`diagnosis.duration_ms`），这里不再需要接收返回值；\n'
        '    # 早先写成 `_duration_ms` 把它丢掉了，于是列表接口整列为空。\n'
        '    row, _ = diagnosis_service.run_diagnosis(',
        label='auto-discard',
    )
    AUTO.write_text(auto, encoding='utf-8')
    print('patched app/diagnosis/auto.py')

    # ---- 4. serialiser prefers the persisted value ----
    api = API.read_text(encoding='utf-8')
    api = sub(
        api,
        '        ruleSetVersion=row.rule_set_version,\n'
        '        notes=list(row.notes or []),\n'
        '        durationMs=duration_ms,\n'
        '    )',
        '        ruleSetVersion=row.rule_set_version,\n'
        '        notes=list(row.notes or []),\n'
        '        # 优先用**落库的**耗时；调用方传入的用于"刚算出来、还没读回来"\n'
        '        # 的场景。两者都为 None 时如实返回 null，不编一个 0。\n'
        '        durationMs=duration_ms if duration_ms is not None else row.duration_ms,\n'
        '    )',
        label='api-serialise',
    )
    API.write_text(api, encoding='utf-8')
    print('patched app/api/diagnoses.py')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
