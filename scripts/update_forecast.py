#!/usr/bin/env python3
"""
Génère data/forecast.json pour Gréolières-les-Neiges à partir du modèle
ICON-CH1 (Open-Meteo / MétéoSuisse), avec correction nocturne du "trou à
froid" apprise automatiquement à partir des observations de la station
Datacake.

La correction n'est PAS un simple décalage fixe appliqué toute la nuit :
le refroidissement radiatif d'un trou à froid est rapide juste après le
coucher du soleil puis ralentit progressivement jusqu'au lever du jour.
Le script apprend donc un décalage indépendant pour chaque heure comptée
depuis le début de la fenêtre de correction (~15 min avant le coucher du
soleil), via une moyenne mobile par "case horaire" (data/bias_state.json
-> "offset_profile"), plutôt qu'un seul chiffre moyen pour toute la nuit.

L'éligibilité (une heure compte-t-elle comme "ciel dégagé + calme" pour
appliquer/apprendre la correction ?) se base sur une nébulosité effective
qui privilégie les nuages bas et moyens (ceux qui bloquent vraiment le
rayonnement infrarouge nocturne) et n'accorde qu'un poids atténué aux
nuages hauts (cirrus), qui freinent un peu le refroidissement mais bien
moins que des nuages bas.

Variables d'environnement attendues (secrets GitHub Actions) :
  DATACAKE_TOKEN        token d'accès personnel Datacake (obligatoire pour l'apprentissage)
  DATACAKE_DEVICE_ID    UUID du device Datacake (pas l'ID de la page publique /pd/...)
  DATACAKE_TEMP_FIELD   nom du champ température (ex: TEMPERATURE), défaut "TEMPERATURE"

Sans DATACAKE_TOKEN, le script fonctionne quand même : il applique le
dernier profil de correction connu (stocké dans data/bias_state.json)
mais ne peut pas l'affiner.
"""
import json
import math
import os
import sys
from datetime import datetime, timedelta
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

CLEAR_CLOUD_THRESHOLD = 20      # nébulosité EFFECTIVE (%) en-dessous de laquelle le ciel est considéré pleinement dégagé
CLOUD_ZERO_THRESHOLD = 45       # nébulosité EFFECTIVE (%) au-delà de laquelle la correction est nulle (rampe entre les deux)
CALM_WIND_THRESHOLD = 10        # vent moyen (km/h) en-dessous duquel il est considéré pleinement calme
WIND_ZERO_THRESHOLD = 22        # vent moyen (km/h) au-delà duquel la correction est nulle (rampe entre les deux)
LEARN_CLARITY_MIN = 0.6         # clarté minimale d'une heure pour qu'elle compte dans l'apprentissage nocturne
HIGH_CLOUD_ATTENUATION = 0.3    # poids résiduel des nuages hauts dans la nébulosité effective (0=ignorés, 1=comme bas/moyen)
SUNSET_LEAD_MINUTES = 15        # la fenêtre de correction démarre ~15 min avant le coucher du soleil
DEFAULT_ALPHA = 0.25            # poids donné à la dernière nuit dans la moyenne mobile (par case horaire)
MAX_HISTORY = 90                # nombre de nuits conservées dans l'historique
MAX_BUCKET_HOURS = 16           # nombre max de cases horaires suivies après le début de fenêtre (nuits longues d'hiver)


def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_openmeteo(past_days=2, forecast_days=3):
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
        # past_days étend aussi le tableau "daily" (lever/coucher du soleil) en
        # arrière, contrairement à past_hours qui ne joue que sur "hourly" -
        # indispensable pour retrouver le coucher de soleil d'hier soir et
        # calculer rétrospectivement la fenêtre de correction de la nuit passée.
        "past_days": past_days,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    return http_get_json(url)


def parse_iso_local(s):
    # Open-Meteo renvoie des heures locales "naïves" (timezone=Europe/Berlin/Paris demandé)
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=TZ)


def effective_cloud(cloud_low, cloud_mid, cloud_high):
    """Nébulosité 'effective' pour juger si le ciel est assez dégagé pour un
    fort refroidissement radiatif : les nuages bas/moyens comptent plein pot,
    les nuages hauts (cirrus) ne comptent que partiellement (ils freinent un
    peu le refroidissement mais bien moins qu'un vrai plafond bas)."""
    low_mid = max(cloud_low or 0, cloud_mid or 0)
    return min(100, low_mid + HIGH_CLOUD_ATTENUATION * (cloud_high or 0))


def clarity_factor(eff_cloud, wind_speed):
    """Facteur continu (0 à 1) remplaçant un seuil tout-ou-rien : la
    correction s'estompe progressivement quand la nébulosité effective ou
    le vent augmentent, au lieu de basculer brutalement on/off d'une heure
    à l'autre pour un petit écart autour du seuil (effet "yo-yo")."""
    def ramp(value, full_at, zero_at):
        if value <= full_at:
            return 1.0
        if value >= zero_at:
            return 0.0
        return 1.0 - (value - full_at) / (zero_at - full_at)

    cloud_c = ramp(eff_cloud, CLEAR_CLOUD_THRESHOLD, CLOUD_ZERO_THRESHOLD)
    wind_c = ramp(wind_speed, CALM_WIND_THRESHOLD, WIND_ZERO_THRESHOLD)
    return cloud_c * wind_c


def pick_picto(cloud_total, cloud_low, cloud_mid, cloud_high, precipitation, rain, snowfall, humidity, wind_speed):
    if snowfall and snowfall > 0.05:
        return "neige"
    if rain and rain > 4:
        return "pluie-forte"
    if precipitation and precipitation > 0.1:
        return "pluie-faible"
    if humidity is not None and humidity > 95 and wind_speed < 5 and cloud_total > 80:
        return "brouillard"
    # Nuages bas/moyens quasi absents mais nuages hauts significatifs
    # (cirrus) -> ciel voilé, quel que soit le "total" (qui peut être élevé à
    # cause des seuls nuages hauts, ou d'un calcul de recouvrement des étages).
    # Le bas compte plein pot (un stratus bas obscurcit vraiment le ciel), le
    # moyen ne compte qu'à moitié (un peu d'altocumulus n'empêche pas un ciel
    # de rester perçu comme "voilé" plutôt que "couvert").
    voile_gate = (cloud_low or 0) + 0.5 * (cloud_mid or 0)
    if voile_gate < 25 and cloud_high is not None and cloud_high >= 40:
        return "voile"
    if cloud_total <= 20:
        return "clair"
    if cloud_total <= 50:
        return "peu-nuageux"
    if cloud_total <= 80:
        return "tres-nuageux"
    return "couvert"


def default_offset(bucket):
    """Estimation de départ (avant tout apprentissage) pour la case horaire
    `bucket` (0 = première heure de la fenêtre de correction, proche du
    coucher du soleil). Forme volontairement non-linéaire : chute assez
    rapide les 2-3 premières heures puis ralentissement, sans plafond dur
    (l'apprentissage réel prendra le relais et peut aller bien plus loin
    sur un trou à froid marqué)."""
    return -(2.0 + 8.0 * (1 - math.exp(-bucket / 2.5)))


def load_bias_state():
    if os.path.exists(BIAS_PATH):
        with open(BIAS_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("offset_profile", {})
        state.setdefault("alpha", DEFAULT_ALPHA)
        state.setdefault("last_processed_night", None)
        state.setdefault("last_night_error_c", None)
        state.setdefault("last_night_samples", 0)
        state.setdefault("history", [])
        return state
    return {
        "offset_profile": {},   # {"0": -2.1, "1": -4.3, ...} appris case horaire par case horaire
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
    """Retourne une liste de (datetime, temperature) depuis Datacake en cas de
    succès (liste vide si succès mais aucune donnée), ou None si la requête
    elle-même a échoué (erreur réseau/HTTP) - ce cas ne doit JAMAIS être
    traité comme "nuit sans données exploitables", pour permettre un nouvel
    essai au prochain passage plutôt que d'abandonner définitivement."""
    if not (DATACAKE_TOKEN and DATACAKE_DEVICE_ID):
        return None
    params = {
        "fields": DATACAKE_TEMP_FIELD,
        "resolution": "raw",
        "timeframe_start": start_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timeframe_end": end_dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    url = f"https://api.datacake.co/v1/devices/{DATACAKE_DEVICE_ID}/historic_data/?" + urllib.parse.urlencode(params)
    try:
        data = http_get_json(url, headers={"Authorization": f"Token {DATACAKE_TOKEN}"})
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = "(impossible de lire le corps de la réponse)"
        print(
            f"[warn] Datacake indisponible: HTTP {e.code} {e.reason} — "
            f"device_id_len={len(DATACAKE_DEVICE_ID)} field='{DATACAKE_TEMP_FIELD}' "
            f"resolution='raw' url={url} — réponse: {body}",
            file=sys.stderr,
        )
        return None
    except (urllib.error.URLError, ValueError) as e:
        print(f"[warn] Datacake indisponible: {e}", file=sys.stderr)
        return None
    if not data:
        print(
            "[warn] Datacake a répondu mais sans aucune donnée sur cette période "
            "(vérifier DATACAKE_DEVICE_ID, ou absence de mesures récentes).",
            file=sys.stderr,
        )
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
    if not out:
        available = sorted(k for k in data[0].keys() if k != "time")
        print(
            f"[warn] Aucune valeur trouvée pour le champ DATACAKE_TEMP_FIELD="
            f"'{DATACAKE_TEMP_FIELD}'. Champs disponibles sur ce device : {available}",
            file=sys.stderr,
        )
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
    """Fenêtre d'AFFICHAGE, fixe : 18h du jour même -> 17h le lendemain."""
    window_start = now.replace(hour=18, minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(hours=23)  # lendemain 17h
    if now > window_end:
        window_start += timedelta(days=1)
        window_end += timedelta(days=1)
    return window_start, window_end


def corr_window_for_evening(evening_date, sunset_by_date, sunrise_by_date):
    """Fenêtre de CORRECTION pour la nuit qui commence le soir de `evening_date` :
    ~15 min avant le coucher du soleil (arrondi à l'heure pleine) -> lever du
    soleil du lendemain + 1h. Retourne (None, None) si les données manquent."""
    sunset_dt = sunset_by_date.get(evening_date)
    if sunset_dt is None:
        return None, None
    start = (sunset_dt - timedelta(minutes=SUNSET_LEAD_MINUTES)).replace(minute=0, second=0, microsecond=0)
    next_day = evening_date + timedelta(days=1)
    sunrise_dt = sunrise_by_date.get(next_day)
    end = (sunrise_dt + timedelta(hours=1)) if sunrise_dt else None
    return start, end


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not DATACAKE_TOKEN or not DATACAKE_DEVICE_ID:
        print(
            "[warn] DATACAKE_TOKEN et/ou DATACAKE_DEVICE_ID absents ou vides : "
            "la correction s'appliquera avec le profil déjà appris (ou le profil "
            "par défaut), mais aucun apprentissage n'aura lieu ce passage-ci.",
            file=sys.stderr,
        )
    now = datetime.now(TZ)
    bias = load_bias_state()
    profile = bias["offset_profile"]
    alpha = bias.get("alpha", DEFAULT_ALPHA)

    om = fetch_openmeteo(past_days=2, forecast_days=3)
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

    daily_dates = [datetime.fromisoformat(d).date() for d in om["daily"]["time"]]
    sunrise_by_date, sunset_by_date = {}, {}
    for i, d in enumerate(daily_dates):
        sunrise_by_date[d] = parse_iso_local(om["daily"]["sunrise"][i])
        sunset_by_date[d] = parse_iso_local(om["daily"]["sunset"][i])

    # --- Fenêtre d'affichage (18h -> 17h lendemain) ---
    window_start, window_end = compute_window(now)
    corr_start, corr_end = corr_window_for_evening(window_start.date(), sunset_by_date, sunrise_by_date)

    def offset_for_bucket(bucket):
        key = str(bucket)
        if key in profile:
            return profile[key]
        return default_offset(bucket)

    current_offset_c = None
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

        eff_cloud = effective_cloud(cl, cm, ch)
        in_corr_window = corr_end is not None and corr_start <= t <= corr_end
        clarity = clarity_factor(eff_cloud, ws) if in_corr_window else 0.0
        apply_corr = in_corr_window and clarity > 0

        applied_offset = None
        bucket = None
        if apply_corr:
            bucket = min(int((t - corr_start).total_seconds() // 3600), MAX_BUCKET_HOURS)
            applied_offset = offset_for_bucket(bucket) * clarity
            corrected_t = raw_t + applied_offset
            if t.replace(minute=0, second=0, microsecond=0) == now.replace(minute=0, second=0, microsecond=0):
                current_offset_c = applied_offset
        else:
            corrected_t = raw_t

        picto = pick_picto(ct, cl, cm, ch, pr, rn, sn, hu, ws)

        hours_out.append({
            "time": t.isoformat(),
            "temp_raw": round(raw_t, 1),
            "temp_corrected": round(corrected_t, 1),
            "corrected": apply_corr,
            "correction_offset_c": round(applied_offset, 2) if applied_offset is not None else None,
            "clarity": round(clarity, 2) if in_corr_window else None,
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

    # --- Apprentissage : la nuit la plus récente entièrement écoulée (celle
    #     de l'avant-veille au soir, forcément déjà finie à l'heure où l'on
    #     est puisque la fenêtre d'affichage courante commence à "aujourd'hui 18h") ---
    candidate_evening = window_start.date() - timedelta(days=1)
    cand_start, cand_end = corr_window_for_evening(candidate_evening, sunset_by_date, sunrise_by_date)
    night_ready = cand_end is not None and now >= cand_end
    night_key = candidate_evening.isoformat()

    if night_ready and bias.get("last_processed_night") != night_key and DATACAKE_TOKEN and DATACAKE_DEVICE_ID:
        obs_series = fetch_datacake_series(cand_start - timedelta(minutes=30), cand_end + timedelta(minutes=30))
        if obs_series is None:
            # Échec de la requête (réseau/HTTP) : on NE marque PAS cette nuit
            # comme traitée, pour que le prochain passage horaire réessaie -
            # sinon une panne temporaire de l'API condamnerait cette nuit à
            # rester non-apprise pour toujours.
            print(
                f"[warn] Nuit {night_key} non traitée (échec de récupération Datacake) — "
                "nouvel essai au prochain passage.",
                file=sys.stderr,
            )
            learned = None
        else:
            learned = []
            t = cand_start
            while t <= cand_end:
                if t in idx_by_time:
                    i = idx_by_time[t]
                    eff_cloud = effective_cloud(cloud_low[i], cloud_mid[i], cloud_high[i])
                    ws_ = wind_speed[i] or 0
                    clarity_ = clarity_factor(eff_cloud, ws_)
                    if clarity_ >= LEARN_CLARITY_MIN:
                        obs_v = nearest_value(obs_series, t)
                        if obs_v is not None and temp[i] is not None:
                            bucket = min(int((t - cand_start).total_seconds() // 3600), MAX_BUCKET_HOURS)
                            error = obs_v - temp[i]
                            key = str(bucket)
                            old_val = profile.get(key, default_offset(bucket))
                            new_val = (1 - alpha) * old_val + alpha * error
                            profile[key] = round(new_val, 2)
                            learned.append({"bucket": bucket, "error_c": round(error, 2), "offset_after": profile[key]})
                t += timedelta(hours=1)

        if learned is not None:
            bias["offset_profile"] = profile
            bias["last_processed_night"] = night_key
            bias["last_night_samples"] = len(learned)
            bias["last_night_error_c"] = (
                round(sum(x["error_c"] for x in learned) / len(learned), 2) if learned else None
            )
            bias["history"].append({"night": night_key, "buckets_learned": learned})
            bias["history"] = bias["history"][-MAX_HISTORY:]

    save_bias_state(bias)

    output = {
        "generated_at": now.isoformat(),
        "run_time": times[0].isoformat() if times else None,
        "location": {"name": LOCATION_NAME, "lat": LAT, "lon": LON},
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "correction": {
            "current_offset_c": round(current_offset_c, 2) if current_offset_c is not None else None,
            "offset_profile": {k: profile[k] for k in sorted(profile, key=int)},
            "alpha": alpha,
            "last_night_error_c": bias.get("last_night_error_c"),
            "last_night_samples": bias.get("last_night_samples", 0),
            "clear_cloud_threshold_pct": CLEAR_CLOUD_THRESHOLD,
            "cloud_zero_threshold_pct": CLOUD_ZERO_THRESHOLD,
            "calm_wind_threshold_kmh": CALM_WIND_THRESHOLD,
            "wind_zero_threshold_kmh": WIND_ZERO_THRESHOLD,
            "high_cloud_attenuation": HIGH_CLOUD_ATTENUATION,
            "sunset_lead_minutes": SUNSET_LEAD_MINUTES,
        },
        "hours": hours_out,
    }

    with open(FORECAST_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"OK — {len(hours_out)} heures écrites, {len(profile)} cases horaires apprises")


if __name__ == "__main__":
    main()
