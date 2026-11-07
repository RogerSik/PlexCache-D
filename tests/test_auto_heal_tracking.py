"""Background auto-heal of stale tracking entries.

Media deleted or replaced outside PlexCache leaves exclude-list and
timestamps.json entries pointing at files that are gone. The CLI run only
cleans them at its start, so the dashboard showed tracking warnings for up to
a day. auto_heal_stale_entries() clears them from the 5-minute background
refresh — after a grace period, and never when the cache looks unmounted or a
run is moving files.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

GRACE = 30 * 60


def _make_service(tmp_path, extra_settings=None):
    cache_root = tmp_path / "cache" / "Movies"
    array_root = tmp_path / "user" / "Movies"
    cache_root.mkdir(parents=True)
    array_root.mkdir(parents=True)
    settings = {
        "path_mappings": [{
            "name": "Movies",
            "cache_path": str(cache_root),
            "real_path": str(array_root),
            "cacheable": True,
            "enabled": True,
        }],
    }
    settings.update(extra_settings or {})
    settings_file = tmp_path / "plexcache_settings.json"
    settings_file.write_text(json.dumps(settings), encoding="utf-8")

    (tmp_path / "data").mkdir()
    with patch("web.services.maintenance_service.SETTINGS_FILE", settings_file), \
         patch("web.services.maintenance_service.CONFIG_DIR", tmp_path), \
         patch("web.services.maintenance_service.DATA_DIR", tmp_path / "data"):
        from web.services.maintenance_service import MaintenanceService
        svc = MaintenanceService()
    svc.settings_file = settings_file
    svc.exclude_file = tmp_path / "plexcache_cached_files.txt"
    svc.timestamps_file = tmp_path / "data" / "timestamps.json"
    svc._get_pinned_cache_paths = MagicMock(return_value=set())
    svc.get_released_files = MagicMock(return_value=set())
    svc._record_auto_heal = MagicMock()
    return svc, cache_root


def _populate(svc, cache_root, present, gone):
    """Write tracking files listing present + gone; create only present files."""
    paths = []
    for name in present:
        p = cache_root / name
        p.write_text("x")
        paths.append(str(p))
    for name in gone:
        paths.append(str(cache_root / name))
    svc.exclude_file.write_text("".join(p + "\n" for p in paths), encoding="utf-8")
    svc.timestamps_file.write_text(
        json.dumps({p: {"cached_at": "2026-11-01T00:00:00"} for p in paths}),
        encoding="utf-8")
    return [str(cache_root / n) for n in gone]


@pytest.fixture
def idle_runners():
    runner = MagicMock(is_running=False)
    with patch("web.services.get_operation_runner", return_value=runner), \
         patch("web.services.get_maintenance_runner", return_value=runner):
        yield runner


def _exclude_lines(svc):
    return [l for l in svc.exclude_file.read_text().splitlines() if l]


def _timestamp_keys(svc):
    return set(json.loads(svc.timestamps_file.read_text()))


class TestGracePeriod:

    def test_first_sighting_removes_nothing(self, tmp_path, idle_runners):
        svc, root = _make_service(tmp_path)
        _populate(svc, root, ["a.mkv", "b.mkv", "c.mkv"], ["gone.mkv"])

        assert svc.auto_heal_stale_entries(now=1000.0) == {"exclude": 0, "timestamps": 0}
        assert len(_exclude_lines(svc)) == 4

    def test_removed_once_stale_for_the_grace_period(self, tmp_path, idle_runners):
        svc, root = _make_service(tmp_path)
        gone = _populate(svc, root, ["a.mkv", "b.mkv", "c.mkv"], ["gone.mkv"])

        svc.auto_heal_stale_entries(now=1000.0)
        assert svc.auto_heal_stale_entries(now=1000.0 + GRACE - 1)["exclude"] == 0
        removed = svc.auto_heal_stale_entries(now=1000.0 + GRACE)

        assert removed == {"exclude": 1, "timestamps": 1}
        assert gone[0] not in _exclude_lines(svc)
        assert gone[0] not in _timestamp_keys(svc)
        assert len(_exclude_lines(svc)) == 3
        svc._record_auto_heal.assert_called_once()

    def test_file_that_reappears_is_kept_and_forgotten(self, tmp_path, idle_runners):
        svc, root = _make_service(tmp_path)
        _populate(svc, root, ["a.mkv", "b.mkv", "c.mkv"], ["back.mkv"])

        svc.auto_heal_stale_entries(now=1000.0)
        (root / "back.mkv").write_text("x")  # e.g. a move finished
        svc.auto_heal_stale_entries(now=1000.0 + GRACE)

        assert len(_exclude_lines(svc)) == 4
        assert svc._auto_heal_first_seen["exclude"] == {}


class TestSafetyRails:

    def test_setting_off_disables_it(self, tmp_path, idle_runners):
        svc, root = _make_service(tmp_path, {"auto_heal_tracking": False})
        _populate(svc, root, ["a.mkv", "b.mkv", "c.mkv"], ["gone.mkv"])

        svc.auto_heal_stale_entries(now=1000.0)
        svc.auto_heal_stale_entries(now=1000.0 + GRACE)

        assert len(_exclude_lines(svc)) == 4

    def test_running_operation_resets_the_clock(self, tmp_path, idle_runners):
        svc, root = _make_service(tmp_path)
        _populate(svc, root, ["a.mkv", "b.mkv", "c.mkv"], ["gone.mkv"])

        svc.auto_heal_stale_entries(now=1000.0)
        idle_runners.is_running = True
        svc.auto_heal_stale_entries(now=1000.0 + GRACE)
        idle_runners.is_running = False
        # Grace starts over after the run; one more cycle is not enough.
        svc.auto_heal_stale_entries(now=1000.0 + GRACE + 300)

        assert len(_exclude_lines(svc)) == 4

    def test_missing_cache_dir_is_a_no_op(self, tmp_path, idle_runners):
        svc, root = _make_service(tmp_path)
        _populate(svc, root, ["a.mkv"], ["gone.mkv"])
        svc.auto_heal_stale_entries(now=1000.0)
        for f in root.iterdir():
            f.unlink()
        root.rmdir()  # pool unmounted

        svc.auto_heal_stale_entries(now=1000.0 + GRACE)

        assert len(_exclude_lines(svc)) == 2

    def test_mass_disappearance_is_left_alone(self, tmp_path, idle_runners):
        svc, root = _make_service(tmp_path)
        present = [f"keep{i}.mkv" for i in range(10)]
        gone = [f"gone{i}.mkv" for i in range(30)]
        _populate(svc, root, present, gone)

        svc.auto_heal_stale_entries(now=1000.0)
        removed = svc.auto_heal_stale_entries(now=1000.0 + GRACE)

        assert removed == {"exclude": 0, "timestamps": 0}
        assert len(_exclude_lines(svc)) == 40

    def test_pinned_and_released_paths_are_never_removed(self, tmp_path, idle_runners):
        svc, root = _make_service(tmp_path)
        gone = _populate(svc, root, ["a.mkv", "b.mkv", "c.mkv"],
                         ["pinned.mkv", "released.mkv", "gone.mkv"])
        svc._get_pinned_cache_paths.return_value = {gone[0]}
        svc.get_released_files.return_value = {gone[1]}

        svc.auto_heal_stale_entries(now=1000.0)
        svc.auto_heal_stale_entries(now=1000.0 + GRACE)

        lines = _exclude_lines(svc)
        assert gone[0] in lines and gone[1] in lines
        assert gone[2] not in lines


class TestRemoveExcludeEntries:

    def test_keeps_unrelated_lines_verbatim_and_in_order(self, tmp_path):
        svc, root = _make_service(tmp_path)
        svc.exclude_file.write_text(
            f"{root}/b.mkv\n\n{root}/gone.mkv\n{root}/a.mkv\n", encoding="utf-8")

        assert svc._remove_exclude_entries({f"{root}/gone.mkv"}) == 1
        assert svc.exclude_file.read_text() == f"{root}/b.mkv\n\n{root}/a.mkv\n"


class TestCleanExcludeKeepsPinned:

    def test_pinned_stale_entry_survives_manual_cleanup(self, tmp_path):
        """clean_exclude counted pinned entries as kept but rewrote the file
        from exclude & cache, dropping them anyway."""
        svc, root = _make_service(tmp_path)
        gone = _populate(svc, root, ["a.mkv"], ["pinned.mkv", "gone.mkv"])
        svc._get_pinned_cache_paths.return_value = {gone[0]}

        result = svc.clean_exclude(dry_run=False)

        assert result.affected_count == 1
        lines = _exclude_lines(svc)
        assert gone[0] in lines and gone[1] not in lines


class TestUntrackedNeedsDecision:

    def test_cache_only_files_need_no_decision(self):
        from web.services.maintenance_service import UntrackedFile
        new = UntrackedFile("/c/a.mkv", "a.mkv", 1, "1 B", False, None, False, None, "sync_to_array")
        backup = UntrackedFile("/c/b.mkv", "b.mkv", 1, "1 B", True, "/a/b.mkv.plexcached", False, None, "fix_with_backup")
        dup = UntrackedFile("/c/c.mkv", "c.mkv", 1, "1 B", False, None, True, "/a/c.mkv", "fix_with_backup")
        assert not new.needs_decision
        assert backup.needs_decision and dup.needs_decision

    def test_groups_needing_a_decision_sort_first(self, tmp_path):
        from web.services.maintenance_service import UntrackedFile
        svc, _ = _make_service(tmp_path)
        files = [
            UntrackedFile("/c/A/a.mkv", "a.mkv", 1, "1 B", False, None, False, None, "sync_to_array"),
            UntrackedFile("/c/Z/z.mkv", "z.mkv", 1, "1 B", False, None, True, "/a/z.mkv", "fix_with_backup"),
        ]
        groups = svc._group_untracked_by_directory(files)
        assert [g["primary"].filename for g in groups] == ["z.mkv", "a.mkv"]
        assert groups[0]["needs_decision"] and not groups[1]["needs_decision"]


class TestWebCacheHook:

    def test_pre_refresh_hook_runs_before_callbacks(self, tmp_path):
        from web.services.web_cache import WebCacheService
        with patch("web.services.web_cache.DATA_DIR", tmp_path):
            cache = WebCacheService()
        cache._disk_cache_file = tmp_path / "web_ui_cache.json"
        order = []
        cache.register_pre_refresh_hook(lambda: order.append("hook"))
        cache.register_refresh_callback("k", lambda: order.append("refresh") or {})
        cache.refresh_all()
        assert order == ["hook", "refresh"]

    def test_failing_hook_does_not_block_refresh(self, tmp_path):
        from web.services.web_cache import WebCacheService
        with patch("web.services.web_cache.DATA_DIR", tmp_path):
            cache = WebCacheService()
        cache._disk_cache_file = tmp_path / "web_ui_cache.json"
        ran = []

        def boom():
            raise RuntimeError("nope")
        cache.register_pre_refresh_hook(boom)
        cache.register_refresh_callback("k", lambda: ran.append(1) or {})
        cache.refresh_all()
        assert ran == [1]
