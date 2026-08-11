from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from claude1_account_pool import (
    AccountCandidate,
    AccountPool,
    PoolConfigError,
    PoolConfigStore,
    PoolExhausted,
    PoolStateError,
    credential_fingerprint,
)


class MutableClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class AccountPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config = self.root / "account-pools.json"
        self.state = self.root / "account-state.sqlite3"
        self.clock = MutableClock()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_config(
        self,
        *,
        strategy: str = "round_robin",
        primary_weight: int = 1,
        secondary_weight: int = 1,
        primary_priority: int = 0,
        secondary_priority: int = 0,
        cooldown_seconds: int = 60,
        max_cooldown_seconds: int = 3600,
    ) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": {
                        "id:primary": {
                            "strategy": strategy,
                            "cooldown_seconds": cooldown_seconds,
                            "max_cooldown_seconds": max_cooldown_seconds,
                            "members": [
                                {
                                    "provider": "id:primary",
                                    "weight": primary_weight,
                                    "priority": primary_priority,
                                    "enabled": True,
                                },
                                {
                                    "provider": "id:secondary",
                                    "weight": secondary_weight,
                                    "priority": secondary_priority,
                                    "enabled": True,
                                },
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.config.chmod(0o600)

    @staticmethod
    def _candidates(
        primary_token: str = "fixture-primary-secret",
        secondary_token: str = "fixture-secondary-secret",
    ) -> dict[str, AccountCandidate]:
        return {
            "id:primary": AccountCandidate(
                credential_fingerprint(primary_token),
                endpoint="https://pool.invalid",
                credential_type="ANTHROPIC_AUTH_TOKEN",
            ),
            "id:secondary": AccountCandidate(
                credential_fingerprint(secondary_token),
                endpoint="https://pool.invalid",
                credential_type="ANTHROPIC_AUTH_TOKEN",
            ),
        }

    def _pool(self) -> AccountPool:
        return AccountPool(self.config, self.state, clock=self.clock)

    def test_missing_config_preserves_legacy_single_account_without_state_file(self) -> None:
        pool = self._pool()
        candidates = self._candidates()

        lease = pool.acquire("id:primary", candidates)

        self.assertEqual(lease.member, "id:primary")
        self.assertFalse(lease.managed)
        self.assertFalse(self.state.exists())
        self.assertNotIn(candidates["id:primary"].fingerprint, repr(lease))

    def test_round_robin_persists_across_pool_instances(self) -> None:
        self._write_config()
        candidates = self._candidates()

        sequence = [
            AccountPool(self.config, self.state, clock=self.clock)
            .acquire("id:primary", candidates)
            .member
            for _ in range(4)
        ]

        self.assertEqual(
            sequence,
            ["id:primary", "id:secondary", "id:primary", "id:secondary"],
        )

    def test_weighted_round_robin_uses_configured_ratio(self) -> None:
        self._write_config(
            strategy="weighted",
            primary_weight=2,
            secondary_weight=1,
        )
        pool = self._pool()
        candidates = self._candidates()

        sequence = [pool.acquire("id:primary", candidates).member for _ in range(6)]

        self.assertEqual(
            sequence,
            [
                "id:primary",
                "id:primary",
                "id:secondary",
                "id:primary",
                "id:primary",
                "id:secondary",
            ],
        )

    def test_priority_is_failover_before_round_robin(self) -> None:
        self._write_config(primary_priority=0, secondary_priority=10)
        pool = self._pool()
        candidates = self._candidates()

        first = pool.acquire("id:primary", candidates)
        second = pool.acquire("id:primary", candidates)
        pool.report(second, 429, "30")
        failover = pool.acquire("id:primary", candidates)

        self.assertEqual(first.member, "id:primary")
        self.assertEqual(second.member, "id:primary")
        self.assertEqual(failover.member, "id:secondary")

    def test_429_retry_after_cools_member_and_then_recovers(self) -> None:
        self._write_config()
        pool = self._pool()
        candidates = self._candidates()
        first = pool.acquire("id:primary", candidates)
        self.assertEqual(first.member, "id:primary")

        pool.report(first, 429, "20")
        during_cooldown = pool.acquire("id:primary", candidates)
        self.assertEqual(during_cooldown.member, "id:secondary")
        statuses = {item.member: item for item in pool.inspect("id:primary", candidates)}
        self.assertEqual(statuses["id:primary"].state, "cooldown")
        self.assertEqual(statuses["id:primary"].retry_after, 20)

        self.clock.value += 21
        recovered = pool.acquire("id:primary", candidates)
        self.assertEqual(recovered.member, "id:primary")

    def test_invalid_retry_after_uses_default_and_large_value_is_bounded(self) -> None:
        self._write_config(cooldown_seconds=15, max_cooldown_seconds=30)
        pool = self._pool()
        candidates = self._candidates()
        first = pool.acquire("id:primary", candidates)
        pool.report(first, 429, "not-a-date")
        status = pool.inspect("id:primary", candidates)[0]
        self.assertEqual(status.retry_after, 15)

        pool.reset("id:primary")
        next_lease = pool.acquire("id:primary", candidates)
        pool.report(next_lease, 429, "999999")
        status = pool.inspect("id:primary", candidates)[0]
        self.assertEqual(status.retry_after, 30)

        pool.reset("id:primary")
        zero_lease = pool.acquire("id:primary", candidates)
        pool.report(zero_lease, 429, "0")
        status = pool.inspect("id:primary", candidates)[0]
        self.assertEqual(status.retry_after, 1)

        pool.reset("id:primary")
        huge_lease = pool.acquire("id:primary", candidates)
        pool.report(huge_lease, 429, "9" * 5_000)
        status = pool.inspect("id:primary", candidates)[0]
        self.assertEqual(status.retry_after, 30)

    def test_auth_failure_disables_until_token_changes_or_state_is_reset(self) -> None:
        self._write_config()
        pool = self._pool()
        candidates = self._candidates()
        first = pool.acquire("id:primary", candidates)
        pool.report(first, 401)

        self.assertEqual(
            pool.acquire("id:primary", candidates).member,
            "id:secondary",
        )

        changed = self._candidates(primary_token="fixture-rotated-primary-secret")
        self.assertEqual(pool.acquire("id:primary", changed).member, "id:primary")

        rotated_lease = pool.acquire(
            "id:primary", changed, exclude={"id:secondary"}
        )
        pool.report(rotated_lease, 403)
        self.assertEqual(pool.reset("id:primary", "id:primary"), 2)
        self.assertEqual(
            pool.acquire("id:primary", changed, exclude={"id:secondary"}).member,
            "id:primary",
        )

    def test_concurrent_success_does_not_erase_auth_disable_or_cooldown(self) -> None:
        self._write_config(primary_priority=0, secondary_priority=10)
        pool = self._pool()
        candidates = self._candidates()
        first = pool.acquire("id:primary", candidates)
        concurrent = pool.acquire("id:primary", candidates)
        self.assertEqual(first.member, concurrent.member)

        pool.report(first, 401)
        pool.report(concurrent, 200)
        self.assertEqual(
            pool.acquire("id:primary", candidates).member,
            "id:secondary",
        )

        pool.reset("id:primary")
        limited = pool.acquire("id:primary", candidates)
        concurrent = pool.acquire("id:primary", candidates)
        pool.report(limited, 429, "30")
        pool.report(concurrent, 200)
        self.assertEqual(
            pool.acquire("id:primary", candidates).member,
            "id:secondary",
        )

    def test_old_key_result_cannot_erase_new_key_auth_disable(self) -> None:
        self._write_config()
        pool = self._pool()
        old_candidates = self._candidates()
        old_lease = pool.acquire(
            "id:primary",
            old_candidates,
            exclude={"id:secondary"},
        )
        new_candidates = self._candidates(
            primary_token="fixture-rotated-primary-secret"
        )
        new_lease = pool.acquire(
            "id:primary",
            new_candidates,
            exclude={"id:secondary"},
        )

        pool.report(new_lease, 401)
        pool.report(old_lease, 200)

        self.assertEqual(
            pool.acquire("id:primary", new_candidates).member,
            "id:secondary",
        )

    def test_exhaustion_distinguishes_auth_disable_from_cooldown(self) -> None:
        self._write_config()
        pool = self._pool()
        candidates = self._candidates()
        primary = pool.acquire(
            "id:primary", candidates, exclude={"id:secondary"}
        )
        secondary = pool.acquire(
            "id:primary", candidates, exclude={"id:primary"}
        )
        pool.report(primary, 401)
        pool.report(secondary, 403)

        with self.assertRaises(PoolExhausted) as captured:
            pool.acquire("id:primary", candidates)
        self.assertEqual(captured.exception.reason, "auth_disabled")
        self.assertIsNone(captured.exception.retry_after)

    def test_each_priority_group_has_an_independent_round_robin_cursor(self) -> None:
        self.config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "providers": {
                        "id:primary": {
                            "strategy": "round_robin",
                            "cooldown_seconds": 1,
                            "max_cooldown_seconds": 10,
                            "members": [
                                {"provider": "id:primary", "priority": 0},
                                {"provider": "id:secondary", "priority": 10},
                                {"provider": "id:third", "priority": 10},
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.config.chmod(0o600)
        candidates = self._candidates()
        candidates["id:third"] = AccountCandidate(
            credential_fingerprint("fixture-third-secret"),
            endpoint="https://pool.invalid",
            credential_type="ANTHROPIC_AUTH_TOKEN",
        )
        pool = self._pool()
        fallback_sequence = []

        for _index in range(6):
            primary = pool.acquire("id:primary", candidates)
            self.assertEqual(primary.member, "id:primary")
            pool.report(primary, 429, "1")
            fallback_sequence.append(
                pool.acquire("id:primary", candidates).member
            )
            self.clock.value += 2

        self.assertEqual(
            fallback_sequence,
            [
                "id:secondary",
                "id:third",
                "id:secondary",
                "id:third",
                "id:secondary",
                "id:third",
            ],
        )

    def test_exclude_never_retries_the_same_account_in_one_request(self) -> None:
        self._write_config()
        pool = self._pool()
        candidates = self._candidates()
        first = pool.acquire("id:primary", candidates)

        second = pool.acquire(
            "id:primary", candidates, exclude={first.member}
        )

        self.assertNotEqual(first.member, second.member)
        with self.assertRaises(PoolExhausted):
            pool.acquire(
                "id:primary", candidates, exclude={first.member, second.member}
            )

    def test_missing_or_incompatible_member_fails_closed(self) -> None:
        self._write_config()
        pool = self._pool()
        candidates = self._candidates()
        del candidates["id:secondary"]
        with self.assertRaises(PoolConfigError):
            pool.acquire("id:primary", candidates)

        candidates = self._candidates()
        candidates["id:secondary"] = AccountCandidate(
            candidates["id:secondary"].fingerprint,
            endpoint="https://pool.invalid",
            credential_type="ANTHROPIC_API_KEY",
        )
        with self.assertRaises(PoolConfigError):
            pool.acquire("id:primary", candidates)

        candidates = self._candidates()
        candidates["id:secondary"] = AccountCandidate(
            candidates["id:secondary"].fingerprint,
            endpoint="https://different.invalid",
            credential_type="ANTHROPIC_AUTH_TOKEN",
        )
        with self.assertRaises(PoolConfigError):
            pool.acquire("id:primary", candidates)

    def test_duplicate_credentials_and_raw_fingerprints_are_rejected(self) -> None:
        self._write_config()
        with self.assertRaises(PoolConfigError):
            AccountCandidate(
                "fixture-raw-credential",
                endpoint="https://pool.invalid",
                credential_type="ANTHROPIC_AUTH_TOKEN",
            )

        candidates = self._candidates()
        candidates["id:secondary"] = AccountCandidate(
            candidates["id:primary"].fingerprint,
            endpoint="https://pool.invalid",
            credential_type="ANTHROPIC_AUTH_TOKEN",
        )
        with self.assertRaisesRegex(PoolConfigError, "same credential"):
            self._pool().acquire("id:primary", candidates)

    def test_config_rejects_names_duplicates_and_secret_fields(self) -> None:
        invalid_configs = [
            {
                "version": 1,
                "providers": {
                    "name": {"members": ["id:a"]},
                },
            },
            {
                "version": 1,
                "providers": {
                    "id:a": {"members": ["id:a", "id:a"]},
                },
            },
            {
                "version": 1,
                "providers": {
                    "id:a": {
                        "token": "must-not-be-accepted",  # secret-guard: allow generic-secret-assignment
                        "members": ["id:a"],
                    }
                },
            },
        ]
        for raw in invalid_configs:
            with self.subTest(raw=raw):
                self.config.write_text(json.dumps(raw), encoding="utf-8")
                self.config.chmod(0o600)
                with self.assertRaises(PoolConfigError):
                    self._pool().definitions()

    @unittest.skipUnless(os.name == "posix", "POSIX private file permissions")
    def test_config_rejects_group_or_world_readable_file(self) -> None:
        self._write_config()
        self.config.chmod(0o644)
        with self.assertRaises(PoolConfigError):
            self._pool().definitions()

    def test_state_contains_no_tokens_and_is_private(self) -> None:
        self._write_config()
        candidates = self._candidates()
        pool = self._pool()
        lease = pool.acquire("id:primary", candidates)
        pool.report(lease, 429, "10")

        raw = self.state.read_bytes()
        self.assertNotIn(b"fixture-primary-secret", raw)
        self.assertNotIn(b"fixture-secondary-secret", raw)
        if os.name == "posix":
            self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_unknown_future_state_version_fails_without_downgrading(self) -> None:
        self._write_config()
        connection = sqlite3.connect(self.state)
        try:
            connection.execute("PRAGMA user_version=99")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(PoolStateError):
            self._pool().acquire("id:primary", self._candidates())

        connection = sqlite3.connect(self.state)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(version, 99)

    def test_nonempty_unversioned_state_fails_without_marking_schema_current(self) -> None:
        self._write_config()
        connection = sqlite3.connect(self.state)
        try:
            connection.execute("CREATE TABLE pool_cursor (pool TEXT PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(PoolStateError):
            self._pool().acquire("id:primary", self._candidates())

        connection = sqlite3.connect(self.state)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(version, 0)

    def test_version_one_state_is_migrated_without_losing_health(self) -> None:
        self._write_config()
        candidates = self._candidates()
        connection = sqlite3.connect(self.state)
        try:
            connection.execute(
                "CREATE TABLE pool_cursor ("
                "pool TEXT PRIMARY KEY, config_hash TEXT NOT NULL, "
                "cursor INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE member_state ("
                "pool TEXT NOT NULL, member TEXT NOT NULL, "
                "fingerprint TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0, "
                "cooldown_until REAL NOT NULL DEFAULT 0, last_status INTEGER, "
                "updated_at REAL NOT NULL, PRIMARY KEY (pool, member))"
            )
            connection.execute(
                "INSERT INTO member_state VALUES(?,?,?,?,?,?,?)",
                (
                    "id:primary",
                    "id:primary",
                    candidates["id:primary"].fingerprint,
                    1,
                    0,
                    401,
                    self.clock.value,
                ),
            )
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(
            self._pool().acquire("id:primary", candidates).member,
            "id:secondary",
        )
        connection = sqlite3.connect(self.state)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            member_pk = {
                row[1]: row[5]
                for row in connection.execute(
                    "PRAGMA table_info(member_state)"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertEqual(version, 2)
        self.assertEqual(
            {key: member_pk[key] for key in ("pool", "member", "fingerprint")},
            {"pool": 1, "member": 2, "fingerprint": 3},
        )

    def test_concurrent_default_acquire_keeps_one_shared_cursor_without_lock_errors(
        self,
    ) -> None:
        self._write_config()
        candidates = self._candidates()

        def select(_index: int) -> str:
            return AccountPool(
                self.config,
                self.state,
                clock=self.clock,
            ).acquire("id:primary", candidates).member

        with ThreadPoolExecutor(max_workers=16) as executor:
            selected = list(executor.map(select, range(500)))

        self.assertEqual(selected.count("id:primary"), 250)
        self.assertEqual(selected.count("id:secondary"), 250)


class PoolConfigStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "nested" / "account-pools.json"
        self.store = PoolConfigStore(self.path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_store_adds_updates_and_removes_only_non_secret_member_metadata(self) -> None:
        self.store.upsert_member(
            "id:primary",
            "id:secondary",
            weight=3,
            priority=5,
        )
        self.store.set_strategy("id:primary", "weighted")

        raw = self.store.normalized()
        pool = raw["providers"]["id:primary"]
        self.assertEqual(pool["strategy"], "weighted")
        self.assertEqual(
            pool["members"][1],
            {
                "provider": "id:secondary",
                "weight": 3,
                "priority": 5,
                "enabled": True,
            },
        )
        serialized = self.path.read_text(encoding="utf-8")
        self.assertNotIn("token", serialized.casefold())
        self.assertNotIn("secret", serialized.casefold())
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

        self.store.remove_member("id:primary", "id:secondary")
        self.assertEqual(
            [
                member["provider"]
                for member in self.store.normalized()["providers"]["id:primary"][
                    "members"
                ]
            ],
            ["id:primary"],
        )
        self.assertTrue(self.store.delete_pool("id:primary"))
        self.assertEqual(self.store.normalized()["providers"], {})

    def test_store_does_not_allow_removing_primary_member(self) -> None:
        self.store.upsert_member("id:primary", "id:secondary")
        with self.assertRaises(PoolConfigError):
            self.store.remove_member("id:primary", "id:primary")


if __name__ == "__main__":
    unittest.main()
