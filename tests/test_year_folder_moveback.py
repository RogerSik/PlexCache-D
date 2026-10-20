"""Move-back decisions for movies stored in year folders (#232).

Layout: media/Film/2012/<movie>.mkv. The folder-name fallback reads a
numbers-only folder as a TV season, so a movie still on a user's OnDeck
became "Film, season 2012, episode 4" (the "e 4" in "glaciale 4"), was queued
for move-back, then kept by the later filter and reported as a failed move on
every run. In the other direction every movie in those folders shared the
name "Film", so one OnDeck movie kept every watched one on cache.
"""

import os
import sys
import types

import pytest

for _mod in ("fcntl", "pwd", "grp"):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

from core.file_operations import (  # noqa: E402
    CacheTimestampTracker,
    FileFilter,
    OnDeckTracker,
    WatchlistTracker,
)

ONDECK_NAME = "L'era glaciale 4 - Continenti alla deriva (2012) [IT+EN] [Bluray-1080p x265] [DTS 5.1].mkv"
WATCHED_NAME = "Inception (2010) [Bluray-1080p].mkv"


@pytest.fixture
def env(tmp_path):
    cache_dir = tmp_path / "cache"
    array_dir = tmp_path / "array"
    ondeck_cache = cache_dir / "Film" / "2012" / ONDECK_NAME
    watched_cache = cache_dir / "Film" / "2010" / WATCHED_NAME
    for f in (ondeck_cache, watched_cache):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"\0" * 1024)

    exclude_file = tmp_path / "exclude.txt"
    exclude_file.write_text(f"{ondeck_cache}\n{watched_cache}\n", encoding="utf-8")

    # "pre-existing" entries carry no media_type, which is what sends the
    # classification to the folder-name fallback.
    ts = CacheTimestampTracker(str(tmp_path / "timestamps.json"))
    ts.record_cache_time(str(ondeck_cache), source="pre-existing")
    ts.record_cache_time(str(watched_cache), source="pre-existing")

    ondeck_real = str(array_dir / "Film" / "2012" / ONDECK_NAME)
    od = OnDeckTracker(str(tmp_path / "ondeck.json"))
    od.update_entry(ondeck_real, "Unicorno")

    ff = FileFilter(
        real_source=str(array_dir),
        cache_dir=str(cache_dir),
        is_unraid=False,
        mover_cache_exclude_file=str(exclude_file),
        timestamp_tracker=ts,
        cache_retention_hours=0,
        ondeck_tracker=od,
        watchlist_tracker=WatchlistTracker(str(tmp_path / "watchlist.json")),
    )
    ff.set_media_info_map({ondeck_real: {"media_type": "movie", "episode_info": None}})
    return {
        "ff": ff,
        "ondeck_real": ondeck_real,
        "ondeck_cache": str(ondeck_cache),
        "watched_cache": str(watched_cache),
        "watched_real": str(array_dir / "Film" / "2010" / WATCHED_NAME),
    }


def _move_back(env):
    return env["ff"].get_files_to_move_back_to_array({env["ondeck_real"]}, set(), files_to_skip=set())


def test_ondeck_movie_in_year_folder_is_kept(env):
    to_move, _, exclude_paths = _move_back(env)
    assert env["ondeck_real"] not in to_move
    assert env["ondeck_cache"] not in exclude_paths


def test_watched_movie_in_year_folder_is_moved_back(env):
    """Another movie on OnDeck must not keep this one on cache."""
    to_move, _, exclude_paths = _move_back(env)
    assert env["watched_real"] in to_move
    assert env["watched_cache"] in exclude_paths


def test_ondeck_movie_kept_by_metadata_without_exact_path(env):
    """Same file reached through a different real path spelling still matches by name."""
    ff = env["ff"]
    other_real = env["ondeck_real"].replace(os.sep + "array" + os.sep, os.sep + "array2" + os.sep)
    ff.set_media_info_map({other_real: {"media_type": "movie", "episode_info": None},
                           env["ondeck_real"]: {"media_type": "movie", "episode_info": None}})
    to_move, _, _ = ff.get_files_to_move_back_to_array({other_real}, set(), files_to_skip=set())
    assert env["ondeck_real"] not in to_move


def test_known_movie_name_ignores_year_folder():
    ff = FileFilter.__new__(FileFilter)
    path = os.path.join(os.sep, "media", "Film", "2012", ONDECK_NAME)
    assert ff._extract_media_name(path) == "Film"            # unchanged fallback
    assert ff._extract_media_name(path, known_movie=True) != "Film"


def test_tv_numeric_season_folders_still_work():
    """Date-based shows use numbers-only season folders; keep reading them as TV."""
    ff = FileFilter.__new__(FileFilter)
    path = os.path.join(os.sep, "tv", "Daily Show", "2012", "Daily Show - S2012E05.mkv")
    assert ff._extract_tv_info(path) == ("Daily Show", 2012, 5)


# -- app-level guard -------------------------------------------------------------

def test_files_kept_this_run_are_never_queued_for_move_back(monkeypatch):
    """Anything in all_active_media is dropped from the move-back lists, so it is
    neither reported as a restore nor counted as a failed array move."""
    from unittest.mock import MagicMock
    from core.app import PlexCacheApp

    app = PlexCacheApp.__new__(PlexCacheApp)
    app.ondeck_items = set()
    app.watchlist_items = set()
    app.files_to_skip = []
    app.pinned_paths_cache = set()
    app.media_to_array = []
    app.dry_run = True
    app.all_active_media = ["/mnt/user/media/Film/2012/kept.mkv"]
    app.file_filter = MagicMock()
    app.file_filter.get_files_to_move_back_to_array.return_value = (
        ["/mnt/user/media/Film/2012/kept.mkv", "/mnt/user/media/Film/2010/watched.mkv"],
        [],
        ["/mnt/cache/media/Film/2012/kept.mkv", "/mnt/cache/media/Film/2010/watched.mkv"],
    )

    app._check_files_to_move_back_to_array()

    assert app.media_to_array == ["/mnt/user/media/Film/2010/watched.mkv"]
    assert app._move_back_exclude_paths == ["/mnt/cache/media/Film/2010/watched.mkv"]
