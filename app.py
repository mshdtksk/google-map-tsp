from __future__ import annotations

import html
import json
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from travel_planner import GeminiClient, GoogleMapsClient, Place, build_schedule, deduplicate, elevation_gain_matrix, load_secret, optimize_route_near_target


ROOT = Path(__file__).resolve().parent
st.set_page_config(page_title="Michi | AI Day Trip Planner", page_icon="🗺️", layout="wide")
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;600;700&family=Noto+Sans+JP:wght@400;600;700&display=swap');
:root { --ink:#17211b; --paper:#f4f1e8; --green:#176b4d; --coral:#e96b4c; --line:#d7d2c5; }
.stApp { background:radial-gradient(circle at 85% 5%,#d9e9d8 0,transparent 28%),linear-gradient(120deg,#f7f4ec,#edf2e9); color:var(--ink); font-family:'Noto Sans JP',sans-serif; }
h1,h2,h3 { font-family:'DM Sans','Noto Sans JP',sans-serif; letter-spacing:0!important; }
.block-container { max-width:1380px; padding-top:2rem; }
.brand { font:700 3rem/1 'DM Sans'; color:var(--ink); margin-bottom:.4rem; }
.brand b { color:var(--coral); }
.lede { color:#526158; margin-bottom:1.6rem; }
[data-testid="stForm"] { border:1px solid var(--line); border-radius:8px; background:#fffdf8cc; padding:1.2rem; }
.stop { display:grid; grid-template-columns:60px 1fr auto; gap:14px; align-items:center; padding:14px 0; border-bottom:1px solid var(--line); }
.clock { font:700 1rem 'DM Sans'; color:var(--green); }
.place-name { font-weight:700; } .meta { color:#66736b; font-size:.82rem; margin-top:3px; }
.place-name a { color:var(--ink); text-decoration-color:var(--coral); text-underline-offset:3px; }
.place-name a:hover { color:var(--green); }
.badge { background:#e5efe7; color:#176b4d; padding:4px 8px; border-radius:4px; font-size:.76rem; }
div[data-testid="stMetric"] * { color:#111!important; }
div.stButton>button[kind="primary"] { background:var(--green); border-color:var(--green); }
</style>
""", unsafe_allow_html=True)


def linked_place_name(place: Place, index: int) -> str:
    label = f"{index}. {html.escape(place.name)}"
    if place.maps_uri.startswith("https://"):
        return f'<a href="{html.escape(place.maps_uri, quote=True)}" target="_blank" rel="noopener noreferrer">{label}</a>'
    return label


def render_map(schedule: list[dict], maps_key: str) -> None:
    points = [{"name": item["place"].name, "lat": item["place"].latitude, "lng": item["place"].longitude, "time": item["arrival"].strftime("%H:%M")} for item in schedule]
    payload = json.dumps(points, ensure_ascii=False).replace("</", "<\\/")
    components.html(f"""
    <div id="map" style="height:620px;border-radius:8px;overflow:hidden"></div>
    <script>
      const points={payload};
      function initMap() {{
        const map=new google.maps.Map(document.getElementById('map'),{{center:points[0],zoom:12,mapId:'DEMO_MAP_ID',disableDefaultUI:true,zoomControl:true}});
        const bounds=new google.maps.LatLngBounds();
        points.forEach((p,i)=>{{
          const marker=new google.maps.Marker({{position:p,map,label:String(i+1),title:p.name}});
          const info=new google.maps.InfoWindow({{content:`<strong>${{p.time}} ${{p.name}}</strong>`}});
          marker.addListener('click',()=>info.open({{anchor:marker,map}})); bounds.extend(p);
        }});
        new google.maps.Polyline({{path:points,geodesic:true,strokeColor:'#e96b4c',strokeOpacity:.9,strokeWeight:4,map}});
        map.fitBounds(bounds,48);
      }}
    </script>
    <script async src="https://maps.googleapis.com/maps/api/js?key={html.escape(maps_key)}&callback=initMap&v=weekly"></script>
    """, height=630)


st.markdown('<div class="brand">michi<b>.</b></div><div class="lede">ことばから、渋滞まで読んだ一日の旅程へ。</div>', unsafe_allow_html=True)

gemini_key = load_secret(ROOT / ".env", "GEMINI_API_KEY")
maps_key = load_secret(ROOT / ".env", "GOOGLE_MAPS_API_KEY")

with st.form("planner"):
    request_text = st.text_area("どんな一日にしたいですか？", "東京で、建築と現代アート、下町の甘味を楽しみたい。混雑を避けつつ、写真スポットを多めに10か所以上回りたい。", height=110)
    col1, col2, col3, col4 = st.columns(4)
    trip_date = col1.date_input("日付", datetime.now().date() + timedelta(days=1))
    start_time = col2.time_input("出発", time(9, 0))
    end_time = col3.time_input("帰着", time(20, 0))
    candidate_limit = col4.slider("候補数", 10, 20, 15)
    start_location = st.text_input("出発・帰着地点", "東京駅")
    submitted = st.form_submit_button("AIで旅程をつくる", type="primary", use_container_width=True)

if submitted:
    if not gemini_key or not maps_key:
        st.error("Gemini または Google Maps API キーが見つかりません。配置先を確認してください。")
        st.stop()
    try:
        with st.status("希望を読み解いています…", expanded=True) as status:
            gemini = GeminiClient(gemini_key)
            intent = gemini.parse_request(request_text, str(start_time), str(end_time))
            st.write(f"エリア: {intent['area']} / テーマ: {', '.join(intent['themes'])}")
            maps = GoogleMapsClient(maps_key)
            depot_results = maps.search_places(f"{start_location} {intent['area']}", 1)
            if not depot_results:
                raise ValueError("出発地点を特定できませんでした。")
            depot = replace(depot_results[0], stay_minutes=0, category="depot")
            candidates: list[Place] = []
            queries = intent["candidate_queries"] + intent["meal_queries"] + intent["must_visit"]
            for query in queries:
                candidates.extend(maps.search_places(query if intent["area"] in query else f"{intent['area']} {query}", 4))
            candidates = deduplicate(candidates, candidate_limit)
            if len(candidates) < 10:
                st.warning(f"重複を除くと候補は {len(candidates)} 件でした。条件を広げると10件以上にしやすくなります。")
            stay = max(15, min(25, int(intent["default_stay_minutes"])))
            places = [depot] + [replace(place, stay_minutes=stay if "restaurant" not in place.category else 35) for place in candidates]
            departure = datetime.combine(trip_date, start_time).astimezone()
            durations, distances, congestion_delays = maps.route_matrix(places, departure, intent["transport"])
            elevations = maps.elevations(places)
            elevation_gains = elevation_gain_matrix(elevations)
            route, solver = optimize_route_near_target(
                durations,
                [p.stay_minutes for p in places],
                int((datetime.combine(trip_date, end_time) - datetime.combine(trip_date, start_time)).total_seconds() / 60),
                min(15, len(candidates)),
                10,
                congestion_delays,
                [p.wait_minutes for p in places],
                elevation_gains,
            )
            schedule = build_schedule(route, places, durations, distances, departure, congestion_delays, elevation_gains)
            gemini_schedule = None
            gemini_summary = ""
            try:
                optimized_indices = route[1:-1]
                gemini_route, gemini_summary = gemini.propose_route(
                    places,
                    len(optimized_indices),
                    optimized_indices,
                )
                gemini_schedule = build_schedule(gemini_route, places, durations, distances, departure, congestion_delays, elevation_gains)
            except Exception:
                gemini_summary = "Geminiの参考プランは今回生成できませんでした。"
            status.update(label=f"{len(route)-2}か所の旅程ができました", state="complete", expanded=False)
        st.session_state.plan = {
            "schedule": schedule,
            "solver": solver,
            "intent": intent,
            "gemini_schedule": gemini_schedule,
            "gemini_summary": gemini_summary,
        }
    except Exception as exc:
        st.error(f"旅程を作成できませんでした: {str(exc).split('?key=', 1)[0]}")

if "plan" in st.session_state:
    plan = st.session_state.plan
    schedule = plan["schedule"]
    left, right = st.columns([0.92, 1.35], gap="large")
    with left:
        st.subheader("Today's route")
        total_travel = sum(item["travel_minutes"] for item in schedule)
        total_congestion = sum(item["congestion_minutes"] for item in schedule)
        total_wait = sum(item["wait_minutes"] for item in schedule)
        total_elevation = round(sum(item["elevation_gain_m"] for item in schedule))
        metric1, metric2, metric3, metric4 = st.columns(4)
        metric1.metric("移動時間", f"{total_travel}分")
        metric2.metric("渋滞遅延", f"{total_congestion}分")
        metric3.metric("推定待ち", f"{total_wait}分")
        metric4.metric("上り高低差", f"{total_elevation}m")
        st.caption(f"{len(schedule)-2} stops · 最小コストTSP · {plan['solver']} · {plan['intent']['transport']}")
        with st.expander("最適化モデル"):
            st.latex(r"\min \sum_{i \ne j}(t_{ij}+\lambda t^{traffic}_{ij}+w_j+0.1h^+_{ij})x_{ij},\quad \lambda=1")
            st.caption("最大15地点訪問、各地点への入出1回、部分巡回路の禁止、帰着時刻を制約にしています。上り100mを10分相当として評価します。")
        for index, item in enumerate(schedule):
            place = item["place"]
            travel = "出発" if index == 0 else f"移動 {item['travel_minutes']}分 · 渋滞 {item['congestion_minutes']}分 · 待ち {item['wait_minutes']}分"
            rating = f"★ {place.rating:.1f} ({place.review_count:,})" if place.rating else ""
            place_name = linked_place_name(place, index + 1)
            st.markdown(f'<div class="stop"><div class="clock">{item["arrival"]:%H:%M}</div><div><div class="place-name">{place_name}</div><div class="meta">{html.escape(place.address)}<br>{rating}</div></div><span class="badge">{travel}</span></div>', unsafe_allow_html=True)
    with right:
        render_map(schedule, maps_key)

    st.divider()
    st.subheader("Geminiが作る参考プラン")
    st.caption("同じ訪問地点の名称・カテゴリ・評価だけをGeminiに渡し、移動時間・渋滞・待ち時間・高低差・最適化順序を伏せた比較用プランです。")
    st.write(plan["gemini_summary"])
    gemini_schedule = plan.get("gemini_schedule")
    if gemini_schedule:
        gemini_left, gemini_right = st.columns([0.92, 1.35], gap="large")
        with gemini_left:
            gemini_travel = sum(item["travel_minutes"] for item in gemini_schedule)
            gemini_congestion = sum(item["congestion_minutes"] for item in gemini_schedule)
            gemini_wait = sum(item["wait_minutes"] for item in gemini_schedule)
            gemini_elevation = round(sum(item["elevation_gain_m"] for item in gemini_schedule))
            gemini_metric1, gemini_metric2, gemini_metric3, gemini_metric4 = st.columns(4)
            gemini_metric1.metric("移動時間", f"{gemini_travel}分")
            gemini_metric2.metric("渋滞遅延", f"{gemini_congestion}分")
            gemini_metric3.metric("推定待ち", f"{gemini_wait}分")
            gemini_metric4.metric("上り高低差", f"{gemini_elevation}m")
            for index, item in enumerate(gemini_schedule):
                place = item["place"]
                travel = "出発" if index == 0 else f"移動 {item['travel_minutes']}分 · 渋滞 {item['congestion_minutes']}分 · 待ち {item['wait_minutes']}分"
                place_name = linked_place_name(place, index + 1)
                st.markdown(
                    f'<div class="stop"><div class="clock">{item["arrival"]:%H:%M}</div>'
                    f'<div><div class="place-name">{place_name}</div>'
                    f'<div class="meta">{html.escape(place.address)}</div></div><span class="badge">{travel}</span></div>',
                    unsafe_allow_html=True,
                )
        with gemini_right:
            render_map(gemini_schedule, maps_key)
