import pytest

import content_sources as cs


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "DB_PATH", str(tmp_path / "test.db"))


def test_seeds_defaults_on_first_use():
    sources = cs.list_active_sources()
    assert len(sources) == 3
    urls = {s["url"] for s in sources}
    assert "https://www.oneusefulthing.org/feed" in urls
    assert "https://www.latent.space/feed" in urls
    assert "https://www.xataka.com/feedburner.xml" in urls


def test_seed_not_repeated_on_second_call():
    cs.list_active_sources()
    sources = cs.list_active_sources()
    assert len(sources) == 3


def test_add_source_appears_in_list():
    cs.list_active_sources()  # trigger seed
    cs.add_source("My Blog", "https://myblog.com/feed", "rss")
    sources = cs.list_active_sources()
    names = [s["name"] for s in sources]
    assert "My Blog" in names


def test_add_source_returns_ok():
    result = cs.add_source("Test Feed", "https://test.com/feed", "rss")
    assert result["status"] == "ok"
    assert result["name"] == "Test Feed"
    assert result["url"] == "https://test.com/feed"
    assert result["type"] == "rss"


def test_add_duplicate_name_raises():
    cs.list_active_sources()  # trigger seed
    cs.add_source("Dup", "https://a.com/feed", "rss")
    with pytest.raises(Exception):
        cs.add_source("Dup", "https://b.com/feed", "rss")


def test_deactivate_removes_from_active_list():
    sources_before = cs.list_active_sources()
    name = sources_before[0]["name"]
    cs.deactivate_source(name)
    active_names = [s["name"] for s in cs.list_active_sources()]
    assert name not in active_names


def test_deactivate_preserves_historical_record():
    sources = cs.list_active_sources()
    name = sources[0]["name"]
    cs.deactivate_source(name)
    # Still in the table (active=0), just not listed as active
    with cs._conn() as conn:
        row = conn.execute(
            "SELECT active FROM content_sources WHERE name = ?", (name,)
        ).fetchone()
    assert row is not None
    assert row[0] == 0


def test_deactivate_nonexistent_does_not_raise():
    cs.list_active_sources()
    result = cs.deactivate_source("does-not-exist")
    assert result["status"] == "ok"


def test_source_fields_present():
    sources = cs.list_active_sources()
    for s in sources:
        assert "id" in s
        assert "name" in s
        assert "url" in s
        assert "type" in s
        assert "added_at" in s


def test_all_defaults_are_rss():
    sources = cs.list_active_sources()
    for s in sources:
        assert s["type"] == "rss"
