from datetime import datetime
from types import SimpleNamespace

import pytest

from travel_planner import GeminiClient, GoogleMapsClient, Place, build_schedule, deduplicate, elevation_gain_matrix, estimate_wait_minutes, load_secret, optimize_route, optimize_route_near_target


def test_load_secret_supports_comments_and_assignment(tmp_path) -> None:
    secret_file = tmp_path / "key.txt"
    secret_file.write_text('# note\nGOOGLE_MAPS_API_KEY = "test-value"\n', encoding="utf-8")
    assert load_secret(secret_file, "GOOGLE_MAPS_API_KEY") == "test-value"


def test_gemini_uses_api_key_header(monkeypatch) -> None:
    class Response:
        ok = True

        @staticmethod
        def json():
            return {"candidates": [{"content": {"parts": [{"text": '{"area":"東京"}'}]}}]}

    def fake_post(url, **kwargs):
        assert "key=" not in url
        assert "params" not in kwargs
        assert kwargs["headers"]["x-goog-api-key"] == "secret"
        return Response()

    monkeypatch.setattr("travel_planner.requests.post", fake_post)
    assert GeminiClient("secret").parse_request("散歩", "09:00", "18:00")["area"] == "東京"


def test_gemini_reference_route_is_sanitized_and_completed(monkeypatch) -> None:
    class Response:
        ok = True

        @staticmethod
        def json():
            return {"candidates": [{"content": {"parts": [{"text": '{"route_indices":[2,2,99],"summary":"参考案"}'}]}}]}

    monkeypatch.setattr("travel_planner.requests.post", lambda *args, **kwargs: Response())
    places = [Place(str(index), "", float(index), float(index)) for index in range(4)]
    route, summary = GeminiClient("secret").propose_route(places, 3)
    assert route == [0, 2, 1, 3, 0]
    assert summary == "参考案"


def test_gemini_reference_route_uses_only_optimized_places(monkeypatch) -> None:
    class Response:
        ok = True

        @staticmethod
        def json():
            return {"candidates": [{"content": {"parts": [{"text": '{"route_indices":[1,4],"summary":"参考案"}'}]}}]}

    monkeypatch.setattr("travel_planner.requests.post", lambda *args, **kwargs: Response())
    places = [Place(str(index), "", float(index), float(index)) for index in range(5)]
    route, _ = GeminiClient("secret").propose_route(
        places, 2, allowed_indices=[2, 4]
    )
    assert route == [0, 4, 2, 0]


def test_gemini_reference_prompt_contains_only_public_place_attributes(monkeypatch) -> None:
    captured_prompt = ""

    class Response:
        ok = True

        @staticmethod
        def json():
            return {"candidates": [{"content": {"parts": [{"text": '{"route_indices":[1],"summary":"参考案"}'}]}}]}

    def fake_post(*args, **kwargs):
        nonlocal captured_prompt
        captured_prompt = kwargs["json"]["contents"][0]["parts"][0]["text"]
        return Response()

    monkeypatch.setattr("travel_planner.requests.post", fake_post)
    places = [
        Place("出発地", "SECRET_ADDRESS", 35.0, 139.0, review_count=999999, wait_minutes=99),
        Place("訪問地", "SECRET_ADDRESS", 36.0, 140.0, rating=4.5, review_count=888888, category="museum", wait_minutes=88),
    ]
    GeminiClient("secret").propose_route(places, 1, allowed_indices=[1])
    assert "訪問地" in captured_prompt
    assert '"category": "museum"' in captured_prompt
    assert '"rating": 4.5' in captured_prompt
    for forbidden in ("SECRET_ADDRESS", "latitude", "longitude", "review_count", "wait_minutes", "stay_minutes", "移動時間行列"):
        assert forbidden not in captured_prompt


def test_google_api_error_does_not_expose_credentials() -> None:
    class Response:
        ok = False
        status_code = 403

        @staticmethod
        def json():
            return {"error": {"status": "PERMISSION_DENIED"}}

    with pytest.raises(RuntimeError, match=r"Places API \(New\).+PERMISSION_DENIED") as error:
        GoogleMapsClient._check_response(Response(), "Places API (New)")
    assert "key=" not in str(error.value)


def test_google_api_error_accepts_list_payload() -> None:
    class Response:
        ok = False
        status_code = 429

        @staticmethod
        def json():
            return [{"error": "rate limited"}]

    with pytest.raises(RuntimeError, match="Routes API エラー \\(429: API_ERROR\\)"):
        GoogleMapsClient._check_response(Response(), "Routes API")


def test_transit_matrix_is_split_below_element_limit() -> None:
    client = GoogleMapsClient("secret")
    calls = []

    def fake_post(url, **kwargs):
        body = kwargs["json"]
        calls.append((len(body["origins"]), len(body["destinations"])))
        elements = [
            {"originIndex": i, "destinationIndex": j, "duration": "60s", "distanceMeters": 100}
            for i in range(len(body["origins"]))
            for j in range(len(body["destinations"]))
        ]
        return SimpleNamespace(ok=True, status_code=200, json=lambda: elements)

    client.session.post = fake_post
    places = [Place(str(i), "", float(i), float(i)) for i in range(16)]
    durations, _, congestion = client.route_matrix(places, datetime.now().astimezone(), "TRANSIT")
    assert calls == [(6, 16), (6, 16), (4, 16)]
    assert durations[15][14] == 1
    assert congestion[15][14] == 0


def test_route_not_found_remains_unavailable() -> None:
    client = GoogleMapsClient("secret")
    client.session.post = lambda *args, **kwargs: SimpleNamespace(
        ok=True,
        status_code=200,
        json=lambda: [{"originIndex": 0, "destinationIndex": 1, "condition": "ROUTE_NOT_FOUND"}],
    )
    places = [Place("0", "", 0, 0), Place("1", "", 1, 1)]
    durations, _, _ = client.route_matrix(places, datetime.now().astimezone(), "TRANSIT")
    assert durations[0][1] == 10**6


def test_wait_estimate_penalizes_busy_restaurants() -> None:
    assert estimate_wait_minutes("restaurant", 10_000) > estimate_wait_minutes("park", 10_000)


def test_elevation_gain_matrix_counts_only_ascending_height() -> None:
    assert elevation_gain_matrix([10.0, 25.0, 5.0]) == [[0.0, 15.0, 0.0], [0.0, 0.0, 0.0], [5.0, 20.0, 0.0]]


def test_deduplicate_prefers_better_rating() -> None:
    places = [
        Place("A", "x", 1, 1, "same", 3.0, 10),
        Place("A", "x", 1, 1, "same", 4.5, 100),
        Place("B", "y", 2, 2, "other", 4.0, 50),
    ]
    result = deduplicate(places, 10)
    assert len(result) == 2
    assert next(place for place in result if place.place_id == "same").rating == 4.5


def test_optimizer_and_schedule_return_to_depot() -> None:
    size = 12
    durations = [[0 if i == j else 5 + abs(i - j) for j in range(size)] for i in range(size)]
    route, _ = optimize_route(durations, [0] + [20] * (size - 1), 420, minimum_visits=10)
    assert route[0] == route[-1] == 0
    assert len(set(route[1:-1])) >= 10

    places = [Place(str(i), "", float(i), float(i), stay_minutes=0 if i == 0 else 20) for i in range(size)]
    schedule = build_schedule(route, places, durations, durations, datetime(2026, 1, 1, 9, 0).astimezone())
    assert schedule[-1]["arrival"] > schedule[0]["arrival"]


def test_optimizer_minimizes_travel_congestion_and_wait() -> None:
    durations = [
        [0, 5, 7, 5],
        [5, 0, 5, 5],
        [7, 5, 0, 7],
        [5, 5, 7, 0],
    ]
    congestion = [[0] * 4 for _ in range(4)]
    waits = [0, 0, 30, 0]
    route, _ = optimize_route(durations, [0, 10, 10, 10], 120, minimum_visits=2, congestion_delays=congestion, waits=waits)
    assert set(route[1:-1]) == {1, 3}


def test_optimizer_avoids_steep_climb_when_other_costs_match() -> None:
    durations = [[0, 5, 5, 5], [5, 0, 5, 5], [5, 5, 0, 5], [5, 5, 5, 0]]
    climbs = [[0.0] * 4 for _ in range(4)]
    climbs[0][2] = climbs[1][2] = climbs[3][2] = 500.0
    route, _ = optimize_route(durations, [0, 10, 10, 10], 120, minimum_visits=2, elevation_gains=climbs)
    assert 2 not in route


def test_optimizer_uses_largest_visit_count_that_fits_budget() -> None:
    durations = [[0 if i == j else 5 for j in range(4)] for i in range(4)]
    route, _ = optimize_route_near_target(durations, [0, 15, 15, 15], 45, preferred_visits=3, minimum_visits=2)
    assert len(route) - 2 == 2