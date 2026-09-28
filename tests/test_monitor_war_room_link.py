from pathlib import Path


def test_monitor_header_links_to_war_room_simple_mode() -> None:
    html = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="war-room-link"' in html
    assert 'href="/war-room/simple"' in html
    assert '>War Room</a>' in html
