import unittest

from facesearch.database.retry import (
    database_retry_delay,
    is_transient_database_conflict,
)


class TransientDatabaseConflictTests(unittest.TestCase):
    def test_mysql_deadlock_error_code_is_retryable(self):
        self.assertTrue(
            is_transient_database_conflict(
                Exception(1213, "Deadlock found when trying to get lock")
            )
        )

    def test_lock_wait_timeout_is_retryable(self):
        self.assertTrue(
            is_transient_database_conflict(
                RuntimeError("Lock wait timeout exceeded; try restarting transaction")
            )
        )

    def test_unique_constraint_error_is_not_retryable(self):
        self.assertFalse(
            is_transient_database_conflict(
                Exception(1062, "Duplicate entry for unique key")
            )
        )

    def test_backoff_increases_exponentially_and_caps_base_delay(self):
        values = [
            database_retry_delay(
                attempt,
                base_seconds=0.3,
                max_seconds=1.0,
                jitter_seconds=0.0,
            )
            for attempt in range(5)
        ]
        self.assertEqual(values, [0.3, 0.6, 1.0, 1.0, 1.0])

    def test_backoff_jitter_is_bounded_and_deterministic_when_requested(self):
        low = database_retry_delay(0, jitter_seconds=0.35, random_value=0.0)
        high = database_retry_delay(0, jitter_seconds=0.35, random_value=1.0)
        self.assertAlmostEqual(low, 0.25)
        self.assertAlmostEqual(high, 0.60)


if __name__ == "__main__":
    unittest.main()
