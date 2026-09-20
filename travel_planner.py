from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Place:
    name: str
    address: str
    latitude: float
    longitude: float
    place_id: str = ""
    rating: float | None = None
    review_count: int = 0
    category: str = "spot"
    stay_minutes: int = 45
    wait_minutes: int = 0
    maps_uri: str = ""


def estimate_wait_minutes(category: str, review_count: int) -> int:
    base_waits = {
        "restaurant": 20,
        "cafe": 12,
        "bakery": 10,
        "museum": 10,
        "art_gallery": 8,
        "tourist_attraction": 8,
        "shopping_mall": 8,
        "park": 0,
        "place_of_worship": 3,
    }
    base = base_waits.get(category, 5)
    popularity = min(10, int(math.log10(review_count + 1) * 2)) if base else 0
    return base + popularity


def load_secret(path: Path, env_name: str) -> str:
    value = os.getenv(env_name, "").strip()
    if value:
        return value
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if "=" in candidate:
            name, candidate = candidate.split("=", 1)
            if name.strip() not in (env_name, "API_KEY", "GOOGLE_API_KEY"):
                continue
        return candidate.strip().strip('"').strip("'")
    return ""


def configure_gurobi() -> Path | None:
    license_path = os.getenv("GRB_LICENSE_FILE", "").strip()
    if license_path:
        return Path(license_path)
    if os.getenv("GRB_WLSACCESSID") and os.getenv("GRB_WLSSECRET") and os.getenv("GRB_LICENSEID"):
        return None
    local_license = ROOT / "gurobi.lic"
    if local_license.exists():
        os.environ["GRB_LICENSE_FILE"] = str(local_license)
        return local_license
    return None


class GeminiClient:
    def __init__(self, api_key: str, model: str = "gemini-flash-latest") -> None:
        self.api_key = api_key
        self.model = model

    def parse_request(self, request_text: str, start_time: str, end_time: str) -> dict[str, Any]:
        schema = {
            "type": "object",
            "properties": {
                "area": {"type": "string"},
                "themes": {"type": "array", "items": {"type": "string"}},
                "must_visit": {"type": "array", "items": {"type": "string"}},
                "avoid": {"type": "array", "items": {"type": "string"}},
                "transport": {"type": "string", "enum": ["DRIVE", "WALK", "BICYCLE", "TRANSIT"]},
                "candidate_queries": {"type": "array", "items": {"type": "string"}},
                "meal_queries": {"type": "array", "items": {"type": "string"}},
                "default_stay_minutes": {"type": "integer"},
            },
            "required": ["area", "themes", "must_visit", "avoid", "transport", "candidate_queries", "meal_queries", "default_stay_minutes"],
        }
        prompt = f"""あなたは日本旅行のプランナーです。次の希望をGoogle Places検索用の条件に変換してください。
希望: {request_text}
行動時間: {start_time}〜{end_time}
candidate_queries は地域名を含む具体的な日本語検索語を4〜7件、meal_queriesは食事候補を2〜3件にしてください。
移動手段が明示されていなければ、予測渋滞を最適化できるよう transport は DRIVE にしてください。電車、徒歩、自転車が明示された場合はその希望を優先してください。
10か所以上を回れるよう、観光地だけでなく短時間で寄れる公園、商店街、展望施設、カフェ等も含めてください。"""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        response = requests.post(
            url,
            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema},
            },
            timeout=45,
        )
        if not response.ok:
            status = response.json().get("error", {}).get("status", "API_ERROR")
            raise RuntimeError(f"Gemini API エラー ({response.status_code}: {status})")
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text)

    def propose_route(
        self,
        places: list[Place],
        visit_count: int = 10,
        allowed_indices: list[int] | None = None,
    ) -> tuple[list[int], str]:
        allowed = allowed_indices or list(range(1, len(places)))
        allowed = list(dict.fromkeys(index for index in allowed if 0 < index < len(places)))
        target = min(visit_count, len(allowed))
        candidates = [
            {
                "index": index,
                "name": place.name,
                "category": place.category,
                "rating": place.rating,
            }
            for index, place in enumerate(places)
            if index == 0 or index in allowed
        ]
        schema = {
            "type": "object",
            "properties": {
                "route_indices": {"type": "array", "items": {"type": "integer"}},
                "summary": {"type": "string"},
            },
            "required": ["route_indices", "summary"],
        }
        prompt = f"""提示された訪問地点の情報だけを使い、あなた自身の判断で参考旅行プランを作ってください。
出発・帰着地点は index 0 です。route_indices には index 0 を含めず、提示された訪問地点 {allowed} をすべて1回ずつ、訪問順に入れてください。
    地点の追加・削除はしないでください。
    訪問地点: {json.dumps(candidates, ensure_ascii=False)}"""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        response = requests.post(
            url,
            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema},
            },
            timeout=60,
        )
        if not response.ok:
            raise RuntimeError(f"Gemini参考プラン生成エラー ({response.status_code})")
        result = json.loads(response.json()["candidates"][0]["content"]["parts"][0]["text"])
        selected = []
        for value in result.get("route_indices", []):
            if isinstance(value, int) and value in allowed and value not in selected:
                selected.append(value)
            if len(selected) == target:
                break
        selected.extend(index for index in allowed if index not in selected and len(selected) < target)
        return [0, *selected, 0], str(result.get("summary", "Geminiが候補地から作成した参考プランです。"))


class GoogleMapsClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.headers = {"X-Goog-Api-Key": api_key, "Content-Type": "application/json"}
        self.session = requests.Session()
        self.session.mount(
            "https://",
            HTTPAdapter(max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("POST",))),
        )

    @staticmethod
    def _check_response(response: requests.Response, service: str) -> None:
        if response.ok:
            return
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        status = error.get("status", "API_ERROR") if isinstance(error, dict) else "API_ERROR"
        if response.status_code == 403:
            raise RuntimeError(f"{service} をGoogle Cloud Consoleで有効化し、このAPIキーに利用を許可してください ({status})")
        raise RuntimeError(f"{service} エラー ({response.status_code}: {status})")

    def search_places(self, query: str, limit: int = 5) -> list[Place]:
        fields = ",".join(
            ["places.id", "places.displayName", "places.formattedAddress", "places.location", "places.rating", "places.userRatingCount", "places.primaryType", "places.googleMapsUri"]
        )
        response = self.session.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={**self.headers, "X-Goog-FieldMask": fields},
            json={"textQuery": query, "languageCode": "ja", "regionCode": "JP", "pageSize": min(limit, 20)},
            timeout=30,
        )
        self._check_response(response, "Places API (New)")
        places = []
        for item in response.json().get("places", []):
            location = item.get("location", {})
            category = item.get("primaryType", "spot")
            review_count = int(item.get("userRatingCount", 0))
            places.append(
                Place(
                    name=item.get("displayName", {}).get("text", "名称不明"),
                    address=item.get("formattedAddress", ""),
                    latitude=float(location.get("latitude", 0)),
                    longitude=float(location.get("longitude", 0)),
                    place_id=item.get("id", ""),
                    rating=item.get("rating"),
                    review_count=review_count,
                    category=category,
                    wait_minutes=estimate_wait_minutes(category, review_count),
                    maps_uri=item.get("googleMapsUri", ""),
                )
            )
        return places

    def elevations(self, places: list[Place]) -> list[float]:
        response = self.session.get(
            "https://maps.googleapis.com/maps/api/elevation/json",
            params={
                "locations": "|".join(f"{place.latitude},{place.longitude}" for place in places),
                "key": self.api_key,
            },
            timeout=30,
        )
        self._check_response(response, "Elevation API")
        payload = response.json()
        if payload.get("status") != "OK":
            raise RuntimeError(f"Elevation API エラー ({payload.get('status', 'API_ERROR')})")
        results = payload.get("results", [])
        if len(results) != len(places):
            raise RuntimeError("Elevation APIから全地点の標高を取得できませんでした")
        return [float(item["elevation"]) for item in results]

    def route_matrix(self, places: list[Place], departure: datetime, mode: str) -> tuple[list[list[int]], list[list[int]], list[list[int]]]:
        waypoints = [{"waypoint": {"location": {"latLng": {"latitude": p.latitude, "longitude": p.longitude}}}} for p in places]
        size = len(places)
        durations = [[0 if i == j else 10**6 for j in range(size)] for i in range(size)]
        distances = [[0 for _ in range(size)] for _ in range(size)]
        congestion_delays = [[0 for _ in range(size)] for _ in range(size)]
        max_elements = 100 if mode == "TRANSIT" else 625
        origins_per_request = max(1, max_elements // size)

        for origin_offset in range(0, size, origins_per_request):
            body: dict[str, Any] = {
                "origins": waypoints[origin_offset : origin_offset + origins_per_request],
                "destinations": waypoints,
                "travelMode": mode,
                "languageCode": "ja",
                "units": "METRIC",
            }
            if mode == "DRIVE":
                body.update({"departureTime": departure.astimezone().isoformat(), "routingPreference": "TRAFFIC_AWARE"})
            elif mode == "TRANSIT":
                body["departureTime"] = departure.astimezone().isoformat()
            response = self.session.post(
                "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix",
                headers={**self.headers, "X-Goog-FieldMask": "originIndex,destinationIndex,duration,staticDuration,distanceMeters,condition"},
                json=body,
                timeout=60,
            )
            self._check_response(response, "Routes API")
            for item in response.json():
                if "error" in item or item.get("condition") == "ROUTE_NOT_FOUND" or "duration" not in item:
                    continue
                origin_index = origin_offset + item["originIndex"]
                destination_index = item["destinationIndex"]
                duration = int(math.ceil(float(item.get("duration", "0s")[:-1]) / 60))
                static_duration = int(math.ceil(float(item.get("staticDuration", item.get("duration", "0s"))[:-1]) / 60))
                durations[origin_index][destination_index] = duration
                congestion_delays[origin_index][destination_index] = max(0, duration - static_duration) if mode == "DRIVE" else 0
                distances[origin_index][destination_index] = int(item.get("distanceMeters", 0))

        if mode == "TRANSIT" and any(durations[i][j] >= 10**6 for i in range(size) for j in range(size) if i != j):
            walking_durations, walking_distances, _ = self.route_matrix(places, departure, "WALK")
            for i in range(size):
                for j in range(size):
                    if i != j and durations[i][j] >= 10**6:
                        durations[i][j] = walking_durations[i][j]
                        distances[i][j] = walking_distances[i][j]

        return durations, distances, congestion_delays


def deduplicate(places: list[Place], limit: int) -> list[Place]:
    unique: dict[str, Place] = {}
    for place in places:
        key = place.place_id or f"{place.name}:{place.latitude:.4f}:{place.longitude:.4f}"
        previous = unique.get(key)
        if previous is None or (place.rating or 0, place.review_count) > (previous.rating or 0, previous.review_count):
            unique[key] = place
    return sorted(unique.values(), key=lambda p: ((p.rating or 0) * math.log10(p.review_count + 10)), reverse=True)[:limit]


def elevation_gain_matrix(elevations: list[float]) -> list[list[float]]:
    return [[max(0.0, destination - origin) for destination in elevations] for origin in elevations]


def optimize_route(
    durations: list[list[int]],
    stays: list[int],
    budget_minutes: int,
    minimum_visits: int = 10,
    congestion_delays: list[list[int]] | None = None,
    waits: list[int] | None = None,
    elevation_gains: list[list[float]] | None = None,
) -> tuple[list[int], str]:
    """Solve a selective TSP that visits the required number of stops at minimum cost."""
    configure_gurobi()
    count = len(durations)
    congestion_delays = congestion_delays or [[0] * count for _ in range(count)]
    waits = waits or [0] * count
    elevation_gains = elevation_gains or [[0.0] * count for _ in range(count)]
    target_visits = min(minimum_visits, count - 1)

    def edge_cost(i: int, j: int) -> float:
        return durations[i][j] + congestion_delays[i][j] + (waits[j] if j else 0) + 0.1 * elevation_gains[i][j]

    try:
        import gurobipy as gp
        from gurobipy import GRB

        model = gp.Model("minimum_cost_day_trip_tsp")
        model.Params.OutputFlag = 0
        model.Params.TimeLimit = 3
        model.Params.MIPGap = 0.02
        x = model.addVars(count, count, vtype=GRB.BINARY, name="edge")
        visit = model.addVars(range(1, count), vtype=GRB.BINARY, name="visit")
        order = model.addVars(range(1, count), lb=1, ub=count - 1, name="order")
        for index in range(count):
            x[index, index].ub = 0
        model.addConstr(gp.quicksum(x[0, j] for j in range(1, count)) == 1)
        model.addConstr(gp.quicksum(x[i, 0] for i in range(1, count)) == 1)
        for index in range(1, count):
            model.addConstr(gp.quicksum(x[index, j] for j in range(count) if j != index) == visit[index])
            model.addConstr(gp.quicksum(x[i, index] for i in range(count) if i != index) == visit[index])
        for i in range(1, count):
            for j in range(1, count):
                if i != j:
                    model.addConstr(order[i] - order[j] + count * x[i, j] <= count - 1)
        total = gp.quicksum((durations[i][j] + (stays[j] if j else 0) + (waits[j] if j else 0)) * x[i, j] for i in range(count) for j in range(count) if i != j)
        model.addConstr(total <= budget_minutes)
        model.addConstr(gp.quicksum(visit.values()) == target_visits)
        model.setObjective(gp.quicksum(edge_cost(i, j) * x[i, j] for i in range(count) for j in range(count) if i != j), GRB.MINIMIZE)
        model.optimize()
        if model.SolCount:
            route = [0]
            while True:
                next_index = next((j for j in range(count) if x[route[-1], j].X > 0.5), 0)
                route.append(next_index)
                if next_index == 0:
                    break
            return route, "Gurobi"
    except Exception:
        pass

    route, remaining, elapsed = [0], set(range(1, count)), 0
    while remaining and len(route) - 1 < target_visits:
        current = route[-1]
        feasible = [j for j in remaining if elapsed + durations[current][j] + stays[j] + waits[j] + durations[j][0] <= budget_minutes]
        if not feasible:
            break
        next_index = min(feasible, key=lambda j: edge_cost(current, j))
        elapsed += durations[current][next_index] + stays[next_index] + waits[next_index]
        route.append(next_index)
        remaining.remove(next_index)
    if len(route) - 1 < target_visits:
        raise ValueError(f"指定時間内に{target_visits}か所を訪問できません。滞在時間を延ばすか候補範囲を狭めてください。")
    route.append(0)
    return route, "ヒューリスティック"


def optimize_route_near_target(
    durations: list[list[int]],
    stays: list[int],
    budget_minutes: int,
    preferred_visits: int = 15,
    minimum_visits: int = 10,
    congestion_delays: list[list[int]] | None = None,
    waits: list[int] | None = None,
    elevation_gains: list[list[float]] | None = None,
) -> tuple[list[int], str]:
    maximum = min(preferred_visits, len(durations) - 1)
    minimum = min(minimum_visits, maximum)
    for target in range(maximum, minimum - 1, -1):
        try:
            return optimize_route(
                durations,
                stays,
                budget_minutes,
                target,
                congestion_delays,
                waits,
                elevation_gains,
            )
        except ValueError:
            continue
    raise ValueError(f"指定時間内に{minimum}か所を訪問できません。滞在時間を延ばすか候補範囲を狭めてください。")


def build_schedule(
    route: list[int],
    places: list[Place],
    durations: list[list[int]],
    distances: list[list[int]],
    start: datetime,
    congestion_delays: list[list[int]] | None = None,
    elevation_gains: list[list[float]] | None = None,
) -> list[dict[str, Any]]:
    schedule = []
    current = start
    congestion_delays = congestion_delays or [[0] * len(places) for _ in places]
    elevation_gains = elevation_gains or [[0.0] * len(places) for _ in places]
    for position, place_index in enumerate(route):
        if position:
            previous = route[position - 1]
            current = current.replace(second=0, microsecond=0)
            current = current.fromtimestamp(current.timestamp() + durations[previous][place_index] * 60, current.tzinfo)
        arrival = current
        if position not in (0, len(route) - 1):
            current = current.fromtimestamp(current.timestamp() + (places[place_index].wait_minutes + places[place_index].stay_minutes) * 60, current.tzinfo)
        schedule.append({
            "place": places[place_index], "arrival": arrival, "departure": current,
            "travel_minutes": 0 if not position else durations[route[position - 1]][place_index],
            "congestion_minutes": 0 if not position else congestion_delays[route[position - 1]][place_index],
            "wait_minutes": 0 if position in (0, len(route) - 1) else places[place_index].wait_minutes,
            "elevation_gain_m": 0.0 if not position else elevation_gains[route[position - 1]][place_index],
            "distance_meters": 0 if not position else distances[route[position - 1]][place_index],
        })
    return schedule