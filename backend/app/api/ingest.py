"""`POST /api/v1/ingest/batch` —— 接收成员 A 探针的上报。

契约：`docs/API_CONTRACT.md` §3。

本模块只做 HTTP 层的事：解析、鉴权、限流、错误码映射。
业务逻辑全在 `app.ingest.service` —— 保持 Handler 里没有业务逻辑
（`docs/backend/BACKEND_DESIGN.md` §2.8 的边界要求）。

错误码对照（`API_CONTRACT.md` §2.3）：

| 情形 | 状态码 | code |
|---|---|---|
| 结构合法但字段类型/取值非法 | 422 | `SCHEMA_VALIDATION_FAILED` |
| 超出批量上限（条数 / 字节数） | 400 | `INVALID_ARGUMENT` |
| 触发限流 | 429 | `RATE_LIMITED`（带 `Retry-After`） |
| 开启鉴权但 token 缺失/错误 | 401 | `UNAUTHENTICATED` |
| 数据库不可用 | 503 | `SERVICE_DEGRADED` |
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.diagnosis.auto import run_auto_diagnosis
from app.ingest.limits import metrics, rate_limiter
from app.ingest.schemas import IngestBatch, IngestResponse
from app.ingest.service import BatchTooLarge, ingest_batch

router = APIRouter(tags=["ingest"])

DbSession = Annotated[Session, Depends(get_db)]


def _error(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """统一错误响应（`API_CONTRACT.md` §2.3）。

    必须带 `traceId` ——「错误响应要可诊断，不能只返回 500」是契约要求。
    """
    trace_id = f"ing_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
    body: dict[str, Any] = {"error": {"code": code, "message": message, "traceId": trace_id}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def _check_auth(authorization: str | None) -> JSONResponse | None:
    """可选 Bearer Token 鉴权（成员 A 的 Q7：**默认关闭**）。

    返回 None 表示通过。鉴权失败必须返回 `401 UNAUTHENTICATED`，
    让 A 能明确区分"鉴权失败"与"网络故障"—— A 的 Q7 明确要求
    收到 401 就停止重试，不要把鉴权失败当网络问题无限重放。
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return None

    expected = settings.api_auth_token.strip()
    if not authorization or not authorization.startswith("Bearer "):
        return _error(
            401,
            "UNAUTHENTICATED",
            "缺少 Bearer Token。请停止重试并检查探针的 ZSVIRT_OBS_PROBE_TOKEN 配置。",
        )
    provided = authorization.removeprefix("Bearer ").strip()
    if provided != expected:
        return _error(401, "UNAUTHENTICATED", "Token 不匹配。请停止重试并核对凭据配置。")
    return None


@router.post("/api/v1/ingest/batch")
async def ingest_batch_endpoint(
    request: Request,
    response: Response,
    session: DbSession,
    authorization: str | None = Header(default=None),
) -> Any:
    """接收一批探针上报。"""
    # ---- 鉴权（先于一切，避免未授权请求消耗限流额度）----
    auth_error = _check_auth(authorization)
    if auth_error is not None:
        return auth_error

    # ---- 限流（成员 A 的 Q4 要求明确 429 与 Retry-After 语义）----
    decision = rate_limiter.check_and_record()
    if not decision.allowed:
        metrics.batches_rate_limited += 1
        return _error(
            429,
            "RATE_LIMITED",
            decision.reason,
            details={"retryAfterSeconds": decision.retry_after_seconds},
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    # ---- 读取原始体（需要字节数做 1MB 上限校验）----
    raw_body = await request.body()

    # ---- Schema 校验 ----
    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError as exc:
        return _error(
            422,
            "SCHEMA_VALIDATION_FAILED",
            "请求体不是合法 JSON",
            details={"detail": str(exc)},
        )

    try:
        batch = IngestBatch.model_validate(payload)
    except Exception as exc:  # pydantic.ValidationError
        errors = getattr(exc, "errors", lambda: [])()
        return _error(
            422,
            "SCHEMA_VALIDATION_FAILED",
            "上报体不满足契约。请按 docs/API_CONTRACT.md §3.2 修正探针。",
            details={
                "errors": [
                    {
                        "loc": ".".join(str(p) for p in e.get("loc", ())),
                        "message": e.get("msg", ""),
                    }
                    for e in errors[:20]
                ]
            },
        )

    # ---- 落库 ----
    try:
        result, duplicate = ingest_batch(
            session,
            batch=batch,
            received_at=datetime.now(UTC),
            payload_bytes=len(raw_body),
        )
        session.commit()
    except BatchTooLarge as exc:
        session.rollback()
        return _error(400, "INVALID_ARGUMENT", str(exc))
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        # 落库失败属 B 自身能力受损 → 503 SERVICE_DEGRADED
        # （与 502 UPSTREAM_UNAVAILABLE 区分：那是 ZSvirt 挂了）
        return _error(
            503,
            "SERVICE_DEGRADED",
            f"落库失败：{type(exc).__name__}",
            details={"detail": str(exc)[:300]},
        )

    # ---- 自动诊断联动（C 的 Q13：自动 + 手动都要）----
    #
    # **在上报事务提交之后**执行。诊断失败绝不能影响上报结果 ——
    # 数据采集是主流程，根因分析是增值能力，增值能力不能反过来毁掉主流程。
    #
    # 只诊断**本批新建**的告警：累加到既有告警上的证据不重复触发诊断，
    # 否则一条持续数小时的告警会按上报频率产出成百上千条几乎相同的结论。
    if not duplicate and result.touchedAlertIds:
        auto = run_auto_diagnosis(session, list(result.touchedAlertIds))
        session.commit()
        result.alerts["autoDiagnosis"] = auto.as_dict()

    response.headers["X-Ingest-Duplicate"] = "true" if duplicate else "false"
    return IngestResponse(
        data=result,
        meta={
            "traceId": f"ing_{batch.batchId}",
            "serverTime": datetime.now(UTC).isoformat(),
            "clockDriftMs": metrics.clock_drift_ms,
        },
    )
