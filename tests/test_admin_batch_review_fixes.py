import asyncio
import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import orjson

from app.control.account.enums import AccountStatus
from app.control.account.models import AccountPage, AccountRecord
from app.control.account.refresh import RefreshResult
from app.control.account.backends.redis import RedisAccountRepository
from app.platform.errors import UpstreamError, ValidationError
from app.products.web.admin import batch as admin_batch
from app.products.web.admin.batch import BatchRequest, batch_nsfw, batch_refresh
from app.products.web.admin import tokens as admin_tokens


class _Repo:
    def __init__(self) -> None:
        self.records = {
            "active-token": AccountRecord(token="active-token", status=AccountStatus.ACTIVE),
            "disabled-token": AccountRecord(token="disabled-token", status=AccountStatus.DISABLED),
        }
        self.requested_tokens: list[str] = []

    async def get_accounts(self, tokens: list[str]) -> list[AccountRecord]:
        self.requested_tokens = tokens
        return [self.records[token] for token in tokens if token in self.records]


class _RefreshService:
    def __init__(self) -> None:
        self.refreshed_tokens: list[str] = []

    async def refresh_tokens(self, tokens: list[str]) -> RefreshResult:
        self.refreshed_tokens.extend(tokens)
        return RefreshResult(refreshed=len(tokens))


class _NsfwRepo:
    def __init__(self) -> None:
        self.records = [
            AccountRecord(token="off-token", status=AccountStatus.ACTIVE),
            AccountRecord(token="on-token", status=AccountStatus.ACTIVE, tags=["nsfw"]),
            AccountRecord(token="disabled-token", status=AccountStatus.DISABLED),
        ]
        self.patches = []

    async def list_accounts(self, query):
        return AccountPage(
            items=self.records,
            total=len(self.records),
            page=1,
            page_size=query.page_size,
            total_pages=1,
        )

    async def patch_accounts(self, patches):
        self.patches.extend(patches)


class _Pipeline:
    def __init__(self, redis: "_Redis") -> None:
        self.redis = redis
        self.keys: list[str] = []

    async def __aenter__(self) -> "_Pipeline":
        self.redis.pipeline_count += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def hgetall(self, key: str) -> None:
        self.keys.append(key)

    async def execute(self) -> list[dict[str, str]]:
        return [self.redis.hashes.get(key, {}) for key in self.keys]


class _Redis:
    def __init__(self) -> None:
        active = AccountRecord(token="active-token", status=AccountStatus.ACTIVE)
        self.hashes = {
            "accounts:record:active-token": RedisAccountRepository._to_hash(active, revision=7),
        }
        self.pipeline_count = 0
        self.hgetall_count = 0

    def pipeline(self) -> _Pipeline:
        return _Pipeline(self)

    async def hgetall(self, key: str) -> dict[str, str]:
        self.hgetall_count += 1
        return self.hashes.get(key, {})


class AdminBatchReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_refresh_filters_non_manageable_explicit_tokens(self):
        repo = _Repo()
        refresh_svc = _RefreshService()

        response = await batch_refresh(
            BatchRequest(tokens=["active-token", "disabled-token"]),
            async_mode=False,
            all_manageable=False,
            concurrency=None,
            repo=repo,
            refresh_svc=refresh_svc,
        )

        body = orjson.loads(response.body)
        self.assertEqual(repo.requested_tokens, ["active-token", "disabled-token"])
        self.assertEqual(refresh_svc.refreshed_tokens, ["active-token"])
        self.assertEqual(body["summary"]["total"], 1)
        self.assertEqual(body["summary"]["ok"], 1)
        self.assertEqual(body["summary"]["fail"], 0)

    async def test_batch_refresh_rejects_only_non_manageable_explicit_tokens(self):
        repo = _Repo()
        refresh_svc = _RefreshService()

        with self.assertRaises(ValidationError) as cm:
            await batch_refresh(
                BatchRequest(tokens=["disabled-token"]),
                async_mode=False,
                all_manageable=False,
                concurrency=None,
                repo=repo,
                refresh_svc=refresh_svc,
            )

        self.assertIn("No manageable tokens available", str(cm.exception))
        self.assertEqual(refresh_svc.refreshed_tokens, [])

    async def test_batch_nsfw_all_disabled_skips_enabled_and_unmanageable_accounts(self):
        repo = _NsfwRepo()
        handler = AsyncMock(return_value={"success": True, "tagged": True})

        with patch.object(admin_batch, "_nsfw_one", handler):
            response = await batch_nsfw(
                BatchRequest(tokens=[]),
                async_mode=False,
                all_manageable=False,
                all_nsfw_disabled=True,
                concurrency=1,
                enabled=True,
                repo=repo,
            )

        body = orjson.loads(response.body)
        handler.assert_awaited_once_with(repo, "off-token", True)
        self.assertEqual(body["summary"]["total"], 1)
        self.assertEqual(body["summary"]["ok"], 1)

    async def test_nsfw_retries_rate_limit_before_tagging_account(self):
        repo = _NsfwRepo()
        sequence = AsyncMock(side_effect=[UpstreamError("rate limited", status=429), None])

        with (
            patch("app.dataplane.reverse.protocol.xai_auth.nsfw_sequence", sequence),
            patch.object(admin_batch, "_config_int", return_value=1),
            patch.object(admin_batch, "_config_float", return_value=0.0),
            patch("app.products.web.admin.batch.asyncio.sleep", AsyncMock()) as sleep,
        ):
            result = await admin_batch._nsfw_one(repo, "off-token", True)

        self.assertEqual(result, {"success": True, "tagged": True})
        self.assertEqual(sequence.await_count, 2)
        sleep.assert_awaited_once_with(0.0)
        self.assertEqual(len(repo.patches), 1)


class RedisRepositoryReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_accounts_reads_many_tokens_with_one_pipeline(self):
        redis = _Redis()
        repo = RedisAccountRepository(redis)

        records = await repo.get_accounts(["active-token", "missing-token"])

        self.assertEqual([record.token for record in records], ["active-token"])
        self.assertEqual(redis.pipeline_count, 1)
        self.assertEqual(redis.hgetall_count, 0)


class AccountHtmlReviewFixTests(unittest.TestCase):
    def test_disabled_nsfw_buttons_use_row_specific_unavailable_reason(self):
        with open("app/statics/admin/account.html", encoding="utf-8") as fh:
            html = fh.read()
        disabled_branches = re.findall(
            r"data-tip=\"\$\{xe\(canManageNsfw \? tr\('account\.batchNsfw(?:Disable)?'.*?"
            r": tr\('account\.rowActionNotSupported'.*?aria-label=\"\$\{xe\((.*?)\)\}\"",
            html,
        )

        self.assertEqual(len(disabled_branches), 2)
        self.assertTrue(
            all(
                "canManageNsfw ?" in branch and "account.rowActionNotSupported" in branch
                for branch in disabled_branches
            )
        )

    def test_row_action_not_supported_is_translated_for_all_account_locales(self):
        for path in Path("app/statics/i18n").glob("*.json"):
            data = orjson.loads(path.read_bytes())
            with self.subTest(locale=path.name):
                self.assertIn("account", data, f"Locale {path.name} missing account section")
                self.assertIn("rowActionNotSupported", data["account"])

    def test_filter_chip_lists_keep_a_visible_horizontal_scrollbar(self):
        html = Path("app/statics/admin/account.html").read_text(encoding="utf-8")

        self.assertIn("scrollbar-width:thin", html)
        self.assertIn(".filter-chip-list::-webkit-scrollbar-thumb", html)
        self.assertNotIn(".filter-chip-list::-webkit-scrollbar {\n      display:none", html)


class ConfigHtmlReviewFixTests(unittest.TestCase):
    def test_get_current_value_preserves_schema_defaults(self):
        html = Path("app/statics/admin/config.html").read_text(encoding="utf-8")

        self.assertIn("function _getCurrentValue(section, key, field)", html)
        self.assertIn("_getValue(section, key, field)", html)
        self.assertIn("_getCurrentValue(section, field.key, field)", html)


class AdminTokenTaskReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        getattr(admin_tokens, "_background_tasks", set()).clear()
        admin_tokens._active_import_task_id = None

    async def asyncTearDown(self) -> None:
        pending = list(getattr(admin_tokens, "_background_tasks", set()))
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        getattr(admin_tokens, "_background_tasks", set()).clear()
        admin_tokens._active_import_task_id = None

    async def test_fire_and_forget_keeps_task_until_completion(self):
        release = asyncio.Event()

        async def _wait() -> None:
            await release.wait()

        task = admin_tokens._fire_and_forget(_wait())

        self.assertIn(task, admin_tokens._background_tasks)
        release.set()
        await task
        await asyncio.sleep(0)
        self.assertNotIn(task, admin_tokens._background_tasks)

    async def test_large_replace_import_reports_chunked_progress(self):
        class _ProgressRepo:
            async def replace_pool_with_progress(self, command, progress):
                for start in range(0, len(command.upserts), 500):
                    progress(len(command.upserts[start:start + 500]))

        original_threshold = admin_tokens._import_async_threshold
        admin_tokens._import_async_threshold = lambda: 1
        self.addCleanup(setattr, admin_tokens, "_import_async_threshold", original_threshold)

        tokens = [f"token-{i}" for i in range(1001)]
        response = await admin_tokens.save_tokens(
            admin_tokens.SaveTokensRequest({"basic": tokens}),
            auto_nsfw=False,
            repo=_ProgressRepo(),
            refresh_svc=object(),
        )
        body = orjson.loads(response.body)
        self.assertEqual(response.status_code, 202)
        self.assertFalse(body["already_running"])

        task = admin_tokens.get_task(body["task_id"])
        for _ in range(100):
            if task and task.status != "running":
                break
            await asyncio.sleep(0.01)

        self.assertIsNotNone(task)
        self.assertEqual(task.status, "done")
        self.assertEqual(task.processed, 1001)
        self.assertEqual(task.result["count"], 1001)


if __name__ == "__main__":
    unittest.main()
