"""Tests for the optional Jellyfin OnDeck source (core/jellyfin_api.py)."""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

sys.modules['fcntl'] = MagicMock()
for _mod in [
    'apscheduler', 'apscheduler.schedulers',
    'apscheduler.schedulers.background', 'apscheduler.triggers',
    'apscheduler.triggers.cron', 'apscheduler.triggers.interval',
    'plexapi', 'plexapi.server', 'plexapi.video', 'plexapi.myplex',
    'plexapi.library', 'plexapi.exceptions', 'requests',
]:
    sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.jellyfin_api import JellyfinManager


MAPPINGS = {"/media/en/": "/mnt/user/plex-media/", "/media/": "/data/"}


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%S.0000000Z')


def _episode(item_id, season, episode, path, runtime_min=45, series_id="s1", last_played=None):
    return {
        "Id": item_id, "Type": "Episode", "Name": f"Ep {season}x{episode}",
        "SeriesId": series_id, "SeriesName": "Show",
        "ParentIndexNumber": season, "IndexNumber": episode,
        "RunTimeTicks": runtime_min * 60 * 10_000_000,
        "MediaSources": [{"Path": path}],
        "UserData": {"LastPlayedDate": last_played} if last_played else {},
    }


class FakeSession:
    """Routes GET requests to canned JSON by path; raises for paths mapped to an exception."""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        path = url.split("http://jf", 1)[1]
        self.calls.append((path, params))
        result = self.routes[path]
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result(params)
        response = MagicMock()
        response.json.return_value = result
        return response


def _manager(routes, **kwargs):
    return JellyfinManager("http://jf/", "key", path_mappings=MAPPINGS, session=FakeSession(routes), **kwargs)


def _routes(resume=None, next_up=None, episodes=None, users=None):
    return {
        "/Users": users if users is not None else [{"Id": "u1", "Name": "roger"}],
        "/UserItems/Resume": {"Items": resume or []},
        "/Shows/NextUp": {"Items": next_up or []},
        "/Shows/s1/Episodes": {"Items": episodes or []},
    }


class TestPathTranslation:
    def test_longest_prefix_wins(self):
        m = _manager({})
        assert m.to_plex_path("/media/en/Movies/a.mkv") == "/mnt/user/plex-media/Movies/a.mkv"
        assert m.to_plex_path("/media/de/Movies/a.mkv") == "/data/de/Movies/a.mkv"

    def test_unmapped_path_is_dropped(self):
        m = _manager({})
        assert m.to_plex_path("/other/a.mkv") is None
        assert m._item_paths({"Path": "/other/a.mkv"}) == []

    def test_all_versions_and_path_fallback(self):
        m = _manager({})
        item = {"MediaSources": [{"Path": "/media/en/a.mkv"}, {"Path": "/media/en/a-4k.mkv"}]}
        assert m._item_paths(item) == ["/mnt/user/plex-media/a.mkv", "/mnt/user/plex-media/a-4k.mkv"]
        assert m._item_paths({"Path": "/media/en/b.mkv"}) == ["/mnt/user/plex-media/b.mkv"]

    def test_auth_header(self):
        m = _manager({})
        assert m.session.headers["Authorization"] == 'MediaBrowser Client="PlexCache-D", Token="key"'


class TestOnDeck:
    def test_movie_and_episode_with_prefetch(self):
        movie = {"Id": "m1", "Type": "Movie", "Name": "Film", "Path": "/media/en/Movies/film.mkv",
                 "UserData": {"LastPlayedDate": _ago(1)}}
        current = _episode("e2", 1, 2, "/media/en/TV/s01e02.mkv")
        episodes = [_episode(f"e{i}", 1, i, f"/media/en/TV/s01e0{i}.mkv") for i in range(1, 6)]
        m = _manager(_routes(resume=[movie], next_up=[current], episodes=episodes))

        items = m.get_on_deck_media(days_to_monitor=30, number_episodes=2)

        assert [(i.file_path, i.is_current_ondeck) for i in items] == [
            ("/mnt/user/plex-media/Movies/film.mkv", True),
            ("/mnt/user/plex-media/TV/s01e02.mkv", True),
            ("/mnt/user/plex-media/TV/s01e03.mkv", False),
            ("/mnt/user/plex-media/TV/s01e04.mkv", False),
        ]
        assert all(i.username == "roger" for i in items)
        assert items[1].episode_info == {"show": "Show", "season": 1, "episode": 2}
        assert items[3].episode_info == {"show": "Show", "season": 1, "episode": 4}
        assert m.is_ondeck_data_complete()

    def test_prefetch_minimum_minutes_uses_runtime(self):
        current = _episode("e1", 1, 1, "/media/en/TV/e1.mkv")
        episodes = [_episode(f"e{i}", 1, i, f"/media/en/TV/e{i}.mkv", runtime_min=20) for i in range(1, 8)]
        m = _manager(_routes(next_up=[current], episodes=episodes))

        items = m.get_on_deck_media(days_to_monitor=30, number_episodes=1, prefetch_minimum_minutes=60)

        # 3 x 20 min needed to cover 60 minutes
        assert len([i for i in items if not i.is_current_ondeck]) == 3

    def test_item_in_resume_and_next_up_counted_once(self):
        current = _episode("e1", 1, 1, "/media/en/TV/e1.mkv", last_played=_ago(1))
        m = _manager(_routes(resume=[current], next_up=[current], episodes=[current]))

        items = m.get_on_deck_media(days_to_monitor=30, number_episodes=1)

        assert [i.file_path for i in items] == ["/mnt/user/plex-media/TV/e1.mkv"]

    def test_stale_resume_item_is_skipped(self):
        movie = {"Id": "m1", "Type": "Movie", "Path": "/media/en/old.mkv",
                 "UserData": {"LastPlayedDate": _ago(90)}}
        m = _manager(_routes(resume=[movie]))

        assert m.get_on_deck_media(days_to_monitor=30, number_episodes=1) == []

    def test_next_up_cutoff_sent_to_server(self):
        m = _manager(_routes())
        m.get_on_deck_media(days_to_monitor=30, number_episodes=1)

        params = next(p for path, p in m.session.calls if path == "/Shows/NextUp")
        cutoff = datetime.strptime(params["nextUpDateCutoff"], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
        assert abs((datetime.now(timezone.utc) - cutoff) - timedelta(days=30)) < timedelta(minutes=1)

    def test_skip_users(self):
        movie = {"Id": "m1", "Type": "Movie", "Path": "/media/en/a.mkv"}
        users = [{"Id": "u1", "Name": "roger"}, {"Id": "u2", "Name": "kid"}]
        m = _manager(_routes(resume=[movie], users=users), skip_users=["kid"])

        items = m.get_on_deck_media(days_to_monitor=30, number_episodes=1)

        assert [i.username for i in items] == ["roger"]
        assert [p["userId"] for path, p in m.session.calls if path == "/UserItems/Resume"] == ["u1"]

    @pytest.mark.critical
    def test_user_list_failure_marks_incomplete(self):
        m = _manager({"/Users": RuntimeError("down")})

        assert m.get_on_deck_media(days_to_monitor=30, number_episodes=1) == []
        assert not m.is_ondeck_data_complete()

    @pytest.mark.critical
    def test_single_user_failure_marks_incomplete(self):
        routes = _routes()
        routes["/UserItems/Resume"] = RuntimeError("timeout")
        m = _manager(routes)

        m.get_on_deck_media(days_to_monitor=30, number_episodes=1)

        assert not m.is_ondeck_data_complete()

    def test_complete_flag_resets_on_next_run(self):
        routes = _routes()
        routes["/Users"] = RuntimeError("down")
        m = _manager(routes)
        m.get_on_deck_media(days_to_monitor=30, number_episodes=1)
        routes["/Users"] = [{"Id": "u1", "Name": "roger"}]

        m.get_on_deck_media(days_to_monitor=30, number_episodes=1)

        assert m.is_ondeck_data_complete()


class TestActiveSessions:
    def test_path_from_now_playing_item(self):
        sessions = [{"UserId": "u1", "NowPlayingItem": {"Id": "m1", "Path": "/media/en/a.mkv"}}, {"UserId": "u2"}]
        m = _manager({"/Sessions": sessions})

        assert m.get_active_session_paths() == ["/mnt/user/plex-media/a.mkv"]

    def test_fetches_item_when_path_missing(self):
        sessions = [{"UserId": "u1", "NowPlayingItem": {"Id": "m1"}}]
        m = _manager({"/Sessions": sessions, "/Items": {"Items": [{"Path": "/media/en/a.mkv"}]}})

        assert m.get_active_session_paths() == ["/mnt/user/plex-media/a.mkv"]
        assert m.session.calls[-1][1]["userId"] == "u1"

    def test_failure_returns_empty(self):
        m = _manager({"/Sessions": RuntimeError("down")})

        assert m.get_active_session_paths() == []


class TestConfig:
    def _load(self, tmp_path, monkeypatch, settings, env=None):
        from core.config import ConfigManager
        for key in ("JELLYFIN_URL", "JELLYFIN_API_KEY", "JELLYFIN_PATH_MAPPINGS", "JELLYFIN_SKIP_USERS"):
            monkeypatch.delenv(key, raising=False)
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        cm = ConfigManager(str(tmp_path / "settings.json"))
        cm.settings_data = settings
        cm._load_jellyfin_config()
        return cm.jellyfin

    def test_disabled_by_default(self, tmp_path, monkeypatch):
        assert not self._load(tmp_path, monkeypatch, {}).enabled

    def test_settings_file(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {
            "jellyfin_url": "http://jf", "jellyfin_api_key": "k",
            "jellyfin_path_mappings": MAPPINGS, "jellyfin_skip_users": ["kid"],
        })
        assert cfg.enabled
        assert cfg.path_mappings == MAPPINGS
        assert cfg.skip_users == ["kid"]

    def test_env_overrides_settings(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {"jellyfin_url": "http://file", "jellyfin_api_key": "file"},
                         env={"JELLYFIN_URL": "http://env", "JELLYFIN_API_KEY": "env"})
        assert (cfg.url, cfg.api_key) == ("http://env", "env")

    def test_env_mappings_and_skip_users(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {"jellyfin_skip_users": ["file"]}, env={
            "JELLYFIN_PATH_MAPPINGS": '{"/media/en/": "/mnt/user/plex-media/"}',
            "JELLYFIN_SKIP_USERS": "kid, guest",
        })
        assert cfg.path_mappings == {"/media/en/": "/mnt/user/plex-media/"}
        assert cfg.skip_users == ["kid", "guest"]

    def test_invalid_env_mappings_keep_settings(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {"jellyfin_path_mappings": MAPPINGS},
                         env={"JELLYFIN_PATH_MAPPINGS": "not json"})
        assert cfg.path_mappings == MAPPINGS
