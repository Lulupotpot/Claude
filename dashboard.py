#!/usr/bin/env python3
"""Tableau de bord marathon a partir des donnees COROS.

Usage :
    python dashboard.py            # rafraichit les donnees et reconstruit index.html
    python dashboard.py --no-fetch # reconstruit la page a partir du dernier instantane
    python dashboard.py --status    # diagnostic des sources de donnees

Le script ne demande jamais de mot de passe. Il cherche un jeton COROS deja en
cache (variable d'environnement, ~/.coros/, ou .coros_token.json a cote du
script). S'il en trouve un, il interroge l'API COROS et reecrit l'instantane
local. Sinon il reconstruit la page a partir de data/coros_snapshot.json.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

# --------------------------------------------------------------------------
# CONFIGURATION  (les seules valeurs a ajuster a la main)
# --------------------------------------------------------------------------

RACE_NAME = "Marathon de Lille"
RACE_DATE = date(2026, 10, 25)

# Records personnels declares (secondes). Servent de comparaison aux
# predictions COROS ; ils ne sont pas lus depuis l'API.
PERSONAL_BESTS = {
    "5k": 21 * 60 + 49,
    "10k": 44 * 60 + 17,
    "15k": 82 * 60,
}

# Physiologie utilisee pour la charge modelisee et le plafond "footing facile".
HR_MAX = 191               # Tanaka : 208 - 0.7 x 28 ans
EASY_HR_CEILING = 149      # ~78 % FCmax : au-dessus, la sortie n'est plus facile
LONG_RUN_KM = 15.0         # a partir de cette distance : "sortie longue"

QUALITY_KEYWORDS = (
    "fractionn", "interval", "seuil", "tempo", "vma", "allure", "cote",
    "côte", "piste", "specifique", "spécifique", "fartlek", "repet", "répét",
    "bloc", "finale", "progressif", "rappel d'intensite", "rappel d'intensité",
)

RUN_SPORT_TYPES = {100, 101, 102, 103}

WEEKS_SHOWN = 12
RECENT_RUN_WEEKS = 4

HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_PATH = os.path.join(HERE, "data", "coros_snapshot.json")
VO2_HISTORY_PATH = os.path.join(HERE, "data", "vo2max_history.json")
OUTPUT_PATH = os.path.join(HERE, "index.html")

COROS_API_BASE = "https://teamapi.coros.com"
TOKEN_ENV = "COROS_ACCESS_TOKEN"
TOKEN_FILES = (
    os.path.join(os.path.expanduser("~"), ".coros", "token.json"),
    os.path.join(HERE, ".coros_token.json"),
)

# --------------------------------------------------------------------------
# Petits utilitaires
# --------------------------------------------------------------------------


def parse_day(value: str) -> date:
    """Accepte '2026-09-10' comme '20260910'."""
    value = str(value)
    if "-" in value:
        return date.fromisoformat(value)
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def fmt_pace(sec_per_km):
    if not sec_per_km:
        return "—"
    sec_per_km = int(round(sec_per_km))
    return f"{sec_per_km // 60}:{sec_per_km % 60:02d}"


def fmt_duration(seconds):
    if not seconds:
        return "—"
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) / 2


def linear_fit(xs, ys):
    """Regression lineaire simple ; renvoie (pente, ordonnee) ou None."""
    pairs = [(x, y) for x, y in zip(xs, ys) if y is not None]
    if len(pairs) < 2:
        return None
    n = len(pairs)
    sx = sum(p[0] for p in pairs)
    sy = sum(p[1] for p in pairs)
    sxx = sum(p[0] * p[0] for p in pairs)
    sxy = sum(p[0] * p[1] for p in pairs)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-9:
        return None
    slope = (n * sxy - sx * sy) / denom
    return slope, (sy - slope * sx) / n


# --------------------------------------------------------------------------
# Sources de donnees
# --------------------------------------------------------------------------


class TokenNotFound(Exception):
    pass


def find_token():
    """Cherche un jeton COROS deja en cache. Ne demande jamais rien a l'utilisateur.

    Ordre : $COROS_ACCESS_TOKEN, ~/.coros/token.json, ./.coros_token.json
    Le fichier JSON doit contenir une cle 'accessToken' (ou 'token').
    """
    env = os.environ.get(TOKEN_ENV, "").strip()
    if env:
        return env, TOKEN_ENV

    for path in TOKEN_FILES:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"  ! jeton illisible dans {path} ({exc})", file=sys.stderr)
            continue
        token = (blob.get("accessToken") or blob.get("token") or "").strip()
        if token:
            return token, path

    raise TokenNotFound(
        "aucun jeton COROS en cache "
        f"(ni ${TOKEN_ENV}, ni {', '.join(TOKEN_FILES)})"
    )


class CorosClient:
    """Client minimal de l'API web COROS, authentifie par jeton en cache.

    Les chemins ci-dessous sont ceux de l'application web COROS. S'ils changent,
    il suffit de corriger ENDPOINTS : le reste du script est inchange.
    """

    ENDPOINTS = {
        "activities": "/activity/query",
        "daily": "/statistic/metrics/query",
    }

    def __init__(self, token, timeout=30):
        self.token = token
        self.timeout = timeout

    def _get(self, path, params):
        url = f"{COROS_API_BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={
                "accesstoken": self.token,
                "Accept": "application/json",
                "User-Agent": "coros-marathon-dashboard/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        # COROS repond {"result":"0000","data":...} ; tout autre code est une erreur.
        if isinstance(payload, dict) and payload.get("result") not in (None, "0000"):
            raise RuntimeError(
                f"COROS a refuse {path} : result={payload.get('result')} "
                f"message={payload.get('message')!r}"
            )
        return payload.get("data") if isinstance(payload, dict) else payload

    def fetch(self, since: date, until: date) -> dict:
        """Renvoie un instantane au meme format que data/coros_snapshot.json."""
        raw_acts = self._get(
            self.ENDPOINTS["activities"],
            {"size": 300, "pageNumber": 1,
             "startDay": since.strftime("%Y%m%d"), "endDay": until.strftime("%Y%m%d")},
        )
        raw_daily = self._get(
            self.ENDPOINTS["daily"],
            {"startDay": since.strftime("%Y%m%d"), "endDay": until.strftime("%Y%m%d")},
        )
        return normalise_api_payload(raw_acts, raw_daily, since, until)


def normalise_api_payload(raw_acts, raw_daily, since, until) -> dict:
    """Convertit la reponse brute de l'API vers le schema interne.

    Tolerant aux champs manquants : une metrique absente devient None et la
    section correspondante du tableau de bord s'efface d'elle-meme.
    """
    def dig(blob, *keys, default=None):
        for key in keys:
            if isinstance(blob, dict) and key in blob and blob[key] is not None:
                return blob[key]
        return default

    act_rows = dig(raw_acts, "dataList", "activityList", "list", default=raw_acts) or []
    activities = []
    for row in act_rows:
        if not isinstance(row, dict):
            continue
        distance_m = dig(row, "distance", "totalDistance", default=0) or 0
        duration = dig(row, "totalTime", "workoutTime", "duration", default=0) or 0
        km = round(float(distance_m) / 1000.0, 2)
        day = dig(row, "date", "startDay", "happenDay")
        if day is None:
            continue
        activities.append({
            "date": parse_day(day).isoformat(),
            "sportType": int(dig(row, "sportType", "mode", default=0) or 0),
            "sport": str(dig(row, "sportTypeName", "name", default="")),
            "title": str(dig(row, "name", "title", "labelName", default="")),
            "durationSec": int(duration),
            "distanceKm": km,
            "paceSecPerKm": round(int(duration) / km) if km >= 0.4 and duration else None,
            "avgHr": dig(row, "avgHeartRate", "heartRate", "avgHr"),
            "calories": dig(row, "calorie", "calories"),
            "labelId": str(dig(row, "labelId", "id", default="")),
        })

    daily_rows = dig(raw_daily, "dataList", "list", default=raw_daily) or []
    daily, resting_hr, sleep_hrv, training_load = [], [], [], []
    for row in daily_rows:
        if not isinstance(row, dict):
            continue
        day = dig(row, "date", "happenDay")
        if day is None:
            continue
        iso_day = parse_day(day).isoformat()
        daily.append({
            "date": iso_day,
            "steps": dig(row, "steps", "totalStep"),
            "calories": dig(row, "calorie", "calories"),
            "exerciseMin": dig(row, "exerciseTime", "trainingTime"),
            "stressAvg": dig(row, "stress", "avgStress"),
            "sleepTotalMin": dig(row, "totalSleep", "sleepTime"),
            "sleepDeepMin": dig(row, "deepSleep"),
            "sleepLightMin": dig(row, "lightSleep"),
            "sleepRemMin": dig(row, "remSleep"),
            "sleepAwakeMin": dig(row, "awakeTime"),
            "sleepHrAvg": dig(row, "sleepAvgHr", "avgSleepHeartRate"),
            "sleepScore": dig(row, "sleepScore"),
        })
        if dig(row, "restHeartRate", "restingHeartRate") is not None:
            resting_hr.append({"date": iso_day,
                               "bpm": dig(row, "restHeartRate", "restingHeartRate")})
        if dig(row, "hrv", "sleepHrv", "avgHrv") is not None:
            sleep_hrv.append({
                "date": iso_day,
                "avgMs": dig(row, "hrv", "sleepHrv", "avgHrv"),
                "lowMs": dig(row, "hrvLow", "hrvMin"),
                "highMs": dig(row, "hrvHigh", "hrvMax"),
                "baselineMs": dig(row, "hrvBaseline"),
                "evaluation": dig(row, "hrvEvaluation", default=""),
            })
        if dig(row, "shortTrainingLoad", "atl") is not None:
            atl = dig(row, "shortTrainingLoad", "atl")
            ctl = dig(row, "longTrainingLoad", "ctl")
            training_load.append({
                "date": iso_day, "atl": atl, "ctl": ctl,
                "ratio": round(atl / ctl, 2) if atl and ctl else None,
                "comment": dig(row, "loadComment", default=""),
            })

    key = lambda r: r["date"]
    return {
        "schema": 1,
        "source": "api",
        "fetchedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "window": {"from": since.isoformat(), "to": until.isoformat()},
        "activities": sorted(activities, key=key),
        "daily": sorted(daily, key=key),
        "restingHr": sorted(resting_hr, key=key),
        "sleepHrv": sorted(sleep_hrv, key=key),
        "trainingLoad": sorted(training_load, key=key),
    }


def load_snapshot(path=SNAPSHOT_PATH):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_snapshot(snapshot, path=SNAPSHOT_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def merge_snapshots(old, new):
    """Fusionne un rafraichissement dans l'instantane existant.

    Les nouvelles valeurs gagnent, mais l'historique plus ancien que la fenetre
    rafraichie est conserve : la page garde ses 12 semaines meme si l'API n'en
    renvoie que quelques-unes.
    """
    merged = dict(old)
    merged.update({k: v for k, v in new.items() if k not in ("activities", "daily",
                                                             "restingHr", "sleepHrv",
                                                             "trainingLoad")})
    for series in ("activities", "daily", "restingHr", "sleepHrv", "trainingLoad"):
        by_key = {}
        id_of = (lambda r: r.get("labelId") or r["date"]) if series == "activities" \
            else (lambda r: r["date"])
        for row in old.get(series, []):
            by_key[id_of(row)] = row
        for row in new.get(series, []):
            by_key[id_of(row)] = row
        merged[series] = sorted(by_key.values(), key=lambda r: r["date"])
    return merged


def acquire_data(allow_fetch=True):
    """Renvoie (instantane, description_de_la_source)."""
    snapshot = None
    if os.path.exists(SNAPSHOT_PATH):
        try:
            snapshot = load_snapshot()
        except ValueError as exc:
            print(f"  ! instantane illisible ({exc})", file=sys.stderr)

    if not allow_fetch:
        if snapshot is None:
            sys.exit("Aucun instantane local et --no-fetch demande : rien a afficher.")
        return snapshot, "instantane local (--no-fetch)"

    try:
        token, origin = find_token()
    except TokenNotFound as exc:
        if snapshot is None:
            sys.exit(f"Pas de jeton et pas d'instantane : {exc}")
        print(f"  · pas de rafraichissement en direct : {exc}")
        return snapshot, "instantane local"

    until = date.today()
    since = until - timedelta(weeks=WEEKS_SHOWN + 2)
    try:
        fresh = CorosClient(token).fetch(since, until)
    except (urllib.error.URLError, RuntimeError, ValueError, KeyError) as exc:
        if snapshot is None:
            sys.exit(f"Echec de l'appel COROS et pas d'instantane : {exc}")
        print(f"  ! appel COROS en echec ({exc}) — on garde l'instantane local")
        return snapshot, "instantane local (appel API en echec)"

    merged = merge_snapshots(snapshot or {}, fresh)
    save_snapshot(merged)
    print(f"  · donnees rafraichies via l'API COROS (jeton : {origin})")
    return merged, f"API COROS, {len(fresh.get('activities', []))} activites recuperees"


# --------------------------------------------------------------------------
# Analyse
# --------------------------------------------------------------------------


def classify_run(activity):
    """Intention de la seance, deduite du nom et de la distance."""
    if activity["distanceKm"] >= LONG_RUN_KM:
        return "longue"
    title = (activity.get("title") or "").lower()
    if any(word in title for word in QUALITY_KEYWORDS):
        return "qualite"
    return "facile"


CLASS_LABELS = {"facile": "Footing facile", "qualite": "Séance qualité",
                "longue": "Sortie longue"}


def runs_only(activities):
    return [a for a in activities if a.get("sportType") in RUN_SPORT_TYPES
            and a.get("distanceKm")]


def trimp(activity, hr_rest):
    """Charge d'entrainement estimee (TRIMP de Banister, homme)."""
    hr = activity.get("avgHr")
    minutes = (activity.get("durationSec") or 0) / 60.0
    if not hr or minutes <= 0 or HR_MAX <= hr_rest:
        return 0.0
    reserve = max(0.0, min(1.0, (hr - hr_rest) / (HR_MAX - hr_rest)))
    return minutes * reserve * 0.64 * math.exp(1.92 * reserve)


def build_load_series(snapshot, days, hr_rest):
    """CTL / ATL / TSB jour par jour sur la fenetre demandee.

    COROS ne publie sa charge que sur ~31 jours. Au-dela, la serie est
    modelisee a partir du TRIMP des seances, puis mise a l'echelle des valeurs
    COROS sur la periode de recouvrement. Chaque point sait s'il est mesure ou
    estime.
    """
    native = {parse_day(r["date"]): r for r in snapshot.get("trainingLoad", [])}

    per_day = {}
    for act in snapshot.get("activities", []):
        per_day.setdefault(parse_day(act["date"]), 0.0)
        per_day[parse_day(act["date"])] += trimp(act, hr_rest)

    if not per_day and not native:
        return []

    warmup_start = min(list(per_day) + list(native)) if (per_day or native) else days[0]
    start = min(warmup_start, days[0])

    def ewma(tau):
        """Moyenne exponentielle du TRIMP quotidien, jour par jour."""
        value, out, cursor = 0.0, {}, start
        while cursor <= days[-1]:
            value += (per_day.get(cursor, 0.0) - value) / tau
            out[cursor] = value
            cursor += timedelta(days=1)
        return out

    def calibrate(field, taus):
        """Constante de temps et facteur d'echelle qui collent le mieux aux
        valeurs COROS sur la periode de recouvrement.

        Un simple facteur d'echelle ne suffit pas : COROS lisse la charge
        autrement que le modele, et un ATL a 7 jours produirait des pics que
        COROS ne rapporte jamais. On ajuste donc aussi le lissage.
        """
        best = None
        for tau in taus:
            curve = ewma(tau)
            pairs = [(curve[d], native[d][field])
                     for d in native if d in curve and native[d].get(field)]
            if len(pairs) < 5:
                continue
            den = sum(m * m for m, _ in pairs)
            scale = sum(m * n for m, n in pairs) / den if den > 1e-9 else 1.0
            err = sum((m * scale - n) ** 2 for m, n in pairs) / len(pairs)
            if best is None or err < best[0]:
                best = (err, curve, scale, tau)
        if best is None:
            return ewma(taus[len(taus) // 2]), 1.0
        return best[1], best[2]

    ctl_curve, scale_ctl = calibrate("ctl", list(range(28, 57, 2)))
    atl_curve, scale_atl = calibrate("atl", list(range(7, 29)))
    modelled = {day: (ctl_curve.get(day, 0.0), atl_curve.get(day, 0.0))
                for day in ctl_curve}

    series = []
    for day in days:
        row = native.get(day)
        if row and row.get("ctl") is not None and row.get("atl") is not None:
            ctl_v, atl_v, measured = float(row["ctl"]), float(row["atl"]), True
        elif day in modelled:
            ctl_v, atl_v, measured = (modelled[day][0] * scale_ctl,
                                      modelled[day][1] * scale_atl, False)
        else:
            continue
        series.append({
            "date": day.isoformat(),
            "ctl": round(ctl_v, 1),
            "atl": round(atl_v, 1),
            "tsb": round(ctl_v - atl_v, 1),
            "measured": measured,
            "comment": (row or {}).get("comment", ""),
        })
    return series


def build_weekly_mileage(activities, weeks, today):
    runs = runs_only(activities)
    current_week = monday_of(today)
    buckets = []
    for start in weeks:
        end = start + timedelta(days=6)
        week_runs = [r for r in runs if start <= parse_day(r["date"]) <= end]
        buckets.append({
            "weekStart": start.isoformat(),
            "label": start.strftime("%d/%m"),
            "km": round(sum(r["distanceKm"] for r in week_runs), 1),
            "runs": len(week_runs),
            "timeSec": sum(r.get("durationSec") or 0 for r in week_runs),
            "partial": start == current_week,
        })

    # La tendance ignore la semaine en cours, incomplete par construction.
    complete = [(i, b["km"]) for i, b in enumerate(buckets) if not b["partial"]]
    fit = linear_fit([i for i, _ in complete], [k for _, k in complete])
    for i, bucket in enumerate(buckets):
        bucket["trend"] = round(fit[0] * i + fit[1], 1) if fit else None
    return buckets, (fit[0] if fit else None)


def build_recovery(snapshot, days):
    hrv = {parse_day(r["date"]): r for r in snapshot.get("sleepHrv", [])}
    rhr = {parse_day(r["date"]): r for r in snapshot.get("restingHr", [])}
    daily = {parse_day(r["date"]): r for r in snapshot.get("daily", [])}

    rows = []
    for day in days:
        h, r, d = hrv.get(day), rhr.get(day), daily.get(day, {})
        sleep_min = d.get("sleepTotalMin")
        # Les nuits de moins d'une heure sont des artefacts de synchronisation.
        if sleep_min is not None and sleep_min < 60:
            sleep_min = None
        rows.append({
            "date": day.isoformat(),
            "label": day.strftime("%d/%m"),
            "hrv": h.get("avgMs") if h else None,
            "hrvLow": h.get("lowMs") if h else None,
            "hrvHigh": h.get("highMs") if h else None,
            "hrvBaseline": h.get("baselineMs") if h else None,
            "hrvEval": (h or {}).get("evaluation") or "",
            "rhr": r.get("bpm") if r else None,
            "sleepHours": round(sleep_min / 60.0, 2) if sleep_min else None,
            "deepHours": round(d["sleepDeepMin"] / 60.0, 2) if d.get("sleepDeepMin") else None,
            "stress": d.get("stressAvg"),
        })
    return rows


def update_vo2_history(vo2max, today, path=VO2_HISTORY_PATH):
    """COROS ne renvoie que le VO2max courant. On l'archive a chaque execution
    pour qu'une tendance apparaisse au fil des semaines."""
    history = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                history = json.load(fh)
        except (OSError, ValueError):
            history = []
    if not isinstance(history, list):
        history = []

    if vo2max is not None:
        by_date = {row["date"]: row for row in history if isinstance(row, dict)}
        by_date[today.isoformat()] = {"date": today.isoformat(), "vo2max": vo2max}
        history = sorted(by_date.values(), key=lambda r: r["date"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(history, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
    return history


def build_pace_panel(activities, today):
    """Repartition facile / qualite / longue, et verdict sur les jours faciles."""
    horizon = today - timedelta(weeks=WEEKS_SHOWN)
    runs = [r for r in runs_only(activities) if parse_day(r["date"]) >= horizon]

    points = []
    for run in runs:
        day = parse_day(run["date"])
        points.append({
            "date": run["date"],
            "dayOffset": (day - horizon).days,
            "label": day.strftime("%d/%m"),
            "kind": classify_run(run),
            "title": run.get("title") or "",
            "km": run["distanceKm"],
            "pace": run.get("paceSecPerKm"),
            "paceLabel": fmt_pace(run.get("paceSecPerKm")),
            "hr": run.get("avgHr"),
        })

    easy = [p for p in points if p["kind"] == "facile" and p["hr"]]
    too_hard = [p for p in easy if p["hr"] > EASY_HR_CEILING]
    quality = [p for p in points if p["kind"] == "qualite" and p["hr"]]

    return {
        "points": points,
        "horizon": horizon.isoformat(),
        "easyCount": len(easy),
        "tooHard": too_hard,
        "easyAvgHr": round(mean(p["hr"] for p in easy)) if easy else None,
        "qualityAvgHr": round(mean(p["hr"] for p in quality)) if quality else None,
        "easyAvgPace": mean(p["pace"] for p in easy if p["pace"]),
        "qualityAvgPace": mean(p["pace"] for p in quality if p["pace"]),
    }


def analyse(snapshot, today):
    days = [today - timedelta(days=i) for i in range(WEEKS_SHOWN * 7 - 1, -1, -1)]
    weeks = [monday_of(today) - timedelta(weeks=i)
             for i in range(WEEKS_SHOWN - 1, -1, -1)]

    hr_rest = median(r["bpm"] for r in snapshot.get("restingHr", [])
                     if r.get("bpm")) or 55
    load = build_load_series(snapshot, days, hr_rest)
    weekly, weekly_slope = build_weekly_mileage(snapshot.get("activities", []),
                                                weeks, today)
    recovery = build_recovery(snapshot, days[-42:])
    pace = build_pace_panel(snapshot.get("activities", []), today)

    fitness = snapshot.get("fitness") or {}
    vo2_history = update_vo2_history(fitness.get("vo2max"), today)

    recent_cutoff = today - timedelta(weeks=RECENT_RUN_WEEKS)
    recent = [dict(r, kind=classify_run(r), kindLabel=CLASS_LABELS[classify_run(r)],
                   paceLabel=fmt_pace(r.get("paceSecPerKm")),
                   durationLabel=fmt_duration(r.get("durationSec")))
              for r in runs_only(snapshot.get("activities", []))
              if parse_day(r["date"]) >= recent_cutoff]
    recent.sort(key=lambda r: r["date"], reverse=True)

    days_left = (RACE_DATE - today).days
    last_28 = [r for r in runs_only(snapshot.get("activities", []))
               if parse_day(r["date"]) >= today - timedelta(days=28)]

    return {
        "today": today,
        "daysLeft": days_left,
        "weeksLeft": days_left / 7.0,
        "hrRest": round(hr_rest),
        "load": load,
        "weekly": weekly,
        "weeklySlope": weekly_slope,
        "recovery": recovery,
        "pace": pace,
        "recent": recent,
        "fitness": fitness,
        "vo2History": vo2_history,
        "km4w": round(sum(r["distanceKm"] for r in last_28), 1),
        "runs4w": len(last_28),
    }


# --------------------------------------------------------------------------
# Rendu HTML
# --------------------------------------------------------------------------

PAGE_CSS = """
:root {
  color-scheme: light;
  --plane:        #f9f9f7;
  --surface:      #fcfcfb;
  --ink:          #0b0b0b;
  --ink-2:        #52514e;
  --ink-muted:    #898781;
  --grid:         #e1e0d9;
  --axis:         #c3c2b7;
  --hairline:     rgba(11, 11, 11, 0.10);
  --series-1:     #2a78d6;
  --series-2:     #eb6834;
  --series-3:     #1baf7a;
  --good:         #0ca30c;
  --warning:      #fab219;
  --critical:     #d03b3b;
  --wash:         rgba(11, 11, 11, 0.035);
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --plane:      #0d0d0d;
    --surface:    #1a1a19;
    --ink:        #ffffff;
    --ink-2:      #c3c2b7;
    --ink-muted:  #898781;
    --grid:       #2c2c2a;
    --axis:       #383835;
    --hairline:   rgba(255, 255, 255, 0.10);
    --series-1:   #3987e5;
    --series-2:   #d95926;
    --series-3:   #199e70;
    --wash:       rgba(255, 255, 255, 0.045);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --plane:      #0d0d0d;
  --surface:    #1a1a19;
  --ink:        #ffffff;
  --ink-2:      #c3c2b7;
  --ink-muted:  #898781;
  --grid:       #2c2c2a;
  --axis:       #383835;
  --hairline:   rgba(255, 255, 255, 0.10);
  --series-1:   #3987e5;
  --series-2:   #d95926;
  --series-3:   #199e70;
  --wash:       rgba(255, 255, 255, 0.045);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--plane);
  color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased;
}
.shell { max-width: 1180px; margin: 0 auto; padding-block: 28px 56px; padding-left: 20px; padding-right: 20px; }

/* --- en-tete ------------------------------------------------------------ */
.masthead { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 20px 32px; margin-bottom: 28px; }
.masthead h1 { margin: 0; font-size: 19px; font-weight: 600; letter-spacing: -0.01em; }
.masthead .race-date { margin: 2px 0 0; color: var(--ink-2); font-size: 13px; }
.countdown { display: flex; align-items: baseline; gap: 10px; }
.countdown .figure { font-size: 60px; font-weight: 650; line-height: 0.95; letter-spacing: -0.03em; }
.countdown .unit { font-size: 15px; color: var(--ink-2); }
.countdown .weeks { color: var(--ink-muted); font-size: 13px; }
.spacer { flex: 1 1 auto; }
.theme-toggle {
  border: 1px solid var(--hairline); background: var(--surface); color: var(--ink-2);
  border-radius: 999px; padding: 7px 14px; font: inherit; font-size: 12px; cursor: pointer;
}
.theme-toggle:hover { color: var(--ink); }

/* --- pastilles ---------------------------------------------------------- */
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(158px, 1fr)); gap: 12px; margin-bottom: 26px; }
.tile { background: var(--surface); border: 1px solid var(--hairline); border-radius: 12px; padding: 14px 16px; }
.tile .k { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-muted); }
.tile .v { font-size: 27px; font-weight: 620; letter-spacing: -0.02em; margin-top: 5px; }
.tile .v small { font-size: 14px; font-weight: 500; color: var(--ink-2); margin-left: 3px; }
.tile .n { font-size: 12px; color: var(--ink-2); margin-top: 3px; }
.tile .n.good { color: var(--good); }
.tile .n.warn { color: var(--warning); }
.tile .n.bad  { color: var(--critical); }

/* --- cartes ------------------------------------------------------------- */
.card { background: var(--surface); border: 1px solid var(--hairline); border-radius: 14px; padding: 20px 22px 18px; margin-bottom: 20px; }
.card > header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 14px; margin-bottom: 4px; }
.card h2 { margin: 0; font-size: 15px; font-weight: 600; }
.card .caption { margin: 0 0 16px; color: var(--ink-2); font-size: 12.5px; max-width: 76ch; }
.chart-wrap { position: relative; height: 320px; }
.chart-wrap.short { height: 232px; }
.triptych { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 22px; }
.triptych h3 { margin: 0 0 2px; font-size: 13px; font-weight: 600; }
.triptych .sub { margin: 0 0 10px; font-size: 12px; color: var(--ink-muted); }
.split { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 26px; align-items: start; }

/* --- legende ------------------------------------------------------------ */
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 0 0 12px; padding: 0; list-style: none; font-size: 12.5px; color: var(--ink-2); }
.legend li { display: flex; align-items: center; gap: 7px; }
.swatch { width: 11px; height: 11px; border-radius: 3px; flex: none; }
.swatch.line { height: 3px; border-radius: 2px; width: 15px; }

/* --- tableaux ----------------------------------------------------------- */
.table-toggle {
  margin-left: auto; border: 1px solid var(--hairline); background: transparent; color: var(--ink-2);
  border-radius: 999px; padding: 4px 12px; font: inherit; font-size: 11.5px; cursor: pointer;
}
.table-toggle:hover { color: var(--ink); }
.table-scroll { overflow-x: auto; margin-top: 16px; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 7px 10px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th:first-child, td:first-child, th.l, td.l { text-align: left; }
thead th { color: var(--ink-muted); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
tbody tr:last-child td { border-bottom: none; }
td .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 7px; vertical-align: 1px; }
tr.flag td { background: var(--wash); }

/* --- divers ------------------------------------------------------------- */
.note { font-size: 12px; color: var(--ink-muted); margin: 14px 0 0; }
.verdict { margin: 0 0 16px; padding: 12px 14px; border-radius: 10px; background: var(--wash); font-size: 13px; line-height: 1.55; }
.verdict strong { font-weight: 620; }
.footer { color: var(--ink-muted); font-size: 12px; margin-top: 34px; text-align: center; }
.footer code { font-size: 11.5px; }
@media (max-width: 560px) {
  .countdown .figure { font-size: 46px; }
  .chart-wrap { height: 260px; }
  .card { padding: 16px 14px 14px; }
}
"""

PAGE_JS = r"""
(function () {
  var D = window.__DATA__;
  var charts = [];

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }
  function tokens() {
    return {
      s1: css('--series-1'), s2: css('--series-2'), s3: css('--series-3'),
      ink: css('--ink'), ink2: css('--ink-2'), muted: css('--ink-muted'),
      grid: css('--grid'), axis: css('--axis'), surface: css('--surface'),
      wash: css('--wash'), warning: css('--warning'), critical: css('--critical')
    };
  }
  function alpha(hex, a) {
    var h = hex.replace('#', '');
    if (h.length === 3) { h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2]; }
    var n = parseInt(h, 16);
    return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
  }

  /* Moyenne glissante, en ignorant les trous. */
  function rolling(values, window) {
    return values.map(function (_, i) {
      var slice = values.slice(Math.max(0, i - window + 1), i + 1)
                        .filter(function (v) { return v !== null && v !== undefined; });
      if (slice.length < Math.max(2, Math.floor(window / 3))) { return null; }
      return Math.round((slice.reduce(function (a, b) { return a + b; }, 0) / slice.length) * 10) / 10;
    });
  }

  function baseOptions(t, extra) {
    var opts = {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      layout: { padding: { top: 6, right: 8, bottom: 0, left: 0 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: t.surface, titleColor: t.ink, bodyColor: t.ink2,
          borderColor: t.axis, borderWidth: 1, padding: 10, cornerRadius: 8,
          displayColors: true, boxWidth: 9, boxHeight: 9, usePointStyle: true
        }
      },
      scales: {
        x: {
          grid: { display: false, drawBorder: false },
          border: { color: t.axis },
          ticks: { color: t.muted, font: { size: 11 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 9 }
        },
        y: {
          grid: { color: t.grid, drawTicks: false, drawBorder: false },
          border: { display: false },
          ticks: { color: t.muted, font: { size: 11 }, padding: 8 }
        }
      }
    };
    return Object.assign(opts, extra || {});
  }

  /* Bande de fond marquant la portion estimee d'une serie. */
  function washPlugin(untilIndex, t) {
    return {
      id: 'estimatedWash',
      beforeDatasetsDraw: function (chart) {
        if (untilIndex < 1) { return; }
        var xs = chart.scales.x, area = chart.chartArea;
        if (!xs || !area) { return; }
        var right = xs.getPixelForValue(untilIndex);
        var ctx = chart.ctx;
        ctx.save();
        ctx.fillStyle = t.wash;
        ctx.fillRect(area.left, area.top, Math.max(0, right - area.left), area.bottom - area.top);
        ctx.restore();
      }
    };
  }

  /* Ligne de reference horizontale (plafond, zero...). */
  function rulePlugin(value, color, label, t) {
    return {
      id: 'rule-' + label,
      afterDatasetsDraw: function (chart) {
        var ys = chart.scales.y, area = chart.chartArea;
        if (!ys || !area) { return; }
        var y = ys.getPixelForValue(value);
        if (y < area.top || y > area.bottom) { return; }
        var ctx = chart.ctx;
        ctx.save();
        ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.setLineDash([5, 4]);
        ctx.beginPath(); ctx.moveTo(area.left, y); ctx.lineTo(area.right, y); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = color; ctx.font = '600 11px system-ui, sans-serif';
        ctx.textAlign = 'right'; ctx.textBaseline = 'bottom';
        ctx.fillText(label, area.right - 2, y - 4);
        ctx.restore();
      }
    };
  }

  function line(label, data, color, opts) {
    return Object.assign({
      type: 'line',
      label: label, data: data, borderColor: color, backgroundColor: color,
      borderWidth: 2, pointRadius: 0, pointHoverRadius: 5, pointHoverBorderWidth: 2,
      pointHoverBorderColor: css('--surface'), tension: 0.25, spanGaps: true
    }, opts || {});
  }

  /* ------------------------------------------------------------------ */
  function buildLoad(t) {
    var rows = D.load;
    var labels = rows.map(function (r) { return r.label; });
    var lastEstimated = -1;
    rows.forEach(function (r, i) { if (!r.measured) { lastEstimated = i; } });
    var dash = function (ctx) {
      return rows[ctx.p0DataIndex] && !rows[ctx.p0DataIndex].measured ? [5, 4] : undefined;
    };
    return new Chart(document.getElementById('loadChart'), {
      type: 'line',
      data: {
        labels: labels,
        datasets: [
          line('Forme (CTL)', rows.map(function (r) { return r.ctl; }), t.s1, { segment: { borderDash: dash } }),
          line('Fatigue (ATL)', rows.map(function (r) { return r.atl; }), t.s2, { segment: { borderDash: dash } }),
          line('Fraîcheur (TSB)', rows.map(function (r) { return r.tsb; }), t.s3, { segment: { borderDash: dash } })
        ]
      },
      options: baseOptions(t, {
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: t.surface, titleColor: t.ink, bodyColor: t.ink2,
            borderColor: t.axis, borderWidth: 1, padding: 10, cornerRadius: 8,
            usePointStyle: true, boxWidth: 9, boxHeight: 9,
            callbacks: {
              title: function (items) {
                var r = rows[items[0].dataIndex];
                return r.dateLabel + (r.measured ? '' : '  ·  estimé');
              },
              afterBody: function (items) {
                var r = rows[items[0].dataIndex];
                return r.comment ? ['', 'COROS : ' + r.comment] : [];
              }
            }
          }
        }
      }),
      plugins: [washPlugin(lastEstimated, t), rulePlugin(0, t.axis, '0', t)]
    });
  }

  function buildWeekly(t) {
    var rows = D.weekly;
    var fills = rows.map(function (r) { return r.partial ? alpha(t.s1, 0.32) : t.s1; });
    return new Chart(document.getElementById('weeklyChart'), {
      type: 'bar',
      data: {
        labels: rows.map(function (r) { return r.label; }),
        datasets: [
          {
            type: 'bar', label: 'Kilométrage', data: rows.map(function (r) { return r.km; }),
            backgroundColor: fills, borderRadius: 4, borderSkipped: 'bottom',
            categoryPercentage: 0.72, barPercentage: 0.86, order: 2
          },
          line('Tendance', rows.map(function (r) { return r.trend; }), t.s2,
               { borderWidth: 2, tension: 0, order: 1, borderDash: [6, 4] })
        ]
      },
      options: baseOptions(t, {
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: t.surface, titleColor: t.ink, bodyColor: t.ink2,
            borderColor: t.axis, borderWidth: 1, padding: 10, cornerRadius: 8,
            usePointStyle: true, boxWidth: 9, boxHeight: 9,
            callbacks: {
              title: function (items) {
                var r = rows[items[0].dataIndex];
                return 'Semaine du ' + r.dateLabel + (r.partial ? '  ·  en cours' : '');
              },
              afterBody: function (items) {
                var r = rows[items[0].dataIndex];
                return ['', r.runs + ' sortie(s) · ' + r.timeLabel];
              }
            }
          }
        },
        scales: {
          x: { grid: { display: false }, border: { color: t.axis },
               ticks: { color: t.muted, font: { size: 11 }, maxRotation: 0 } },
          y: { beginAtZero: true, grid: { color: t.grid, drawTicks: false },
               border: { display: false },
               ticks: { color: t.muted, font: { size: 11 }, padding: 8,
                        callback: function (v) { return v + ' km'; } } }
        }
      })
    });
  }
"""

PAGE_JS += r"""
  function recoveryChart(canvasId, rows, key, unit, t, opts) {
    opts = opts || {};
    var labels = rows.map(function (r) { return r.label; });
    var values = rows.map(function (r) { return r[key]; });
    var datasets = [];

    if (opts.band) {
      datasets.push({
        label: 'Plage normale (bas)', data: rows.map(function (r) { return r.hrvLow; }),
        borderColor: 'transparent', backgroundColor: 'transparent',
        pointRadius: 0, fill: false, tension: 0.3, spanGaps: true, order: 5
      });
      datasets.push({
        label: 'Plage normale COROS', data: rows.map(function (r) { return r.hrvHigh; }),
        borderColor: 'transparent', backgroundColor: alpha(t.muted, 0.16),
        pointRadius: 0, fill: '-1', tension: 0.3, spanGaps: true, order: 4
      });
    }

    datasets.push(Object.assign(
      line(opts.dailyLabel || 'Valeur du jour', values, t.s1, {
        borderWidth: opts.thin ? 1.5 : 2,
        pointRadius: function (ctx) { return ctx.raw === null ? 0 : 2.2; },
        pointBackgroundColor: t.s1, order: 2
      }), {}));

    datasets.push(line('Moyenne 7 j', rolling(values, 7), t.s2,
                       { borderWidth: 2.5, pointRadius: 0, order: 1 }));

    return new Chart(document.getElementById(canvasId), {
      type: 'line',
      data: { labels: labels, datasets: datasets },
      options: baseOptions(t, {
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: t.surface, titleColor: t.ink, bodyColor: t.ink2,
            borderColor: t.axis, borderWidth: 1, padding: 10, cornerRadius: 8,
            usePointStyle: true, boxWidth: 9, boxHeight: 9,
            filter: function (item) { return item.dataset.label !== 'Plage normale (bas)'; },
            callbacks: {
              title: function (items) { return rows[items[0].dataIndex].dateLabel; },
              label: function (item) {
                if (item.raw === null || item.raw === undefined) { return null; }
                return item.dataset.label + ' : ' + item.raw + ' ' + unit;
              }
            }
          }
        },
        scales: {
          x: { grid: { display: false }, border: { color: t.axis },
               ticks: { color: t.muted, font: { size: 11 }, maxRotation: 0, maxTicksLimit: 6 } },
          y: { grid: { color: t.grid, drawTicks: false }, border: { display: false },
               ticks: { color: t.muted, font: { size: 11 }, padding: 8,
                        callback: function (v) { return v + ' ' + unit; } } }
        }
      })
    });
  }

  function scatterSets(points, field, t) {
    var kinds = [
      { key: 'facile',  label: 'Footing facile', color: t.s1 },
      { key: 'qualite', label: 'Séance qualité', color: t.s2 },
      { key: 'longue',  label: 'Sortie longue',  color: t.s3 }
    ];
    return kinds.map(function (k, i) {
      return {
        label: k.label,
        data: points.filter(function (p) { return p.kind === k.key && p[field]; })
                    .map(function (p) { return { x: p.dayOffset, y: p[field], meta: p }; }),
        backgroundColor: k.color, borderColor: css('--surface'), borderWidth: 2,
        pointRadius: 6, pointHoverRadius: 8, order: 3 - i
      };
    });
  }

  function scatterOptions(t, yTitle, yFormat, reverse) {
    return baseOptions(t, {
      interaction: { mode: 'nearest', intersect: true },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: t.surface, titleColor: t.ink, bodyColor: t.ink2,
          borderColor: t.axis, borderWidth: 1, padding: 10, cornerRadius: 8,
          usePointStyle: true, boxWidth: 9, boxHeight: 9,
          callbacks: {
            title: function (items) {
              var m = items[0].raw.meta;
              return m.dateLabel + ' · ' + (m.title || m.kindLabel);
            },
            label: function (item) {
              var m = item.raw.meta;
              return [m.km.toFixed(2) + ' km', 'Allure ' + m.paceLabel + ' /km',
                      'FC moy. ' + (m.hr || '—') + ' bpm'];
            }
          }
        }
      },
      scales: {
        x: {
          type: 'linear', min: 0, max: D.pace.span,
          grid: { display: false }, border: { color: t.axis },
          ticks: {
            color: t.muted, font: { size: 11 }, maxRotation: 0, stepSize: 14,
            callback: function (v) { return D.pace.axis[v] || ''; }
          }
        },
        y: {
          reverse: !!reverse,
          grid: { color: t.grid, drawTicks: false }, border: { display: false },
          ticks: { color: t.muted, font: { size: 11 }, padding: 8, callback: yFormat }
        }
      }
    });
  }

  function buildPaceHr(t) {
    return new Chart(document.getElementById('paceHrChart'), {
      type: 'scatter',
      data: { datasets: scatterSets(D.pace.points, 'hr', t) },
      options: scatterOptions(t, 'FC', function (v) { return v + ' bpm'; }, false),
      plugins: [rulePlugin(D.easyCeiling, t.warning, 'plafond facile ' + D.easyCeiling + ' bpm', t)]
    });
  }

  function buildPace(t) {
    return new Chart(document.getElementById('paceChart'), {
      type: 'scatter',
      data: { datasets: scatterSets(D.pace.points, 'pace', t) },
      options: scatterOptions(t, 'Allure', function (v) {
        return Math.floor(v / 60) + ':' + ('0' + Math.round(v % 60)).slice(-2);
      }, true)
    });
  }

  function buildVo2(t) {
    var rows = D.vo2History;
    if (!rows || rows.length < 2) { return null; }
    return new Chart(document.getElementById('vo2Chart'), {
      type: 'line',
      data: {
        labels: rows.map(function (r) { return r.label; }),
        datasets: [line('VO2 max', rows.map(function (r) { return r.vo2max; }), t.s1,
                        { pointRadius: 3.5, tension: 0.2 })]
      },
      options: baseOptions(t, {
        scales: {
          x: { grid: { display: false }, border: { color: t.axis },
               ticks: { color: t.muted, font: { size: 11 }, maxRotation: 0 } },
          y: { grid: { color: t.grid, drawTicks: false }, border: { display: false },
               ticks: { color: t.muted, font: { size: 11 }, padding: 8 } }
        }
      })
    });
  }

  /* ------------------------------------------------------------------ */
  function render() {
    charts.forEach(function (c) { if (c) { c.destroy(); } });
    charts = [];
    var t = tokens();
    charts.push(buildLoad(t));
    charts.push(buildWeekly(t));
    charts.push(recoveryChart('hrvChart', D.recovery, 'hrv', 'ms', t,
                              { band: true, dailyLabel: 'VFC nocturne' }));
    charts.push(recoveryChart('rhrChart', D.recovery, 'rhr', 'bpm', t,
                              { dailyLabel: 'FC au repos' }));
    charts.push(recoveryChart('sleepChart', D.recovery, 'sleepHours', 'h', t,
                              { dailyLabel: 'Sommeil' }));
    charts.push(buildPaceHr(t));
    charts.push(buildPace(t));
    charts.push(buildVo2(t));
  }

  /* --- theme -------------------------------------------------------- */
  var toggle = document.getElementById('themeToggle');
  function currentTheme() {
    return document.documentElement.getAttribute('data-theme')
      || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  }
  function applyTheme(name) {
    document.documentElement.setAttribute('data-theme', name);
    try { localStorage.setItem('coros-dash-theme', name); } catch (e) { /* stockage bloque */ }
    toggle.textContent = name === 'dark' ? 'Thème clair' : 'Thème sombre';
    render();
  }
  try {
    var stored = localStorage.getItem('coros-dash-theme');
    if (stored) { document.documentElement.setAttribute('data-theme', stored); }
  } catch (e) { /* stockage bloque */ }
  toggle.textContent = currentTheme() === 'dark' ? 'Thème clair' : 'Thème sombre';
  toggle.addEventListener('click', function () {
    applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
    if (!document.documentElement.getAttribute('data-theme')) { render(); }
  });

  /* --- bascule tableau ---------------------------------------------- */
  Array.prototype.forEach.call(document.querySelectorAll('.table-toggle'), function (btn) {
    var target = document.getElementById(btn.dataset.target);
    btn.addEventListener('click', function () {
      var open = !target.hidden;
      target.hidden = open;
      btn.textContent = open ? 'Voir le tableau' : 'Masquer le tableau';
      btn.setAttribute('aria-expanded', String(!open));
    });
  });

  render();
})();
"""


MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre"]
MONTHS_FR_SHORT = ["janv.", "févr.", "mars", "avril", "mai", "juin", "juil.",
                   "août", "sept.", "oct.", "nov.", "déc."]
DAYS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def long_date(day: date) -> str:
    return f"{DAYS_FR[day.weekday()]} {day.day} {MONTHS_FR[day.month - 1]} {day.year}"


def short_date(day: date) -> str:
    return f"{day.day} {MONTHS_FR_SHORT[day.month - 1]}"


def esc(value) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_payload(a) -> dict:
    load = [dict(r, label=parse_day(r["date"]).strftime("%d/%m"),
                 dateLabel=short_date(parse_day(r["date"]))) for r in a["load"]]

    weekly = [dict(w, dateLabel=short_date(parse_day(w["weekStart"])),
                   timeLabel=fmt_duration(w["timeSec"])) for w in a["weekly"]]

    recovery = [dict(r, dateLabel=short_date(parse_day(r["date"]))) for r in a["recovery"]]

    horizon = parse_day(a["pace"]["horizon"])
    span = (a["today"] - horizon).days
    # Les graduations du nuage de points tombent tous les 14 jours : les
    # libelles doivent etre indexes exactement sur ces valeurs.
    axis = {offset: short_date(horizon + timedelta(days=offset))
            for offset in range(0, span + 1, 14)}

    points = [dict(p, kindLabel=CLASS_LABELS[p["kind"]],
                   dateLabel=short_date(parse_day(p["date"])))
              for p in a["pace"]["points"]]

    vo2 = [dict(v, label=short_date(parse_day(v["date"]))) for v in a["vo2History"]]

    return {
        "load": load,
        "weekly": weekly,
        "recovery": recovery,
        "pace": {"points": points, "span": span, "axis": axis},
        "easyCeiling": EASY_HR_CEILING,
        "vo2History": vo2,
    }


def table(headers, rows, left_cols=(1,)) -> str:
    head = "".join(
        f'<th class="l">{esc(h)}</th>' if i + 1 in left_cols else f"<th>{esc(h)}</th>"
        for i, h in enumerate(headers))
    body = []
    for row in rows:
        if isinstance(row, dict):
            cells, flag = row["cells"], row.get("flag", False)
        else:
            cells, flag = row, False
        tds = "".join(
            f'<td class="l">{c}</td>' if i + 1 in left_cols else f"<td>{c}</td>"
            for i, c in enumerate(cells))
        body.append(f'<tr class="flag">{tds}</tr>' if flag else f"<tr>{tds}</tr>")
    return (f'<div class="table-scroll"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def legend(items) -> str:
    lis = "".join(
        f'<li><span class="swatch {kind}" style="background:{color}"></span>{esc(label)}</li>'
        for label, color, kind in items)
    return f'<ul class="legend">{lis}</ul>'


def card_header(title, table_id) -> str:
    return (f"<header><h2>{esc(title)}</h2>"
            f'<button class="table-toggle" type="button" data-target="{table_id}" '
            f'aria-expanded="false">Voir le tableau</button></header>')


def render_page(a, source_label) -> str:
    payload = build_payload(a)
    load = a["load"]
    latest = load[-1] if load else {}
    four_weeks_ago = load[-29] if len(load) >= 29 else (load[0] if load else {})

    ctl = latest.get("ctl")
    ctl_delta = round(ctl - four_weeks_ago["ctl"], 1) if ctl and four_weeks_ago.get("ctl") else None
    tsb = latest.get("tsb")

    rec = a["recovery"]
    hrv_7 = mean(r["hrv"] for r in rec[-7:])
    hrv_28 = mean(r["hrv"] for r in rec[-28:])
    rhr_7 = mean(r["rhr"] for r in rec[-7:])
    rhr_28 = mean(r["rhr"] for r in rec[-28:])
    sleep_7 = mean(r["sleepHours"] for r in rec[-7:])
    baseline = next((r["hrvBaseline"] for r in reversed(rec) if r.get("hrvBaseline")), None)

    fitness = a["fitness"]
    pace = a["pace"]

    if tsb is None:
        tsb_note, tsb_cls = "—", ""
    elif tsb > 10:
        tsb_note, tsb_cls = "très frais — prêt à performer", "good"
    elif tsb > 3:
        tsb_note, tsb_cls = "frais", "good"
    elif tsb > -10:
        tsb_note, tsb_cls = "charge équilibrée", ""
    else:
        tsb_note, tsb_cls = "fatigue accumulée", "warn"

    def delta_note(now, before, unit, lower_is_better=False):
        if now is None or before is None:
            return "—", ""
        diff = now - before
        cls = ""
        if abs(diff) >= (1.5 if unit == "bpm" else 2):
            good = diff < 0 if lower_is_better else diff > 0
            cls = "good" if good else "bad"
        sign = "+" if diff >= 0 else "−"
        return f"{sign}{abs(diff):.1f} {unit} vs 4 sem.", cls

    hrv_note, hrv_cls = delta_note(hrv_7, hrv_28, "ms")
    rhr_note, rhr_cls = delta_note(rhr_7, rhr_28, "bpm", lower_is_better=True)

    slope = a["weeklySlope"]
    slope_note = ("tendance stable" if slope is None or abs(slope) < 0.3
                  else f"tendance {'+' if slope > 0 else '−'}{abs(slope):.1f} km/semaine")

    tiles = [
        ("VO₂ max", f"{fitness.get('vo2max', '—')}",
         f"niveau course {fitness.get('runningLevel', '—')}/100", ""),
        ("Forme (CTL)", f"{ctl:.0f}" if ctl else "—",
         (f"{'+' if ctl_delta >= 0 else '−'}{abs(ctl_delta):.1f} sur 4 sem."
          if ctl_delta is not None else "—"),
         "good" if (ctl_delta or 0) > 0 else ""),
        ("Fraîcheur (TSB)", f"{tsb:+.0f}" if tsb is not None else "—", tsb_note, tsb_cls),
        ("Volume 4 sem.", f"{a['km4w']:.0f}<small>km</small>",
         f"{a['runs4w']} sorties · {slope_note}", ""),
        ("FC au repos", f"{rhr_7:.0f}<small>bpm</small>" if rhr_7 else "—", rhr_note, rhr_cls),
        ("VFC nocturne", f"{hrv_7:.0f}<small>ms</small>" if hrv_7 else "—",
         hrv_note if baseline is None else f"base {baseline} ms · {hrv_note}", hrv_cls),
    ]
    tiles_html = "".join(
        f'<div class="tile"><div class="k">{k}</div><div class="v">{v}</div>'
        f'<div class="n {cls}">{esc(n)}</div></div>'
        for k, v, n, cls in tiles)

    # --- tableaux ---------------------------------------------------------
    load_rows = [{"cells": [short_date(parse_day(r["date"])), f"{r['ctl']:.1f}",
                            f"{r['atl']:.1f}", f"{r['tsb']:+.1f}",
                            "mesuré" if r["measured"] else "estimé"],
                  "flag": not r["measured"]}
                 for r in reversed(load[-42:])]
    load_table = table(["Date", "CTL", "ATL", "TSB", "Source"], load_rows)

    weekly_table = table(
        ["Semaine du", "Km", "Sorties", "Temps", "Tendance"],
        [{"cells": [short_date(parse_day(w["weekStart"])), f"{w['km']:.1f}", w["runs"],
                    fmt_duration(w["timeSec"]),
                    f"{w['trend']:.1f}" if w["trend"] is not None else "—"],
          "flag": w["partial"]} for w in reversed(a["weekly"])])

    rec_table = table(
        ["Date", "VFC", "Plage normale", "FC repos", "Sommeil", "Stress"],
        [[short_date(parse_day(r["date"])),
          f"{r['hrv']} ms" if r["hrv"] else "—",
          f"{r['hrvLow']}–{r['hrvHigh']} ms" if r.get("hrvLow") else "—",
          f"{r['rhr']} bpm" if r["rhr"] else "—",
          f"{r['sleepHours']:.1f} h" if r["sleepHours"] else "—",
          r["stress"] if r["stress"] is not None else "—"]
         for r in reversed(rec)])

    pace_table = table(
        ["Date", "Séance", "Type", "Km", "Allure", "FC moy."],
        [{"cells": [short_date(parse_day(p["date"])), esc(p["title"] or "—"),
                    CLASS_LABELS[p["kind"]], f"{p['km']:.2f}",
                    p["paceLabel"], f"{p['hr']} bpm" if p["hr"] else "—"],
          "flag": p["kind"] == "facile" and p["hr"] and p["hr"] > EASY_HR_CEILING}
         for p in reversed(pace["points"])], left_cols=(1, 2, 3))

    recent_table = table(
        ["Date", "Séance", "Type", "Distance", "Durée", "Allure", "FC moy."],
        [[short_date(parse_day(r["date"])), esc(r.get("title") or "—"), r["kindLabel"],
          f"{r['distanceKm']:.2f} km", r["durationLabel"], f"{r['paceLabel']} /km",
          f"{r['avgHr']} bpm" if r.get("avgHr") else "—"]
         for r in a["recent"]], left_cols=(1, 2, 3))

    preds = fitness.get("predictions") or {}
    pred_rows = []
    for key, name, pb_key in (("5k", "5 km", "5k"), ("10k", "10 km", "10k"),
                              ("half", "Semi-marathon", None),
                              ("marathon", "Marathon", None)):
        predicted = preds.get(key)
        pb = PERSONAL_BESTS.get(pb_key) if pb_key else None
        gap = "—"
        if predicted and pb:
            diff = predicted - pb
            gap = f"{'+' if diff >= 0 else '−'}{fmt_duration(abs(diff))}"
        pred_rows.append([name, fmt_duration(predicted) if predicted else "—",
                          fmt_duration(pb) if pb else "—", gap])
    pred_table = table(["Distance", "Prédiction COROS", "Record perso", "Écart"], pred_rows)

    # --- verdicts ---------------------------------------------------------
    too_hard = pace["tooHard"]
    if pace["easyCount"]:
        share = 100.0 * len(too_hard) / pace["easyCount"]
        if not too_hard:
            easy_verdict = (
                f"<strong>Les jours faciles sont vraiment faciles.</strong> "
                f"Les {pace['easyCount']} footings des {WEEKS_SHOWN} dernières semaines sont "
                f"tous restés sous {EASY_HR_CEILING} bpm "
                f"(FC moyenne {pace['easyAvgHr']} bpm, allure {fmt_pace(pace['easyAvgPace'])} /km).")
        else:
            shown = too_hard[-6:]
            prefix = "" if len(shown) == len(too_hard) else "les plus récents : "
            listing = ", ".join(f"{short_date(parse_day(p['date']))} ({p['hr']} bpm)"
                                for p in shown)
            last = parse_day(too_hard[-1]["date"])
            since = (a["today"] - last).days
            tail = (f" Aucun débordement depuis {since} jours."
                    if since >= 14 else
                    f" Le dernier date du {short_date(last)}.")
            easy_verdict = (
                f"<strong>{len(too_hard)} footing(s) sur {pace['easyCount']} "
                f"({share:.0f} %)</strong> ont dépassé {EASY_HR_CEILING} bpm — "
                f"{prefix}{listing}. FC moyenne des footings : "
                f"{pace['easyAvgHr']} bpm.{tail}")
        if pace["qualityAvgHr"]:
            easy_verdict += (
                f" Écart avec les séances qualité : "
                f"{pace['qualityAvgHr'] - pace['easyAvgHr']} bpm et "
                f"{fmt_pace(abs(pace['easyAvgPace'] - pace['qualityAvgPace']))} /km.")
    else:
        easy_verdict = "Pas encore assez de footings identifiés sur la fenêtre."

    estimated_days = sum(1 for r in load if not r["measured"])
    load_caption = (
        "CTL = forme de fond (moyenne exponentielle 42 j), ATL = fatigue récente (7 j), "
        "TSB = CTL − ATL, la fraîcheur du jour. ")
    if estimated_days:
        load_caption += (
            f"COROS ne publie sa charge que sur ~31 jours : les {estimated_days} premiers jours "
            "sont reconstitués à partir du TRIMP de toutes les séances — padel, vélo et tennis "
            "compris, ce qui explique le pic de début août — puis calés sur les valeurs COROS "
            "(trait pointillé, fond grisé).")
    return PAGE_TEMPLATE \
        .replace("__CSS__", PAGE_CSS) \
        .replace("__JS__", PAGE_JS) \
        .replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False)) \
        .replace("__RACE_NAME__", esc(RACE_NAME)) \
        .replace("__RACE_DATE__", esc(long_date(RACE_DATE))) \
        .replace("__DAYS_LEFT__", str(a["daysLeft"])) \
        .replace("__WEEKS_LEFT__", f"{a['weeksLeft']:.1f}".replace(".", ",")) \
        .replace("__TILES__", tiles_html) \
        .replace("__LOAD_CAPTION__", load_caption) \
        .replace("__LOAD_TABLE__", load_table) \
        .replace("__WEEKLY_TABLE__", weekly_table) \
        .replace("__REC_TABLE__", rec_table) \
        .replace("__PACE_TABLE__", pace_table) \
        .replace("__PACE_VERDICT__", easy_verdict) \
        .replace("__RECENT_TABLE__", recent_table) \
        .replace("__RECENT_COUNT__", str(len(a["recent"]))) \
        .replace("__PRED_TABLE__", pred_table) \
        .replace("__VO2__", str(fitness.get("vo2max", "—"))) \
        .replace("__THRESHOLD__", fmt_pace(fitness.get("thresholdPaceSecPerKm"))) \
        .replace("__VO2_HISTORY_NOTE__",
                 ("La courbe apparaîtra dès la deuxième exécution : COROS ne renvoie que la "
                  "valeur courante, le script archive donc une mesure par jour dans "
                  "<code>data/vo2max_history.json</code>."
                  if len(a["vo2History"]) < 2 else
                  f"{len(a['vo2History'])} relevés archivés localement depuis "
                  f"{short_date(parse_day(a['vo2History'][0]['date']))}.")) \
        .replace("__VO2_CHART__",
                 '<div class="chart-wrap short"><canvas id="vo2Chart"></canvas></div>'
                 if len(a["vo2History"]) >= 2 else "") \
        .replace("__SLEEP_AVG__", f"{sleep_7:.1f}".replace(".", ",") if sleep_7 else "—") \
        .replace("__HR_REST__", str(a["hrRest"])) \
        .replace("__SOURCE__", esc(source_label)) \
        .replace("__GENERATED__", esc(long_date(a["today"]))) \
        .replace("__GENERATED_TIME__", datetime.now().strftime("%H:%M"))


PAGE_TEMPLATE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__RACE_NAME__ — tableau de bord</title>
<style>__CSS__</style>
</head>
<body>
<div class="shell">

  <div class="masthead">
    <div>
      <h1>__RACE_NAME__</h1>
      <p class="race-date">__RACE_DATE__</p>
    </div>
    <div class="countdown">
      <span class="figure">__DAYS_LEFT__</span>
      <span class="unit">jours<br><span class="weeks">soit __WEEKS_LEFT__ semaines</span></span>
    </div>
    <div class="spacer"></div>
    <button class="theme-toggle" id="themeToggle" type="button">Thème sombre</button>
  </div>

  <div class="tiles">__TILES__</div>

  <section class="card">
    __HDR_LOAD__
    <p class="caption">__LOAD_CAPTION__</p>
    __LEG_LOAD__
    <div class="chart-wrap"><canvas id="loadChart"></canvas></div>
    <div id="loadTable" hidden>__LOAD_TABLE__</div>
  </section>

  <section class="card">
    __HDR_WEEKLY__
    <p class="caption">Kilométrage course à pied par semaine (lundi–dimanche) sur 12 semaines.
       La droite de tendance est ajustée sur les semaines complètes uniquement ; la semaine en
       cours, forcément partielle, est tramée en clair et exclue du calcul.</p>
    __LEG_WEEKLY__
    <div class="chart-wrap"><canvas id="weeklyChart"></canvas></div>
    <div id="weeklyTable" hidden>__WEEKLY_TABLE__</div>
  </section>

  <section class="card">
    __HDR_REC__
    <p class="caption">Six dernières semaines. Chaque graphique porte sa propre échelle :
       la valeur du jour et sa moyenne glissante sur 7 jours, qui est le signal à suivre.
       Sommeil moyen sur 7 jours : __SLEEP_AVG__ h · FC de repos médiane : __HR_REST__ bpm.</p>
    __LEG_REC__
    <div class="triptych">
      <div>
        <h3>VFC nocturne</h3>
        <p class="sub">Bande grise : plage normale calculée par COROS</p>
        <div class="chart-wrap short"><canvas id="hrvChart"></canvas></div>
      </div>
      <div>
        <h3>FC au repos</h3>
        <p class="sub">Une dérive vers le haut précède souvent la fatigue</p>
        <div class="chart-wrap short"><canvas id="rhrChart"></canvas></div>
      </div>
      <div>
        <h3>Sommeil</h3>
        <p class="sub">Durée totale par nuit, datée au réveil</p>
        <div class="chart-wrap short"><canvas id="sleepChart"></canvas></div>
      </div>
    </div>
    <div id="recTable" hidden>__REC_TABLE__</div>
  </section>

  <section class="card">
    __HDR_PACE__
    <p class="caption">Le type de séance est déduit du nom de la séance et de sa distance ;
       la FC moyenne, elle, vient de la montre. Un footing dont la FC dépasse le plafond n'est
       pas resté facile, quel qu'ait été le plan.</p>
    <p class="verdict">__PACE_VERDICT__</p>
    __LEG_PACE__
    <div class="split">
      <div>
        <h3 style="margin:0 0 2px;font-size:13px;">FC moyenne par sortie</h3>
        <p class="sub" style="margin:0 0 10px;font-size:12px;color:var(--ink-muted);">
           Plafond « facile » à __EASY_CEILING__ bpm</p>
        <div class="chart-wrap short"><canvas id="paceHrChart"></canvas></div>
      </div>
      <div>
        <h3 style="margin:0 0 2px;font-size:13px;">Allure moyenne par sortie</h3>
        <p class="sub" style="margin:0 0 10px;font-size:12px;color:var(--ink-muted);">
           Plus haut = plus rapide</p>
        <div class="chart-wrap short"><canvas id="paceChart"></canvas></div>
      </div>
    </div>
    <div id="paceTable" hidden>__PACE_TABLE__</div>
  </section>

  <section class="card">
    __HDR_VO2__
    <p class="caption">VO₂ max actuel : <strong>__VO2__</strong> ml/kg/min ·
       allure au seuil <strong>__THRESHOLD__ /km</strong>. __VO2_HISTORY_NOTE__</p>
    __VO2_CHART__
    <div id="vo2Table">__PRED_TABLE__</div>
    <p class="note">Écart = prédiction COROS moins record personnel. Les prédictions sur semi
       et marathon n'ont pas de record de référence déclaré.</p>
  </section>

  <section class="card">
    <header><h2>Sorties récentes</h2></header>
    <p class="caption">Les __RECENT_COUNT__ sorties course à pied des 4 dernières semaines.</p>
    __RECENT_TABLE__
  </section>

  <p class="footer">
    Généré le __GENERATED__ à __GENERATED_TIME__ · source : __SOURCE__<br>
    Relancer <code>python dashboard.py</code> pour actualiser.
  </p>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>window.__DATA__ = __PAYLOAD__;</script>
<script>__JS__</script>
</body>
</html>
"""

PAGE_TEMPLATE = (PAGE_TEMPLATE
    .replace("__HDR_LOAD__", card_header("Charge d'entraînement", "loadTable"))
    .replace("__HDR_WEEKLY__", card_header("Kilométrage hebdomadaire", "weeklyTable"))
    .replace("__HDR_REC__", card_header("Récupération", "recTable"))
    .replace("__HDR_PACE__", card_header("Les jours faciles sont-ils faciles ?", "paceTable"))
    .replace("__HDR_VO2__", "<header><h2>VO₂ max et projections</h2></header>")
    .replace("__LEG_LOAD__", legend([
        ("Forme (CTL)", "var(--series-1)", "line"),
        ("Fatigue (ATL)", "var(--series-2)", "line"),
        ("Fraîcheur (TSB)", "var(--series-3)", "line"),
        ("Pointillé + fond grisé : reconstitué", "var(--ink-muted)", "line")]))
    .replace("__LEG_WEEKLY__", legend([
        ("Kilométrage", "var(--series-1)", ""),
        ("Semaine en cours (partielle)", "color-mix(in srgb, var(--series-1) 32%, transparent)", ""),
        ("Tendance 12 semaines", "var(--series-2)", "line")]))
    .replace("__LEG_REC__", legend([
        ("Valeur du jour", "var(--series-1)", "line"),
        ("Moyenne glissante 7 jours", "var(--series-2)", "line")]))
    .replace("__LEG_PACE__", legend([
        ("Footing facile", "var(--series-1)", ""),
        ("Séance qualité", "var(--series-2)", ""),
        ("Sortie longue", "var(--series-3)", "")]))
    .replace("__EASY_CEILING__", str(EASY_HR_CEILING)))


# --------------------------------------------------------------------------
# Point d'entree
# --------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-fetch", action="store_true",
                        help="ne pas appeler l'API, reconstruire depuis l'instantane")
    parser.add_argument("--status", action="store_true",
                        help="afficher l'etat des sources de donnees et sortir")
    parser.add_argument("--out", default=OUTPUT_PATH, help="chemin du fichier HTML produit")
    args = parser.parse_args(argv)

    if args.status:
        try:
            _, origin = find_token()
            print(f"jeton COROS      : trouve ({origin})")
        except TokenNotFound as exc:
            print(f"jeton COROS      : absent — {exc}")
        if os.path.exists(SNAPSHOT_PATH):
            snap = load_snapshot()
            print(f"instantane local : {SNAPSHOT_PATH}")
            print(f"                   capture le {snap.get('fetchedAt', '?')}, "
                  f"{len(snap.get('activities', []))} activites, "
                  f"{len(snap.get('daily', []))} jours de sante")
        else:
            print(f"instantane local : absent ({SNAPSHOT_PATH})")
        return 0

    print(f"Tableau de bord {RACE_NAME} — {RACE_DATE.isoformat()}")
    snapshot, source_label = acquire_data(allow_fetch=not args.no_fetch)
    analysis = analyse(snapshot, date.today())
    html = render_page(analysis, source_label)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    print(f"  · {len(analysis['load'])} jours de charge, "
          f"{len(analysis['weekly'])} semaines, "
          f"{len(analysis['recent'])} sorties recentes")
    print(f"  · page ecrite : {args.out}")
    print(f"  · J-{analysis['daysLeft']} avant {RACE_NAME}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
