"""Let `reconcile_all` forward a `recovery_after` window.

D-084 makes the recovery window configurable ("默认 10 分钟"), and `reconcile`
accepts `recovery_after`. But `reconcile_all` — the entry point built precisely
*for* background tasks ("供后台定时任务或演示脚本调用") — dropped the parameter.
So a scheduled job had no way to honour the configured window.

Rather than have the scheduler call `release_expired_silences` + `reconcile` by
hand (which would duplicate the ordering and the flush subtlety that
`reconcile_all` documents), the parameter is threaded through here.
"""

from __future__ import annotations

import pathlib

TARGET = pathlib.Path(
    '/home/archer/workspace/zsvirt-observability/backend/app/alerts/engine.py'
)


def main() -> int:
    text = TARGET.read_text(encoding='utf-8')

    old_sig = '''def reconcile_all(
    session: Session,
    *,
    rule_set: RuleSet | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:'''
    new_sig = '''def reconcile_all(
    session: Session,
    *,
    rule_set: RuleSet | None = None,
    now: datetime | None = None,
    recovery_after: timedelta | None = None,
) -> dict[str, Any]:'''
    if text.count(old_sig) != 1:
        raise SystemExit('signature anchor mismatch; refusing to write')
    text = text.replace(old_sig, new_sig, 1)

    old_call = '    recovery = reconcile(session, rule_set=rule_set, now=moment)'
    new_call = (
        '    recovery = reconcile(\n'
        '        session, rule_set=rule_set, now=moment, recovery_after=recovery_after\n'
        '    )'
    )
    if text.count(old_call) != 1:
        raise SystemExit('call anchor mismatch; refusing to write')
    text = text.replace(old_call, new_call, 1)

    # document the new parameter in the docstring
    old_doc = '    返回 `{"released": [...], "recovery": {...}}`。\n    """'
    new_doc = (
        '    `recovery_after` 透传给 `reconcile`；不传则用规则集上的默认值\n'
        '    （`D-084` 的 10 分钟）。背景任务应当把配置值传进来，否则\n'
        '    `ALERT_RECOVERY_AFTER_SEC` 这个配置项是死的。\n\n'
        '    返回 `{"released": [...], "recovery": {...}}`。\n    """'
    )
    if text.count(old_doc) != 1:
        raise SystemExit('docstring anchor mismatch; refusing to write')
    text = text.replace(old_doc, new_doc, 1)

    TARGET.write_text(text, encoding='utf-8')
    print('patched app/alerts/engine.py (reconcile_all forwards recovery_after)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
