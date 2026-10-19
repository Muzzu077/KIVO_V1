import json
import urllib.request
from urllib.parse import quote_plus


def _fetch_live_weather(city: str = "") -> dict | None:
    try:
        url = f"https://wttr.in/{quote_plus(city)}?format=j1" if city else "https://wttr.in/?format=j1"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "curl/7.68.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except Exception as e:
        print(f"[Weather] wttr.in query failed: {e}")
        return None


def weather_action(
    parameters: dict = None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    city = (params.get("city") or "").strip()
    when = (params.get("time") or "today").strip()

    # If city is "here", "current", "local", treat as auto-detect
    if city.lower() in ("here", "current", "my location", "local"):
        city = ""

    data = _fetch_live_weather(city)
    if data and "current_condition" in data and data["current_condition"]:
        curr = data["current_condition"][0]
        temp_c = curr.get("temp_C", "N/A")
        feels_c = curr.get("FeelsLikeC", temp_c)
        desc = curr.get("weatherDesc", [{}])[0].get("value", "Clear")
        humidity = curr.get("humidity", "N/A")
        wind_kmh = curr.get("windspeedKmph", "N/A")

        area = data.get("nearest_area", [{}])[0]
        area_name = area.get("areaName", [{}])[0].get("value", city or "your area")
        region = area.get("region", [{}])[0].get("value", "")
        loc_str = f"{area_name}, {region}" if region and region != area_name else area_name

        msg = f"The temperature in {loc_str} is currently {temp_c}°C (feels like {feels_c}°C) with {desc.lower()} conditions. Humidity is {humidity}%, wind speed {wind_kmh} km/h."
        _log(msg, player)

        if session_memory:
            try:
                session_memory.set_last_search(query=f"weather in {loc_str}", response=msg)
            except Exception:
                pass
        return msg

    # Fallback if wttr.in is unavailable
    if city:
        msg = f"Unable to fetch real-time weather sensors for {city}. Please check network connection."
    else:
        msg = "Unable to determine current location weather. Please specify a city name, sir."
    _log(msg, player)
    return msg


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "weather_report",
    "description": "Gives the exact real-time temperature, weather forecast, humidity, and atmospheric conditions for any city or the user's current location. Use whenever the user asks about the weather, outdoor temperature, rain, or climate.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "city": {
                "type": "STRING",
                "description": "City name (optional, defaults to current location if not specified)"
            },
            "time": {
                "type": "STRING",
                "description": "Time period (e.g. 'today', 'tomorrow')"
            }
        },
        "required": []
    },
    "handler": weather_action,
}
