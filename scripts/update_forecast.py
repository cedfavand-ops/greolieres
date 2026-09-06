#!/usr/bin/env python3
"""
Génère data/forecast.json pour Gréolières-les-Neiges à partir du modèle
ICON-CH1 (Open-Meteo / MétéoSuisse), avec correction nocturne du "trou à
froid" apprise automatiquement à partir des observations de la station
Datacake.

Variables d'environnement attendues (secrets GitHub Actions) :
  DATACAKE_TOKEN        token d'accès personnel Datacake (obligatoire pour l'apprentissage)
  DATACAKE_DEVICE_ID    UUID du device Datacake (pas l'ID de la page publique /pd/...)
  DATACAKE_TEMP_FIELD   nom du champ température (ex: TEMPERATURE), défaut "TEMPERATURE"

Sans DATACAKE_TOKEN, le script fonctionne quand même : il applique la
dernière correction connue (stockée dans data/bias_state.json) mais ne
peut pas l'affiner.
"""
import json
import os
import sys
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import urllib.request
import urllib.parse
import urllib.error

LAT = 43.8318
LON = 6.9617
TZ = ZoneInfo("Europe/Paris")
LOCATION_NAME = "Gréolières-les-Neiges"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
FORECAST_PATH = os.path.join(DATA_DIR, "forecast.json")
BIAS_PATH = os.path.join(DATA_DIR, "bias_state.json")

DATACAKE_TOKEN = os.environ.get("DATACAKE_TOKEN", "").strip()
DATACAKE_DEVICE_ID = os.environ.get("DATACAKE_DEVICE_ID", "").strip()
DATACAKE_TEMP_FIELD = os.environ.get("DATACAKE_TEMP_FIELD", "TEMPERATURE").strip()

CLEAR_CLOUD_THRESHOLD = 20      # % de nébulosité totale en-dessous duquel on considère "ciel dégagé"
CALM_WIND_THRESHOLD = 10        # km/h de vent moyen en-dessous duquel on considère "vent faible"
NIGHT_CORR_START_HOUR = 19      # 19h
DEFAULT_ALPHA = 0.25            # poids donné à la dernière nuit dans la moyenne mobile
MAX_HISTORY = 90                # nombre d'entrées de bias_history conservées


def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_openmeteo(past_hours=0, forecast_days=3):
    params = {
        "latitude": LAT,
        "longitude": LON,
        "hourly": ",".join([
            "temperature_2m", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
            "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
            "precipitation", "rain", "snowfall", "relative_humidity_2m",
        ]),
        "daily": "sunrise,sunset",
        "models": "meteoswiss_icon_ch1",
        "timezone": "Europe/Berlin",
        "wind_speed_unit": "kmh",
        "forecast_days": forecast_days,
        "past_hours": past_hours,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    return http_get_json(url)


def parse_iso_local(s):
    # Open-Meteo renvoie des heures locales "naïves" (timezone=Europe/Berlin/Paris demandé)
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=TZ)


def pick_picto(cloud_total, cloud_high, precipitation, rain, snowfall, humidity, wind_speed):
    if snowfall and snowfall > 0.05:
        return "neige"
    if rain and rain > 4:
        return "pluie-forte"
    if precipitation and precipitation > 0.1:
        return "pluie-faible"
    if humidity is not None and humidity > 95 and wind_speed < 5 and cloud_total > 80:
        return "brouillard"
    if cloud_total < 20 and cloud_high is not None and cloud_high > 40:
        return "voile"
    if cloud_total <= 20:
        return "clair"
    if cloud_total <= 50:
        return "peu-nuageux"
    if cloud_total <= 80:
        return "tres-nuageux"
    return "couvert"


def load_bias_state():
    if os.path.exists(BIAS_PATH):
        with open(BIAS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "offset_c": -2.0,   # estimation initiale à affiner : trou à froid ~ -2°C sous le modèle
        "alpha": DEFAULT_ALPHA,
        "last_processed_night": None,
        "last_night_error_c": None,
        "last_night_samples": 0,
        "history": [],
    }


def save_bias_state(state):
    with open(BIAS_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_datacake_series(start_dt, end_dt):
    """Retourne une liste de (datetime, temperature) depuis Datacake, ou [] si indisponible."""
    if not (DATACAKE_TOKEN and DATACAKE_DEVICE_ID):
        return []
    params = {
        "fields": DATACAKE_TEMP_FIELD,
        "resolution": "15m",
        "timeframe_start": start_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeframe_end": end_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    url = f"https://api.datacake.co/v1/devices/{DATACAKE_DEVICE_ID}/historic_data/?" + urllib.parse.urlencode(params)
    try:
        data = http_get_json(url, headers={"Authorization": f"Token {DATACAKE_TOKEN}"})
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
        print(f"[warn] Datacake indisponible: {e}", file=sys.stderr)
        return []
    out = []
    for row in data:
        try:
            t = datetime.fromisoformat(row["time"].replace("Z", "+00:00")).astimezone(TZ)
            v = row.get(DATACAKE_TEMP_FIELD)
            if v is not None:
                out.append((t, float(v)))
        except Exception:
            continue
    return out


def nearest_value(series, target_dt, max_gap_minutes=40):
    best = None
    best_gap = None
    for t, v in series:
        gap = abs((t - target_dt).total_seconds()) / 60
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best = v
    if best is not None and best_gap is not None and best_gap <= max_gap_minutes:
        return best
    return None


def compute_window(now):
    window_start = now.replace(hour=18, minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(hours=23)  # lendemain 17h
    if now > window_end:
        window_start += timedelta(days=1)
        window_end += timedelta(days=1)
    return window_start, window_end


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    now = datetime.now(TZ)
    bias = load_bias_state()

    om = fetch_openmeteo(past_hours=30, forecast_days=3)
    hourly = om["hourly"]
    times = [parse_iso_local(t) for t in hourly["time"]]

    def series(name):
        return hourly.get(name, [None] * len(times))

    temp = series("temperature_2m")
    wind_speed = series("wind_speed_10m")
    wind_gusts = series("wind_gusts_10m")
    wind_dir = series("wind_direction_10m")
    cloud_total = series("cloud_cover")
    cloud_low = series("cloud_cover_low")
    cloud_mid = series("cloud_cover_mid")
    cloud_high = series("cloud_cover_high")
    precipitation = series("precipitation")
    rain = series("rain")
    snowfall = series("snowfall")
    humidity = series("relative_humidity_2m")

    idx_by_time = {t: i for i, t in enumerate(times)}

    daily_times = [datetime.fromisoformat(d).date() for d in om["daily"]["time"]]
    sunrise_by_date = {}
    for i, d in enumerate(daily_times):
        sr = om["daily"]["sunrise"][i]
        sunrise_by_date[d] = parse_iso_local(sr)

    window_start, window_end = compute_window(now)
    next_day = window_start.date() + timedelta(days=1)
    sunrise_next = sunrise_by_date.get(next_day)
    corr_window_start = window_start.replace(hour=NIGHT_CORR_START_HOUR, minute=0)
    corr_window_end = (sunrise_next + timedelta(hours=1)) if sunrise_next else None

    offset_c = bias["offset_c"]

    hours_out = []
    t = window_start
    while t <= window_end:
        if t not in idx_by_time:
            t += timedelta(hours=1)
            continue
        i = idx_by_time[t]
        raw_t = temp[i]
        ws = wind_speed[i] or 0
        wg = wind_gusts[i] or 0
        wd = wind_dir[i] or 0
        ct = cloud_total[i] if cloud_total[i] is not None else 100
        cl = cloud_low[i]
        cm = cloud_mid[i]
        ch = cloud_high[i]
        pr = precipitation[i] or 0
        rn = rain[i] or 0
        sn = snowfall[i] or 0
        hu = humidity[i]

        in_corr_window = corr_window_end is not None and corr_window_start <= t <= corr_window_end
        qualifies = ct < CLEAR_CLOUD_THRESHOLD and ws < CALM_WIND_THRESHOLD
        apply_corr = in_corr_window and qualifies

        corrected_t = raw_t + offset_c if apply_corr else raw_t
        picto = pick_picto(ct, ch, pr, rn, sn, hu, ws)

        hours_out.append({
            "time": t.isoformat(),
            "temp_raw": round(raw_t, 1),
            "temp_corrected": round(corrected_t, 1),
            "corrected": apply_corr,
            "wind_speed": round(ws, 1),
            "wind_gusts": round(wg, 1),
            "wind_dir": round(wd),
            "cloud_cover": round(ct),
            "cloud_cover_low": round(cl) if cl is not None else None,
            "cloud_cover_mid": round(cm) if cm is not None else None,
            "cloud_cover_high": round(ch) if ch is not None else None,
            "picto": picto,
        })
        t += timedelta(hours=1)

    # --- Apprentissage : comparer la nuit précédente (obs Datacake vs modèle) ---
    prev_corr_end = corr_window_start  # la nuit qui vient de s'écouler se termine à 19h aujourd'hui... 
    # En pratique on cherche la fenêtre de correction la plus récente entièrement passée :
    # 19h (hier ou avant-hier) -> lever du soleil + 1h (ce matin ou hier matin)
    candidate_night_start = now.replace(hour=NIGHT_CORR_START_HOUR, minute=0, second=0, microsecond=0)
    if now.hour < NIGHT_CORR_START_HOUR:
        candidate_night_start -= timedelta(days=1)
    candidate_morning_date = (candidate_night_start + timedelta(days=1)).date()
    candidate_sunrise = sunrise_by_date.get(candidate_morning_date)
    night_ready = False
    if candidate_sunrise:
        candidate_night_end = candidate_sunrise + timedelta(hours=1)
        night_ready = now >= candidate_night_end
    night_key = candidate_night_start.date().isoformat()

    if night_ready and bias.get("last_processed_night") != night_key and DATACAKE_TOKEN and DATACAKE_DEVICE_ID:
        obs_series = fetch_datacake_series(candidate_night_start - timedelta(minutes=30),
                                            candidate_night_end + timedelta(minutes=30))
        errors = []
        t = candidate_night_start
        while t <= candidate_night_end:
            if t in idx_by_time:
                i = idx_by_time[t]
                ct = cloud_total[i] if cloud_total[i] is not None else 100
                ws = wind_speed[i] or 0
                if ct < CLEAR_CLOUD_THRESHOLD and ws < CALM_WIND_THRESHOLD:
                    obs_v = nearest_value(obs_series, t)
                    if obs_v is not None and temp[i] is not None:
                        errors.append(obs_v - temp[i])
            t += timedelta(hours=1)

        if len(errors) >= 2:
            mean_error = sum(errors) / len(errors)
            alpha = bias.get("alpha", DEFAULT_ALPHA)
            new_offset = (1 - alpha) * bias["offset_c"] + alpha * mean_error
            bias["history"].append({
                "night": night_key,
                "mean_error_c": round(mean_error, 2),
                "samples": len(errors),
                "offset_before": round(bias["offset_c"], 2),
                "offset_after": round(new_offset, 2),
            })
            bias["history"] = bias["history"][-MAX_HISTORY:]
            bias["offset_c"] = new_offset
            bias["last_night_error_c"] = round(mean_error, 2)
            bias["last_night_samples"] = len(errors)
            bias["last_processed_night"] = night_key
        else:
            bias["last_processed_night"] = night_key  # rien à apprendre cette nuit (pas assez d'heures claires/calmes)
            bias["last_night_error_c"] = None
            bias["last_night_samples"] = len(errors)

    save_bias_state(bias)

    output = {
        "generated_at": now.isoformat(),
        "run_time": times[0].isoformat() if times else None,
        "location": {"name": LOCATION_NAME, "lat": LAT, "lon": LON},
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "correction": {
            "offset_c": round(bias["offset_c"], 2),
            "alpha": bias.get("alpha", DEFAULT_ALPHA),
            "last_night_error_c": bias.get("last_night_error_c"),
            "last_night_samples": bias.get("last_night_samples", 0),
            "clear_cloud_threshold_pct": CLEAR_CLOUD_THRESHOLD,
            "calm_wind_threshold_kmh": CALM_WIND_THRESHOLD,
        },
        "hours": hours_out,
    }

    with open(FORECAST_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"OK — {len(hours_out)} heures écrites, offset actuel = {bias['offset_c']:.2f}°C")


if __name__ == "__main__":
    main()
