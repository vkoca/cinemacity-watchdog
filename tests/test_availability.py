import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watch

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(watch.time, "sleep", lambda *_args, **_kwargs: None)


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def test_seatplan_capacity_parses_real_fixture(monkeypatch):
    """IMAX VOLVO (Praha Flora) má podle veřejného, nechráněného plánu sálu 385 míst."""
    watch._capacity_cache.clear()
    monkeypatch.setattr(watch, "tickets_api", lambda path, method="GET": load_fixture("seatplan_imax_volvo.json"))
    assert watch.seatplan_capacity(80, 1) == 385


def test_seatplan_capacity_is_cached(monkeypatch):
    watch._capacity_cache.clear()
    calls = []

    def fake_tickets_api(path, method="GET"):
        calls.append(path)
        return load_fixture("seatplan_imax_volvo.json")

    monkeypatch.setattr(watch, "tickets_api", fake_tickets_api)
    watch.seatplan_capacity(80, 1)
    watch.seatplan_capacity(80, 1)
    assert len(calls) == 1


def make_event(sold_out, ratio):
    return {
        "id": "1",
        "presentationCode": "1",
        "soldOut": sold_out,
        "availabilityRatio": ratio,
    }


def test_enrich_availability_flags_low_free_count_as_wheelchair_only(monkeypatch):
    watch._capacity_cache.clear()
    monkeypatch.setattr(watch, "seatplan_capacity", lambda venue_id, seatplan_id: 100)
    monkeypatch.setattr(
        watch, "tickets_api", lambda path, method="GET": {"presentation": {"venueId": 80, "seatplanId": 1}}
    )

    event = make_event(sold_out=False, ratio=0.05)  # 5 volných z 100
    watch.enrich_availability(event)

    assert event["freeSeats"] == 5
    assert event["likelyWheelchairOnly"] is True


def test_enrich_availability_does_not_flag_real_availability(monkeypatch):
    watch._capacity_cache.clear()
    monkeypatch.setattr(watch, "seatplan_capacity", lambda venue_id, seatplan_id: 100)
    monkeypatch.setattr(
        watch, "tickets_api", lambda path, method="GET": {"presentation": {"venueId": 80, "seatplanId": 1}}
    )

    event = make_event(sold_out=False, ratio=0.20)  # 20 volných z 100
    watch.enrich_availability(event)

    assert event["freeSeats"] == 20
    assert event["likelyWheelchairOnly"] is False


def test_enrich_availability_never_overrides_already_sold_out(monkeypatch):
    watch._capacity_cache.clear()
    monkeypatch.setattr(watch, "seatplan_capacity", lambda venue_id, seatplan_id: 100)
    monkeypatch.setattr(
        watch, "tickets_api", lambda path, method="GET": {"presentation": {"venueId": 80, "seatplanId": 1}}
    )

    event = make_event(sold_out=True, ratio=0.05)
    watch.enrich_availability(event)

    assert event["likelyWheelchairOnly"] is False


def test_enrich_availability_is_fail_soft_on_network_error(monkeypatch):
    watch._capacity_cache.clear()

    def boom(path, method="GET"):
        raise RuntimeError("tickets API selhalo po 4 pokusech")

    monkeypatch.setattr(watch, "tickets_api", boom)

    event = make_event(sold_out=False, ratio=0.05)
    watch.enrich_availability(event)  # nesmí vyhodit výjimku

    assert "freeSeats" not in event
    assert "likelyWheelchairOnly" not in event


def test_enrich_availability_skips_when_ratio_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(watch, "tickets_api", lambda path, method="GET": calls.append(path))

    event = make_event(sold_out=False, ratio=None)
    watch.enrich_availability(event)

    assert calls == []
    assert "freeSeats" not in event
