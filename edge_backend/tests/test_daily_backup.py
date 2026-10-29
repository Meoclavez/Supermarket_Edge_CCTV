"""Nightly automatic backup: timing in store time, and the scheduler loop."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services import backup_service as bs


def test_next_run_is_tonight_or_tomorrow_at_three_store_time():
    tz = ZoneInfo("Australia/Melbourne")
    before = datetime(2026, 10, 14, 1, 30, tzinfo=tz)
    assert bs.seconds_until_daily_backup(before) == 90 * 60
    after = datetime(2026, 10, 14, 3, 0, tzinfo=tz)
    assert bs.seconds_until_daily_backup(after) == 24 * 3600
    late = datetime(2026, 10, 14, 22, 0, tzinfo=tz)
    assert bs.seconds_until_daily_backup(late) == 5 * 3600


def test_next_run_counts_real_time_across_daylight_saving_changes():
    tz = ZoneInfo("Australia/Melbourne")
    # 4 Oct 2026: clocks jump 02:00 -> 03:00, so 01:30 -> 03:00 is 30 real minutes
    # (wall-clock subtraction said 90 and ran the backup at 04:00).
    assert bs.seconds_until_daily_backup(datetime(2026, 10, 4, 1, 30, tzinfo=tz)) == 30 * 60
    # 5 Apr 2026: clocks fall back 03:00 -> 02:00, so 22:00 -> 03:00 is 6 real hours.
    assert bs.seconds_until_daily_backup(datetime(2026, 4, 4, 22, 0, tzinfo=tz)) == 6 * 3600


def test_scheduler_backs_up_with_auto_tag_and_applies_retention(monkeypatch):
    calls = []

    class FakeService:
        def create_backup(self, tag="auto", dedupe=None):
            calls.append(("create", tag))
            return {"status": "created", "filename": "x.db"}

        def apply_retention(self, now=None):
            calls.append(("retention",))
            return {}

    monkeypatch.setattr(bs, "seconds_until_daily_backup", lambda now: 0.01)

    async def run():
        sched = bs.DailyBackupScheduler(FakeService())
        await sched.start()
        for _ in range(100):
            if ("retention",) in calls:
                break
            await asyncio.sleep(0.01)
        await sched.stop()

    asyncio.run(run())
    assert calls[0] == ("create", "auto")
    assert ("retention",) in calls


def test_a_failed_night_does_not_stop_the_scheduler(monkeypatch):
    attempts = []

    class FailingService:
        def create_backup(self, tag="auto", dedupe=None):
            attempts.append(tag)
            raise OSError("disk full")

        def apply_retention(self, now=None):
            return {}

    monkeypatch.setattr(bs, "seconds_until_daily_backup", lambda now: 0.01)

    async def run():
        sched = bs.DailyBackupScheduler(FailingService())
        await sched.start()
        for _ in range(200):
            if len(attempts) >= 2:
                break
            await asyncio.sleep(0.01)
        await sched.stop()

    asyncio.run(run())
    assert len(attempts) >= 2
