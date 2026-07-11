"""Admin token CRUD — list, import, delete, replace pool.

Performance notes:
  - DI-injected repo (no try/except per call)
  - orjson direct output (bypasses stdlib json)
  - Quota dict: zero deserialization — reads r.quota directly
  - Import refresh: reuses app.state.refresh_service singleton
"""

import asyncio
import re
from typing import TYPE_CHECKING, Awaitable, Callable

import orjson
from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, RootModel

from app.platform.errors import AppError, ErrorKind, ValidationError
from app.platform.config.snapshot import get_config
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_ms
from app.platform.runtime.task import AsyncTask, create_task, expire_task, get_task
from app.control.account.commands import (
    AccountPatch,
    AccountUpsert,
    BulkReplacePoolCommand,
    ListAccountsQuery,
)
from app.control.account.enums import AccountStatus
from app.control.account.state_machine import is_manageable

if TYPE_CHECKING:
    from app.control.account.refresh import AccountRefreshService
    from app.control.account.repository import AccountRepository

from . import get_refresh_svc, get_repo

router = APIRouter(tags=["Admin - Tokens"])
_background_tasks: set[asyncio.Task] = set()
_active_import_task_id: str | None = None

# ---------------------------------------------------------------------------
# Token sanitisation
# ---------------------------------------------------------------------------

_TOKEN_TRANS = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-",
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u00a0": " ", "\u2007": " ", "\u202f": " ",
    "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",
})
_STRIP_RE = re.compile(r"\s+")


def _sanitize(value: str) -> str:
    tok = str(value or "").translate(_TOKEN_TRANS)
    tok = _STRIP_RE.sub("", tok)
    if tok.startswith("sso="):
        tok = tok[4:]
    return tok.encode("ascii", errors="ignore").decode("ascii")


def _mask(token: str) -> str:
    return f"{token[:8]}...{token[-8:]}" if len(token) > 20 else token


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class ReplacePoolRequest(BaseModel):
    pool: str
    tokens: list[str]
    tags: list[str] = []


class AddTokensRequest(BaseModel):
    tokens: list[str]
    pool: str = "basic"
    tags: list[str] = []


class EditTokenRequest(BaseModel):
    old_token: str
    token: str
    pool: str = "basic"


class ToggleTokenDisabledRequest(BaseModel):
    token: str
    disabled: bool


class ToggleTokensDisabledRequest(BaseModel):
    tokens: list[str]
    disabled: bool


class TokenImportItem(BaseModel):
    token: str
    tags: list[str] = []


class SaveTokensRequest(RootModel[dict[str, list[str | TokenImportItem]]]):
    """Bulk-save payload keyed by pool name."""


# ---------------------------------------------------------------------------
# Serialisation — zero-copy quota extraction
# ---------------------------------------------------------------------------

def _quota_brief(q: dict) -> dict:
    """Extract {auto, fast, expert, heavy, console} with only remaining/total from stored quota dict."""
    out = {}
    for mode in ("auto", "fast", "expert", "heavy", "console"):
        v = q.get(mode)
        if isinstance(v, dict):
            out[mode] = {
                "remaining": int(v.get("remaining", 0) or 0),
                "total": int(v.get("total", 0) or 0),
            }
    return out


def _serialize_record(r) -> dict:
    return {
        "token":       r.token,
        "pool":        r.pool or "basic",
        "status":      r.status,
        "quota":       _quota_brief(r.quota) if isinstance(r.quota, dict) else {},
        "use_count":   r.usage_use_count or 0,
        "fail_count":  r.usage_fail_count or 0,
        "last_used_at": r.last_use_at,
        "tags":        r.tags or [],
    }


def _json(data, status_code: int = 200) -> Response:
    """orjson fast-path response."""
    return Response(content=orjson.dumps(data), media_type="application/json", status_code=status_code)


def _fire_and_forget(coro) -> asyncio.Task:
    # Keep a strong reference so import maintenance tasks cannot disappear before completion.
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _cleanup(done: asyncio.Task) -> None:
        _background_tasks.discard(done)
        if done.cancelled():
            return
        if exc := done.exception():
            logger.warning("admin background task failed: error_type={}", type(exc).__name__)

    task.add_done_callback(_cleanup)
    return task


def _schedule_auto_nsfw(
    repo: "AccountRepository",
    tokens: list[str],
    *,
    enabled: bool,
) -> None:
    if not tokens or not enabled:
        return
    unique_tokens = list(dict.fromkeys(tokens))
    _fire_and_forget(_enable_nsfw_imported(repo, unique_tokens))


def _import_async_threshold() -> int:
    """Return the size at which imports use an observable background task."""
    try:
        return max(1, int(get_config("account.refresh.import_async_threshold", 1000)))
    except (TypeError, ValueError):
        return 1000


def _get_active_import_task() -> AsyncTask | None:
    global _active_import_task_id
    if not _active_import_task_id:
        return None
    task = get_task(_active_import_task_id)
    if task and task.status == "running":
        return task
    _active_import_task_id = None
    return None


def _start_import_task(
    total: int,
    operation: Callable[[AsyncTask], Awaitable[dict]],
) -> tuple[AsyncTask, bool]:
    """Start one large import, or return the import already in progress."""
    global _active_import_task_id
    existing = _get_active_import_task()
    if existing:
        return existing, True

    task = create_task(total)
    _active_import_task_id = task.id

    async def _run() -> None:
        global _active_import_task_id
        try:
            task.finish(await operation(task))
        except Exception as exc:
            logger.error("admin import task failed: error_type={}", type(exc).__name__)
            task.fail_task("导入任务失败，请查看服务日志")
        finally:
            if _active_import_task_id == task.id:
                _active_import_task_id = None
            _fire_and_forget(expire_task(task.id, 300))

    _fire_and_forget(_run())
    return task, False


def _maintenance_deferred(tokens: list[str]) -> bool:
    try:
        immediate_limit = max(0, int(get_config("account.refresh.import_immediate_limit", 500)))
    except (TypeError, ValueError):
        immediate_limit = 500
    return len(set(tokens)) > immediate_limit


async def _replace_pool_with_progress(
    repo: "AccountRepository",
    command: BulkReplacePoolCommand,
    task: AsyncTask | None,
) -> None:
    progress_repo = getattr(repo, "replace_pool_with_progress", None)
    if task and callable(progress_repo):
        loop = asyncio.get_running_loop()
        await progress_repo(
            command,
            lambda count: loop.call_soon_threadsafe(task.record_many, True, count),
        )
        # Let callbacks scheduled from SQLite's worker thread publish before
        # the enclosing task emits its final completion event.
        await asyncio.sleep(0)
        return
    await repo.replace_pool(command)
    if task:
        task.record_many(True, len(command.upserts))


async def _upsert_with_progress(
    repo: "AccountRepository",
    upserts: list[AccountUpsert],
    task: AsyncTask | None,
):
    progress_repo = getattr(repo, "upsert_accounts_with_progress", None)
    if task and callable(progress_repo):
        loop = asyncio.get_running_loop()
        result = await progress_repo(
            upserts,
            lambda count: loop.call_soon_threadsafe(task.record_many, True, count),
        )
        await asyncio.sleep(0)
        return result
    result = await repo.upsert_accounts(upserts)
    if task:
        task.record_many(True, len(upserts))
    return result


async def _list_all_records(repo: "AccountRepository") -> list:
    items: list = []
    page_num = 1
    while True:
        page = await repo.list_accounts(ListAccountsQuery(page=page_num, page_size=2000))
        items.extend(page.items)
        if page_num >= page.total_pages or not page.items:
            break
        page_num += 1
    return items


async def _list_token_payloads(repo: "AccountRepository") -> list[dict]:
    fast_list = getattr(repo, "list_token_payloads", None)
    if callable(fast_list):
        return await fast_list()
    return [_serialize_record(r) for r in await _list_all_records(repo)]


async def _list_invalid_tokens(repo: "AccountRepository") -> list[str]:
    fast_list = getattr(repo, "list_invalid_tokens", None)
    if callable(fast_list):
        return await fast_list()
    return [
        item["token"]
        for item in await _list_token_payloads(repo)
        if item.get("status") not in (
            AccountStatus.ACTIVE.value,
            AccountStatus.COOLING.value,
            AccountStatus.DISABLED.value,
        )
    ]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/tokens")
async def list_tokens(
    offset: int | None = Query(None, ge=0),
    limit: int | None = Query(None, ge=1, le=1000),
    repo: "AccountRepository" = Depends(get_repo),
):
    """Return all tokens, or a compact page for the management UI."""
    if offset is None and limit is None:
        return _json({"tokens": await _list_token_payloads(repo)})

    # Direct unit calls retain FastAPI's Query defaults; real requests arrive
    # here as integers.  Normalize both forms before using them as slices.
    page_offset = int(offset) if isinstance(offset, int) else 0
    page_limit = int(limit) if isinstance(limit, int) else 500
    fast_page = getattr(repo, "list_token_payload_page", None)
    if callable(fast_page):
        items, total = await fast_page(offset=page_offset, limit=page_limit)
    else:
        items = await _list_token_payloads(repo)
        total = len(items)
        items = items[page_offset:page_offset + page_limit]
    return _json({"tokens": items, "total": total, "offset": page_offset, "limit": page_limit})


@router.get("/tokens/import/status")
async def import_status():
    """Expose the one running import so a refreshed page can reattach to it."""
    task = _get_active_import_task()
    return _json(task.snapshot() if task else {"status": "idle"})


def _prepare_replace_import(req: SaveTokensRequest) -> list[tuple[str, list[AccountUpsert]]]:
    plans: list[tuple[str, list[AccountUpsert]]] = []
    for pool_name, items in req.root.items():
        upserts: list[AccountUpsert] = []
        for item in items:
            td = {"token": item} if isinstance(item, str) else item.model_dump()
            token_val = _sanitize(td.get("token", ""))
            if token_val:
                upserts.append(AccountUpsert(token=token_val, pool=pool_name, tags=td.get("tags") or []))
        if upserts:
            plans.append((pool_name, upserts))
    return plans


async def _save_replaced_pools(
    plans: list[tuple[str, list[AccountUpsert]]],
    *,
    auto_nsfw: bool,
    repo: "AccountRepository",
    refresh_svc: "AccountRefreshService",
    task: AsyncTask | None = None,
) -> dict:
    total_upserted = 0
    all_tokens: list[str] = []

    for pool_name, upserts in plans:
        await _replace_pool_with_progress(
            repo,
            BulkReplacePoolCommand(pool=pool_name, upserts=upserts),
            task,
        )
        all_tokens.extend(u.token for u in upserts)
        total_upserted += len(upserts)

    logger.info("admin tokens saved across pools: saved_count={}", total_upserted)
    maintenance_deferred = _maintenance_deferred(all_tokens)
    if all_tokens:
        _fire_and_forget(_refresh_then_auto_nsfw(
            refresh_svc,
            repo,
            all_tokens,
            auto_nsfw_enabled=auto_nsfw,
        ))
    return {
        "status": "success",
        "mode": "replace",
        "count": total_upserted,
        "maintenance_deferred": maintenance_deferred,
    }


@router.post("/tokens")
async def save_tokens(
    req: SaveTokensRequest,
    auto_nsfw: bool = Query(False),
    repo: "AccountRepository" = Depends(get_repo),
    refresh_svc: "AccountRefreshService" = Depends(get_refresh_svc),
):
    """Full pool replace — large imports run in the background with SSE progress."""
    plans = _prepare_replace_import(req)
    total = sum(len(upserts) for _pool, upserts in plans)

    if total >= _import_async_threshold():
        task, already_running = _start_import_task(
            total,
            lambda current: _save_replaced_pools(
                plans,
                auto_nsfw=auto_nsfw,
                repo=repo,
                refresh_svc=refresh_svc,
                task=current,
            ),
        )
        return _json({
            "status": "running",
            "task_id": task.id,
            "total": task.total,
            "already_running": already_running,
        }, status_code=202)

    return _json(await _save_replaced_pools(
        plans,
        auto_nsfw=auto_nsfw,
        repo=repo,
        refresh_svc=refresh_svc,
    ))


async def _add_prepared_tokens(
    cleaned: list[str],
    *,
    pool: str,
    tags: list[str],
    auto_nsfw: bool,
    repo: "AccountRepository",
    refresh_svc: "AccountRefreshService",
    task: AsyncTask | None = None,
) -> dict:
    # Only upsert tokens that are not already active — avoids overwriting quota/status.
    # Soft-deleted tokens are treated as non-existing so they can be restored.
    existing = {r.token for r in await repo.get_accounts(cleaned) if not r.is_deleted()}
    new_tokens = [t for t in cleaned if t not in existing]
    if task and existing:
        task.record_many(True, len(existing))

    if not new_tokens:
        return {"status": "success", "mode": "add", "count": 0, "skipped": len(cleaned)}

    upserts = [AccountUpsert(token=t, pool=pool, tags=tags) for t in new_tokens]
    result = await _upsert_with_progress(repo, upserts, task)
    logger.info(
        "admin tokens added: pool={} added_count={} skipped_count={}",
        pool,
        len(new_tokens),
        len(existing),
    )

    _fire_and_forget(_refresh_then_auto_nsfw(
        refresh_svc,
        repo,
        new_tokens,
        auto_nsfw_enabled=auto_nsfw,
    ))
    return {
        "status": "success",
        "mode": "add",
        "count": result.upserted or len(new_tokens),
        "skipped": len(existing),
        "maintenance_deferred": _maintenance_deferred(new_tokens),
    }


@router.post("/tokens/add")
async def add_tokens(
    req: AddTokensRequest,
    auto_nsfw: bool = Query(False),
    repo: "AccountRepository" = Depends(get_repo),
    refresh_svc: "AccountRefreshService" = Depends(get_refresh_svc),
):
    requested_pool = (req.pool or "basic").strip().lower()

    # Deduplicate and sanitize input
    cleaned: list[str] = []
    seen: set[str] = set()
    for token in req.tokens:
        tok = _sanitize(token)
        if tok and tok not in seen:
            seen.add(tok)
            cleaned.append(tok)
    if not cleaned:
        raise ValidationError("No valid tokens provided", param="tokens")

    if len(cleaned) >= _import_async_threshold():
        task, already_running = _start_import_task(
            len(cleaned),
            lambda current: _add_prepared_tokens(
                cleaned,
                pool=requested_pool,
                tags=req.tags,
                auto_nsfw=auto_nsfw,
                repo=repo,
                refresh_svc=refresh_svc,
                task=current,
            ),
        )
        return _json({
            "status": "running",
            "task_id": task.id,
            "total": task.total,
            "already_running": already_running,
        }, status_code=202)

    return _json(await _add_prepared_tokens(
        cleaned,
        pool=requested_pool,
        tags=req.tags,
        auto_nsfw=auto_nsfw,
        repo=repo,
        refresh_svc=refresh_svc,
    ))


@router.delete("/tokens")
async def delete_tokens(
    tokens: list[str] = Body(...),
    repo: "AccountRepository" = Depends(get_repo),
):
    cleaned = [t for t in (_sanitize(t) for t in tokens) if t]
    if not cleaned:
        raise ValidationError("No valid tokens provided", param="tokens")
    await repo.delete_accounts(cleaned)
    logger.info("admin tokens deleted: deleted_count={}", len(cleaned))
    return _json({"deleted": len(cleaned)})


@router.delete("/tokens/invalid")
async def delete_invalid_tokens(repo: "AccountRepository" = Depends(get_repo)):
    tokens = await _list_invalid_tokens(repo)

    if not tokens:
        return _json({"deleted": 0})

    await repo.delete_accounts(tokens)
    logger.info("admin invalid tokens deleted: deleted_count={}", len(tokens))
    return _json({"deleted": len(tokens)})


@router.put("/tokens/edit")
async def edit_token(
    req: EditTokenRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    old_token = _sanitize(req.old_token)
    new_token = _sanitize(req.token)
    pool = (req.pool or "basic").strip().lower()

    if not old_token or not new_token:
        raise ValidationError("Token is required", param="token")

    records = await repo.get_accounts([old_token])
    if not records:
        raise AppError(
            "Account not found",
            kind=ErrorKind.VALIDATION,
            code="account_not_found",
            status=404,
        )
    record = records[0]

    if old_token != new_token:
        existing = await repo.get_accounts([new_token])
        if existing:
            raise AppError(
                "Target token already exists",
                kind=ErrorKind.VALIDATION,
                code="token_conflict",
                status=409,
            )

    await repo.upsert_accounts([AccountUpsert(
        token=new_token,
        pool=pool,
        tags=record.tags,
        ext=record.ext,
    )])

    if old_token == new_token:
        logger.info("admin token updated: token={} pool={}", _mask(new_token), pool)
        return _json({"status": "success", "token": new_token, "pool": pool})

    qs = record.quota_set()
    await repo.patch_accounts([AccountPatch(
        token=new_token,
        status=record.status,
        tags=record.tags,
        quota_auto=qs.auto.to_dict(),
        quota_fast=qs.fast.to_dict(),
        quota_expert=qs.expert.to_dict(),
        usage_use_delta=record.usage_use_count,
        usage_fail_delta=record.usage_fail_count,
        usage_sync_delta=record.usage_sync_count,
        last_use_at=record.last_use_at,
        last_fail_at=record.last_fail_at,
        last_fail_reason=record.last_fail_reason,
        last_sync_at=record.last_sync_at,
        last_clear_at=record.last_clear_at,
        state_reason=record.state_reason,
        ext_merge=record.ext,
    )])
    await repo.delete_accounts([old_token])

    logger.info("admin token replaced: previous_token={} current_token={} pool={}", _mask(old_token), _mask(new_token), pool)
    return _json({"status": "success", "token": new_token, "pool": pool})


@router.post("/tokens/disabled")
async def toggle_token_disabled(
    req: ToggleTokenDisabledRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    token = _sanitize(req.token)
    if not token:
        raise ValidationError("Token is required", param="token")

    records = await repo.get_accounts([token])
    if not records:
        raise AppError(
            "Account not found",
            kind=ErrorKind.VALIDATION,
            code="account_not_found",
            status=404,
        )
    record = records[0]

    if req.disabled:
        await repo.patch_accounts([AccountPatch(
            token=token,
            status=AccountStatus.DISABLED,
            state_reason="operator_disabled",
            ext_merge={
                **record.ext,
                "disabled_at": now_ms(),
                "disabled_reason": "operator_disabled",
            },
        )])
        logger.info("admin token disabled: token={}", _mask(token))
        return _json({"status": "success", "token": token, "disabled": True})

    await repo.patch_accounts([AccountPatch(
        token=token,
        status=AccountStatus.ACTIVE,
        clear_failures=True,
    )])
    logger.info("admin token restored: token={}", _mask(token))
    return _json({"status": "success", "token": token, "disabled": False})


@router.post("/tokens/disabled/batch")
async def toggle_tokens_disabled(
    req: ToggleTokensDisabledRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in req.tokens:
        token = _sanitize(raw)
        if token and token not in seen:
            seen.add(token)
            cleaned.append(token)
    if not cleaned:
        raise ValidationError("No valid tokens provided", param="tokens")

    records = await repo.get_accounts(cleaned)
    if not records:
        raise AppError(
            "No matching accounts found",
            kind=ErrorKind.VALIDATION,
            code="account_not_found",
            status=404,
        )

    ts = now_ms()
    patches: list[AccountPatch] = []
    for record in records:
        if req.disabled:
            patches.append(AccountPatch(
                token=record.token,
                status=AccountStatus.DISABLED,
                state_reason="operator_disabled",
                ext_merge={
                    **record.ext,
                    "disabled_at": ts,
                    "disabled_reason": "operator_disabled",
                },
            ))
        else:
            patches.append(AccountPatch(
                token=record.token,
                status=AccountStatus.ACTIVE,
                clear_failures=True,
            ))

    result = await repo.patch_accounts(patches)
    logger.info(
        "admin tokens disabled batch updated: disabled={} requested_count={} patched_count={}",
        req.disabled,
        len(cleaned),
        result.patched,
    )
    return _json({
        "status": "success",
        "disabled": req.disabled,
        "summary": {
            "total": len(cleaned),
            "ok": result.patched,
            "fail": max(0, len(cleaned) - result.patched),
        },
    })


@router.put("/tokens/pool")
async def replace_pool(
    req: ReplacePoolRequest,
    auto_nsfw: bool = Query(False),
    repo: "AccountRepository" = Depends(get_repo),
    refresh_svc: "AccountRefreshService" = Depends(get_refresh_svc),
):
    cleaned = [t for t in (_sanitize(t) for t in req.tokens) if t]
    upserts = [AccountUpsert(token=t, pool=req.pool, tags=req.tags) for t in cleaned]
    await repo.replace_pool(BulkReplacePoolCommand(pool=req.pool, upserts=upserts))
    logger.info("admin pool replaced: pool={} token_count={}", req.pool, len(cleaned))
    if cleaned:
        _fire_and_forget(_refresh_then_auto_nsfw(
            refresh_svc,
            repo,
            cleaned,
            auto_nsfw_enabled=auto_nsfw,
        ))
    return _json({"pool": req.pool, "count": len(cleaned)})


# ---------------------------------------------------------------------------
# Fire-and-forget import refresh
# ---------------------------------------------------------------------------

async def _refresh_imported(svc: "AccountRefreshService", tokens: list[str]) -> bool:
    try:
        await svc.refresh_on_import(tokens)
        logger.info("admin import quota sync completed: token_count={}", len(tokens))
        return True
    except Exception as exc:
        logger.warning("admin import quota sync failed: token_count={} error={}", len(tokens), exc)
        return False


async def _refresh_then_auto_nsfw(
    svc: "AccountRefreshService",
    repo: "AccountRepository",
    tokens: list[str],
    *,
    auto_nsfw_enabled: bool,
) -> None:
    unique_tokens = list(dict.fromkeys(tokens))
    immediate_limit = max(0, int(get_config("account.refresh.import_immediate_limit", 500)))
    if len(unique_tokens) > immediate_limit:
        logger.info(
            "admin import maintenance deferred: token_count={} immediate_limit={}",
            len(unique_tokens),
            immediate_limit,
        )
        return
    if await _refresh_imported(svc, unique_tokens):
        _schedule_auto_nsfw(repo, unique_tokens, enabled=auto_nsfw_enabled)


async def _enable_nsfw_imported(repo: "AccountRepository", tokens: list[str]) -> None:
    from app.products.web.admin.batch import _concurrency, _nsfw_one
    from app.platform.runtime.batch import run_batch

    records = await repo.get_accounts(tokens)
    by_token = {r.token: r for r in records}
    manageable_tokens = [token for token in tokens if (record := by_token.get(token)) and is_manageable(record)]
    skipped_c = len(tokens) - len(manageable_tokens)
    if not manageable_tokens:
        logger.info("admin import auto nsfw skipped: token_count={} skipped_non_manageable={}", len(tokens), skipped_c)
        return

    ok_c = fail_c = 0

    async def _one(token: str) -> None:
        nonlocal ok_c, fail_c
        try:
            await _nsfw_one(repo, token, True)
            ok_c += 1
        except Exception as exc:
            fail_c += 1
            logger.warning("admin import auto nsfw failed: token={} error={}", _mask(token), exc)

    await run_batch(manageable_tokens, _one, concurrency=_concurrency(None, "batch.nsfw_concurrency"))
    logger.info(
        "admin import auto nsfw completed: token_count={} skipped_non_manageable={} ok={} failed={}",
        len(manageable_tokens),
        skipped_c,
        ok_c,
        fail_c,
    )
