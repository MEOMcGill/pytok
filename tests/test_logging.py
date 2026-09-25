"""Session loggers and search stop-reason logging.

Offline: no browser, no network. The search routes are stubbed so each test drives
search_type down one exit.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from pytok.api.search import Search
from pytok.exceptions import ApiFailedException
from pytok.tiktok import PyTok


def _account(name):
    return SimpleNamespace(display_name=name, profile_dir=None)


# PyTok.__del__ is async, so collecting an unstarted PyTok warns.
@pytest.mark.filterwarnings("ignore:coroutine 'PyTok.__del__' was never awaited")
def test_session_level_does_not_reconfigure_the_pytok_logger():
    root = logging.getLogger("PyTok")
    root.setLevel(logging.INFO)
    try:
        quiet = PyTok(account=_account("quiet.one"), logging_level=logging.WARNING)
        loud = PyTok(account=_account("loud"))
        assert root.level == logging.INFO
        assert quiet.logger.name == "PyTok.quiet_one"
        assert quiet.logger.getEffectiveLevel() == logging.WARNING
        assert loud.logger.getEffectiveLevel() == logging.INFO
    finally:
        root.setLevel(logging.NOTSET)


class _Parent:
    def __init__(self):
        self.logger = logging.getLogger("PyTok.test_search")

    def video(self, data):
        return data


def _search(monkeypatch, harvest, scroll_pages=0, scroll_reason="no new results"):
    search = Search("alberta", parent=_Parent())

    async def api_fails(*args, **kwargs):
        raise ApiFailedException("empty body")
        yield

    async def noop(*args, **kwargs):
        pass

    async def harvest_page(obj_type):
        results, has_more = harvest
        search._exhausted_listing = not has_more
        return [({"id": i}, i) for i in results], has_more, len(results), "sid"

    async def scroll(obj_type, count):
        try:
            for i in range(scroll_pages * 12):
                yield {"id": f"s{i}"}, f"s{i}"
        finally:
            search._scroll_stop = scroll_reason

    monkeypatch.setattr(search, "_search_type_api", api_fails)
    monkeypatch.setattr(search, "_load_search_page", noop)
    monkeypatch.setattr(search, "_harvest_page_results", harvest_page)
    monkeypatch.setattr(search, "_scroll_for_results", scroll)
    return search


def _run(search, count, stop_after=None):
    async def go():
        n = 0
        async for _ in search.videos(count=count):
            n += 1
            if n == stop_after:
                break
        return n
    return asyncio.run(go())


def _end_line(caplog):
    lines = [r for r in caplog.records if "ended with" in r.getMessage()]
    assert len(lines) == 1
    return lines[0]


def test_short_first_page_logs_why_it_stopped(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="PyTok")
    search = _search(monkeypatch, harvest=(range(7), False))
    assert _run(search, count=100) == 7
    line = _end_line(caplog)
    assert line.levelno == logging.INFO
    assert "'alberta'" in line.getMessage()
    assert "7 of 100" in line.getMessage()
    assert "short last page" in line.getMessage()


def test_scroll_stopping_short_of_count_is_a_warning(monkeypatch, caplog):
    search = _search(monkeypatch, harvest=(range(12), True), scroll_pages=1)
    assert _run(search, count=100) == 24
    line = _end_line(caplog)
    assert line.levelno == logging.WARNING
    assert "24 of 100" in line.getMessage()
    assert "scrolling stopped (no new results)" in line.getMessage()


def test_reaching_count_and_caller_stopping_are_info(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="PyTok")
    _run(_search(monkeypatch, harvest=(range(12), True), scroll_pages=5), count=20)
    assert "reached the requested count" in _end_line(caplog).getMessage()

    caplog.clear()
    _run(_search(monkeypatch, harvest=(range(12), True), scroll_pages=5), count=100, stop_after=3)
    line = _end_line(caplog)
    assert line.levelno == logging.INFO
    assert "caller stopped iterating" in line.getMessage()
