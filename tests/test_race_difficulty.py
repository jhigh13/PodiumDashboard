from datetime import date

import pytest

import app.services.race_difficulty as rd


# ── parse_event_prog_key ──────────────────────────────────────────────────────

def test_parse_event_prog_key():
    assert rd.parse_event_prog_key("123:456") == (123, 456)
    assert rd.parse_event_prog_key(" 123:456 ") == (123, 456)
    assert rd.parse_event_prog_key("123") is None
    assert rd.parse_event_prog_key("a:b") is None
    assert rd.parse_event_prog_key("") is None
    assert rd.parse_event_prog_key(None) is None


# ── sport matching / default pick ─────────────────────────────────────────────

def test_pick_default_prefers_bike():
    workouts = [
        {"workout_id": "1", "sport": "Run"},
        {"workout_id": "2", "sport": "Cycling"},
        {"workout_id": "3", "sport": "Bike"},
    ]
    assert rd.pick_default_workout(workouts, "bike") == "2"
    assert rd.pick_default_workout([{"workout_id": "9", "sport": "Swim"}], "bike") == "9"
    assert rd.pick_default_workout([], "bike") is None


# ── auto_trim_indices ─────────────────────────────────────────────────────────

def test_auto_trim_strips_leading_and_trailing_dead_time():
    t = [float(i) for i in range(10)]
    power = [0, 0, 0, 200, 210, 220, 215, 0, 0, 0]
    speed = [0.0] * 10
    start, end = rd.auto_trim_indices(t, power, speed)
    assert (start, end) == (3, 6)


def test_auto_trim_leading_gap_picks_race_after_it():
    # Pre-race pause: a few samples, a >60s gap, then the bike leg.
    t = [0.0, 10.0, 20.0, 100.0, 101.0, 102.0]
    power = [0, 150, 150, 250, 250, 250]
    speed = [0.0] * 6
    assert rd.auto_trim_indices(t, power, speed) == (3, 5)


def test_auto_trim_trailing_gap_keeps_race_before_it():
    # Bike leg first, then a long gap and a single null-power "stop" record
    # (the real-world case that produced 1-sample streams).
    t = [0.0, 1.0, 2.0, 3.0, 2000.0]
    power = [0, 300, 310, 305, None]
    speed = [0.0] * 5
    assert rd.auto_trim_indices(t, power, speed) == (1, 3)


def test_auto_trim_gaps_on_both_sides():
    # Pre-race gap AND post-race gap around the bike leg.
    t = [0.0, 100.0, 101.0, 102.0, 500.0]
    power = [0, 280, 290, 285, None]
    speed = [0.0] * 5
    assert rd.auto_trim_indices(t, power, speed) == (1, 3)


def test_auto_trim_all_zero_degenerates_to_full_range():
    t = [0.0, 1.0, 2.0]
    assert rd.auto_trim_indices(t, [0, 0, 0], [0.0, 0.0, 0.0]) == (0, 2)


def test_auto_trim_empty():
    assert rd.auto_trim_indices([], [], []) == (0, -1)


# ── downsample_series ─────────────────────────────────────────────────────────

def test_downsample_caps_length_and_keeps_small_series():
    t = [float(i) for i in range(5000)]
    wkg = [3.0] * 5000
    t2, w2 = rd.downsample_series(t, wkg, max_points=1200)
    assert len(t2) <= 1200
    assert len(t2) == len(w2)
    assert t2[0] == 0.0

    t3, w3 = rd.downsample_series([0.0, 1.0], [1.0, 2.0], max_points=1200)
    assert (t3, w3) == ([0.0, 1.0], [1.0, 2.0])


# ── group_race_rows ───────────────────────────────────────────────────────────

def test_group_race_rows_dedupes_and_collects_participants():
    rows = [
        {"event_id": 1, "prog_id": 10, "athlete_id": 100, "event_date": date(2026, 5, 1),
         "event_name": "WTCS Yokohama", "event_venue": "Yokohama", "prog_name": "Elite Men",
         "prog_distance_category": "standard"},
        {"event_id": 1, "prog_id": 10, "athlete_id": 200, "event_date": date(2026, 5, 1),
         "event_name": "WTCS Yokohama", "event_venue": "Yokohama", "prog_name": "Elite Men",
         "prog_distance_category": "standard"},
        {"event_id": 2, "prog_id": 20, "athlete_id": 100, "event_date": date(2026, 3, 1),
         "event_name": "World Cup Napier", "event_venue": "Napier", "prog_name": "Elite Men",
         "prog_distance_category": "sprint"},
    ]
    podium = {
        100: {"podium_athlete_id": 1, "name": "Alice"},
        200: {"podium_athlete_id": 2, "name": "Bob"},
    }
    races = rd.group_race_rows(rows, podium)
    assert len(races) == 2
    # Sorted by date
    assert races[0]["event_name"] == "World Cup Napier"
    assert races[1]["key"] == "1:10"
    assert races[1]["participants"] == ["Alice", "Bob"]
    assert races[0]["participants"] == ["Alice"]


# ── extract_power_series / build_athlete_stream ───────────────────────────────

def _payload(power_values, speed_values=None, step_ms=1000):
    n = len(power_values)
    speed_values = speed_values or [0.0] * n
    return {
        "WorkoutChannels": {
            "Channels": ["Power", "Speed"],
            "Data": [
                {"Event": "", "MillisecondOffset": i * step_ms,
                 "Values": [power_values[i], speed_values[i]]}
                for i in range(n)
            ],
        }
    }


def test_extract_power_series_converts_ms_to_seconds():
    t, power, speed = rd.extract_power_series(_payload([100, 110, 120]))
    assert t == [0.0, 1.0, 2.0]
    assert power == [100.0, 110.0, 120.0]


def test_build_athlete_stream_trims_and_computes_wkg():
    power = [0, 0, 300, 320, 310, 0, 0]
    stream, reason = rd.build_athlete_stream(_payload(power), weight_kg=70.0)
    assert reason is None
    assert stream["auto_trim"] == {"lead_s": 2, "trail_s": 2}
    assert stream["t"][0] == 0.0
    assert stream["duration_s"] == 2
    assert stream["wkg"] == [round(300 / 70.0, 3), round(320 / 70.0, 3), round(310 / 70.0, 3)]


def test_build_athlete_stream_rejects_missing_power_and_weight():
    no_power = {"WorkoutChannels": {"Channels": ["Speed"], "Data": [
        {"Event": "", "MillisecondOffset": 0, "Values": [5.0]}]}}
    stream, reason = rd.build_athlete_stream(no_power, weight_kg=70.0)
    assert stream is None and "power" in reason.lower()

    stream, reason = rd.build_athlete_stream(_payload([0, 0, 0]), weight_kg=70.0)
    assert stream is None and "empty" in reason.lower()

    stream, reason = rd.build_athlete_stream(_payload([200, 200]), weight_kg=0)
    assert stream is None and "weight" in reason.lower()


# ── compute_scores (reference implementation the template JS mirrors) ─────────

def test_compute_scores_reference_values():
    # sample stdev of [4.0, 3.5, 3.0] is 0.5 → lower = 2.0, denom = 0.03
    scores = rd.compute_scores([4.0, 3.5, 3.0])
    assert scores[1] == pytest.approx(50.0)
    assert scores[0] == pytest.approx((4.0 - 2.0) / 0.03)
    assert scores[2] == pytest.approx((3.0 - 2.0) / 0.03)
    assert all(0.0 <= s <= 100.0 for s in scores)


def test_compute_scores_degenerate_cases():
    assert rd.compute_scores([3.5]) == [50.0]
    assert rd.compute_scores([3.5, 3.5, 3.5]) == [50.0, 50.0, 50.0]
    assert rd.compute_scores([]) == []
    assert rd.compute_scores([4.0, None, 3.0]) [1] is None


def test_compute_scores_mean_is_50():
    scores = rd.compute_scores([2.8, 3.1, 3.4, 4.0])
    values_mean_score = sum(scores) / len(scores)
    assert values_mean_score == pytest.approx(50.0)


# ── derive_weight_kg (in-memory sqlite) ───────────────────────────────────────

@pytest.fixture()
def mem_db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.base import Base

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng, autoflush=False, future=True)
    monkeypatch.setattr(rd, "get_session", lambda: SessionLocal())
    return SessionLocal


def test_derive_weight_kg_uses_most_recent_valid_detail(mem_db):
    from app.models.tables import Athlete, Workout, WorkoutDetail

    with mem_db() as s:
        s.add(Athlete(id=1, name="Alice"))
        # Older workout: 280 W at 4.0 W/kg → 70 kg
        s.add(Workout(id=10, athlete_id=1, date=date(2026, 1, 1), tp_workout_id="a"))
        s.add(WorkoutDetail(workout_id=10, power_average=280.0, watts_per_kg=4.0))
        # Newer workout: 272 W at 4.0 W/kg → 68 kg (should win)
        s.add(Workout(id=11, athlete_id=1, date=date(2026, 6, 1), tp_workout_id="b"))
        s.add(WorkoutDetail(workout_id=11, power_average=272.0, watts_per_kg=4.0))
        # Newest but invalid (no watts_per_kg) — ignored
        s.add(Workout(id=12, athlete_id=1, date=date(2026, 7, 1), tp_workout_id="c"))
        s.add(WorkoutDetail(workout_id=12, power_average=290.0, watts_per_kg=None))
        s.commit()

    assert rd.derive_weight_kg(1) == 68.0


def test_derive_weight_kg_none_when_absent(mem_db):
    from app.models.tables import Athlete

    with mem_db() as s:
        s.add(Athlete(id=2, name="Bob"))
        s.commit()

    assert rd.derive_weight_kg(2) is None
