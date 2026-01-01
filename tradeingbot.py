# tradingbot.py – Capital.com Tick → Candle → Demo-Signal

import os
import json
import requests
import asyncio
import websockets
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from collections import deque
from colorama import Fore, Style, init
from chart_gui import ChartManager

# Alle externen Timestamps kommen als UTC ms und werden ausschließlich via to_local_dt() benutzt.
charts = ChartManager(window_size_sec=300)
init(autoreset=True)

# Ringpuffer für Tick-Daten (mid)
TICK_RING_MAXLEN = 60000   # z.B. ~120–600 Minuten bei 100–500 Ticks/min
TICK_RING = {}             # { epic: deque([(ts_ms:int, mid:float)], maxlen=...) }

_last_dirlog_sec = {}
_last_ticklog_sec = {}   # epic -> last logged second (int)
_last_close_ts = {}
CLOSE_COOLDOWN_SEC = 2

# Zugangsdaten aus Umgebungsvariablen oder direkt hier eintragen
API_KEY  = os.getenv("CAPITAL_API_KEY") or "l8HA4NGKyCXoVUXJ"
USERNAME = os.getenv("CAPITAL_USERNAME") or "carsten.schoettke@gmx.de"
PWD      = os.getenv("CAPITAL_PASSWORD") or "G8ZdGJHN7VB9vJy_"

# API Adressen
BASE_STREAM = "wss://api-streaming-capital.backend-capital.com/connect"
# Basis-URLs LIVE
#BASE_REST   = "https://api-capital.backend-capital.com"
#ACCOUNT  = os.getenv("CAPITAL_ACCOUNT_TYPE", "live")  # "demo" oder "live"
# Basis-URLs DEMO
BASE_REST   = "https://demo-api-capital.backend-capital.com"
ACCOUNT  = os.getenv("CAPITAL_ACCOUNT_TYPE", "demo")

# Instrumente
#INSTRUMENTS = ["BTCUSD", "ETHUSD", "XRPUSD"]
INSTRUMENTS = ["ETHUSD"]

# Lokalzeit
LOCAL_TZ = ZoneInfo("Europe/Berlin")

CST, XSEC = None, None

# ==============================
# CONFIG ping
# ==============================
PING_INTERVAL    = 15   # Sekunden zwischen WebSocket-Pings
RECONNECT_DELAY  = 3    # Sekunden warten nach Verbindungsabbruch
RECV_TIMEOUT     = 60   # Sekunden Timeout fürs Warten auf eine Nachricht

# ==============================
# STRATEGIE-EINSTELLUNGEN
# ==============================

EMA_FAST = 10 # 5 #9   # kurze EMA-Periode (z. B. 9, 10, 20)
EMA_SLOW = 18 # 11 #21  # lange EMA-Periode (z. B. 21, 30, 50)

TRADE_RISK_PCT = 0.0025  # 2% vom verfügbaren Kapital pro Trade
MANUAL_TRADE_SIZE = 0.3 # ETHUSD 0.3 ~1000€, XRPUSD 400 ~1000€, BTCUSD 0.01 ~1000€
USE_HMA = True  # Wenn False → klassische EMA, wenn True → Hull MA

# ==============================
# SIGNALFILTER – Entry-Feinjustage
# ==============================

# Maximal zulässige Entfernung zwischen Kurs und schnellem MA in Einheiten
# des aktuellen Spreads.
#
# Interpretation:
#   distance = abs(last_close - ma_fast)
#   max_distance = spread * SIGNAL_MAX_PRICE_DISTANCE_SPREADS
#
# Nur wenn distance <= max_distance ist, wird ein Trend-Signal (BUY/SELL)
# überhaupt in Betracht gezogen. Liegt der Kurs weiter weg, wird das Signal
# als "überdehnt" auf HOLD gesetzt.
#
# Wirkung:
#   0.5–1.0  → sehr streng: nur Einstiege nahe am Trendband (MA)
#   1.0–2.0  → moderat: schützt vor späten Einstiegen nach großen Moves
#   3.0–4.0  → locker: nur extreme Überdehnung wird geblockt
#   100.0    → praktisch deaktiviert (aktueller Debug-Modus: "alles traden")
SIGNAL_MAX_PRICE_DISTANCE_SPREADS = 4.0

# Momentum-Toleranz für Trend-Signale:
# Gibt an, wie stark das aktuelle Momentum gegenüber der vorherigen Kerze
# nachlassen darf, bevor ein BUY/SELL-Signal verworfen wird.
#
# Beispiel:
#   SIGNAL_MOMENTUM_TOLERANCE = 0.2
#   → momentum_now muss mindestens 20 % von momentum_prev erreichen,
#     sonst wird das Signal als "Momentum schwach" auf HOLD gesetzt.
#
# Wirkung:
#   - kleiner Wert (0.1–0.3): nur "frische" Trends werden gehandelt,
#     Signale nach Momentum-Einbruch werden ignoriert.
#   - großer Wert (1.0): Filter praktisch deaktiviert.
SIGNAL_MOMENTUM_TOLERANCE = 2.0

TRADE_BARRIER = 2 # ur 2, Wert * spread zwischen zwei aufeinanderfolgenden Candle-Closes, ab dem Trade zugelassen wird

# ==============================
# Risk Management Parameter
# ==============================
# ETHUSD/ETHEUR
STOP_LOSS_PCT             = 0.0030 # fester Stop-Loss
TRAILING_STOP_PCT         = 0.0050 # Trailing Stop
TRAILING_SET_CALM_DOWN    = 0.5000 # Filter für Trailing-Nachzie-Schwelle (spread*TRAILING_SET_CALM_DOWN)
TAKE_PROFIT_PCT           = 0.0060 # z. B. 0,2% Gewinnziel
BREAK_EVEN_STOP_PCT       = 0.0045 # sicherung der Null-Schwelle / kein Verlust mehr möglich
BREAK_EVEN_BUFFER_PCT     = 0.0002 # Puffer über BREAK_EVEN_STOP, ab dem der BE auf BREAK_EVEN_STOP gesetzt wird

# XRPUSD
# STOP_LOSS_PCT           = 0.015   # fester Stop-Loss
# TRAILING_STOP_PCT       = 0.007   # Trailing Stop
# TRAILING_SET_CALM_DOWN  = 0.0    # Filter für Trailing-Nachzie-Schwelle (spread*TRAILING_SET_CALM_DOWN)
# TAKE_PROFIT_PCT         = 0.015  # z. B. 0,2% Gewinnziel
# BREAK_EVEN_STOP_PCT     = 0.0015 # sicherung der Null-Schwelle / kein Verlust mehr möglich
# BREAK_EVEN_BUFFER_PCT   = 0.0015 # Puffer über BREAK_EVEN_STOP, ab dem der BE auf BREAK_EVEN_STOP gesetzt wird

# BTCUSD
# STOP_LOSS_PCT           = 0.0015    # fester Stop-Loss
# TRAILING_STOP_PCT       = 0.0007    # Trailing Stop
# TRAILING_SET_CALM_DOWN  = 0.0       # Filter für Trailing-Nachzie-Schwelle (spread*TRAILING_SET_CALM_DOWN)
# TAKE_PROFIT_PCT         = 0.0030    # z. B. 0,2% Gewinnziel
# BREAK_EVEN_STOP_PCT     = 0.0001    # sicherung der Null-Schwelle / kein Verlust mehr möglich
# BREAK_EVEN_BUFFER_PCT   = 0.0001    # Puffer über BREAK_EVEN_STOP, ab dem der BE auf BREAK_EVEN_STOP gesetzt wird


# ==============================
# PARAMETER CSV (Reload) – 2 Trigger: Startup + nach Close
# ==============================

PARAMETER_CSV = os.path.join(os.path.dirname(__file__), "parameter.csv")

# Welche Variablen dürfen aus parameter.csv überschrieben werden?
# (Liste bewusst explizit, damit nicht aus Versehen API_KEYS etc. überschrieben werden.)
_PARAM_KEYS = [
    "USE_HMA",
    "EMA_FAST",
    "EMA_SLOW",
    "SIGNAL_MAX_PRICE_DISTANCE_SPREADS",
    "SIGNAL_MOMENTUM_TOLERANCE",
    "STOP_LOSS_PCT",
    "TRAILING_STOP_PCT",
    "TAKE_PROFIT_PCT",
    "BREAK_EVEN_STOP_PCT",
    "BREAK_EVEN_BUFFER_PCT",
    "TRAILING_SET_CALM_DOWN",
    "TRADE_RISK_PCT",
    "MANUAL_TRADE_SIZE",
]

# Merker für "nur loggen, wenn sich wirklich was geändert hat"
_PARAM_LAST_APPLIED = None  # dict | None


def _cast_like_existing(key: str, raw_value: str):
    """Castet raw_value grob auf den Typ der existierenden Global-Variable (ohne Plausibilitätschecks)."""
    if key not in globals():
        return raw_value

    base = globals()[key]

    # Bool ist Unterklasse von int -> Bool zuerst prüfen
    if isinstance(base, bool):
        v = raw_value.strip().lower()
        if v in ("1", "true", "yes", "y", "on"):
            return True
        if v in ("0", "false", "no", "n", "off"):
            return False
        # Wenn es knallt, knallt es (oder wird vom outer try/except abgefangen)
        raise ValueError(f"Bool erwartet für {key}, got: {raw_value!r}")

    if isinstance(base, int):
        return int(raw_value.strip())

    if isinstance(base, float):
        # DE-Notation tolerieren (Komma → Punkt), ohne weitere Checks
        return float(raw_value.strip().replace(",", "."))

    # Fallback: als String
    return raw_value.strip()


def load_parameters(trigger: str) -> bool:
    """
    Lädt parameter.csv (selber Ordner wie Script), 'letzte Zeile gewinnt',
    und überschreibt nur bekannte _PARAM_KEYS.
    Logging: genau 1 Zeile, aber nur wenn sich effektiv etwas geändert hat.

    Return:
      True  -> Parameter wurden geändert und angewendet
      False -> keine Änderung (oder Datei fehlt/fehlerhaft -> bestehende Werte bleiben)
    """
    global _PARAM_LAST_APPLIED

    path = PARAMETER_CSV

    # Snapshot der aktuellen Werte (damit "keine Änderung" sauber erkannt wird)
    current = {k: globals().get(k) for k in _PARAM_KEYS if k in globals()}

    if not os.path.isfile(path):
        # Startup: Defaults bleiben, Laufzeit: bestehende bleiben
        print(f"⚠️ PARAM: {os.path.basename(path)} fehlt ({trigger}) → bestehende/Default-Parameter bleiben aktiv")
        return False

    updated = dict(current)

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if ";" not in line:
                    raise ValueError(f"Ungültige Zeile (kein ';'): {raw!r}")

                key, value = [p.strip() for p in line.split(";", 1)]

                # optional: Header-Zeile ignorieren
                if key.lower() in ("key", "param", "parameter") and value.lower() in ("value", "wert"):
                    continue

                if key not in updated:
                    # unbekannte Keys ignorieren (kein Crash durch Tippfehler)
                    continue

                updated[key] = _cast_like_existing(key, value)

    except Exception as e:
        # Gemäß deinem Failure-Mode-Wunsch (früher): bei kaputter Datei NICHT umschalten
        print(f"⚠️ PARAM: {os.path.basename(path)} unlesbar/kaputt ({trigger}) → keine Änderung. Grund: {e}")
        return False

    # Effektive Änderungen ermitteln (gegen "current", damit erneutes Einlesen nicht spammt)
    changes = [(k, current.get(k), updated.get(k)) for k in updated.keys() if current.get(k) != updated.get(k)]
    if not changes:
        print(f"ℹ️ PARAM gelesen ({trigger}) → keine Änderungen")
        return False

    # Anwenden (global überschreiben)
    for k, _old, new in changes:
        globals()[k] = new

    # Logging: genau eine Zeile
    msg = "; ".join([f"{k} {old}→{new}" for k, old, new in changes])
    print(f"🧩 PARAM geändert ({trigger}): {msg}")

    _PARAM_LAST_APPLIED = {k: globals().get(k) for k in updated.keys()}
    return True



def to_local_dt(ms_since_epoch: int) -> datetime:
    return datetime.fromtimestamp(ms_since_epoch/1000, tz=timezone.utc).astimezone(LOCAL_TZ)

def utc_now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)

# Candle-Historie für EMA-Berechnung
candle_history = {epic: deque(maxlen=200) for epic in INSTRUMENTS}

# Merker: pro Instrument zuletzt ausgegebene Sekunde
last_printed_sec = {epic: None for epic in INSTRUMENTS}

# ==============================
# TRADE berechnen aufgrund von verfügbarem Kontostand und %-davon
# ==============================

def calc_trade_size(CST, XSEC, epic, risk_pct=TRADE_RISK_PCT):
    # # 1. Kontostand abrufen
    # url_acc = f"{BASE_REST}/api/v1/accounts"
    # headers = {
    #     "X-CAP-API-KEY": API_KEY,
    #     "CST": CST,
    #     "X-SECURITY-TOKEN": XSEC,
    #     "Accept": "application/json"
    # }
    # r_acc = requests.get(url_acc, headers=headers)
    # if r_acc.status_code != 200:
    #     print("⚠️ Fehler beim Abrufen des Kontostands:", r_acc.status_code, r_acc.text)
    #     return 1
    # acc_data = r_acc.json()
    # available = float(acc_data.get("availableToDeal", 0))
    # risk_amount = available * risk_pct

    # # 2. Instrument-Infos abrufen
    # url_mkt = f"{BASE_REST}/api/v1/markets/{epic}"
    # r_mkt = requests.get(url_mkt, headers=headers)
    # if r_mkt.status_code != 200:
    #     print("⚠️ Fehler beim Abrufen der Marktdaten:", r_mkt.status_code, r_mkt.text)
    #     return 1
    # mkt_data = r_mkt.json().get("instrument", {})
    # contract_size = float(mkt_data.get("contractSize", 1))
    # margin_factor = float(mkt_data.get("marginFactor", 1)) / 100  # kommt in %

    # # 3. Stück berechnen
    # # -> angenommener Kurs: letzter Preis aus Market-Details
    # snapshot = r_mkt.json().get("snapshot", {})
    # price = float(snapshot.get("bid", 1))
    # margin_per_unit = price * contract_size * margin_factor
    # if margin_per_unit <= 0:
    #     return 1
    # size = risk_amount / margin_per_unit

    # print(f"📊 calc_trade_size Debug → risk_amount={risk_amount}, margin_per_unit={margin_per_unit}, "
    #   f"raw_size={risk_amount / margin_per_unit}, size_rounded={round(size, 3)}, "
    #   f"minDealSize={mkt_data.get('minDealSize')}, lotSize={mkt_data.get('lotSize')}")

    # ETHUSD 0.3 ~1000€, XRPUSD 400 ~1000€
    size = MANUAL_TRADE_SIZE # test mit hartem wert, da im demo konto anscheinend kein kontostand übermittelt wird ...
    return round(size, 3)  # 3 Nachkommastellen, also 0.001 genau

# ==============================
# LOGIN (entspricht Zelle B)
# ==============================

def capital_login():
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {
        "identifier": USERNAME,
        "password": PWD,
        "encryptedPassword": False
    }
    r = requests.post(f"{BASE_REST}/api/v1/session", headers=headers, json=payload)
    print("Login HTTP:", r.status_code)
    CST  = r.headers.get("CST")
    XSEC = r.headers.get("X-SECURITY-TOKEN")
    print("CST vorhanden?", bool(CST), "XSEC vorhanden?", bool(XSEC))
    return CST, XSEC

# ==============================
# POSITIONS-MANAGER mit Auto-ReLogin + Robustheit
# ==============================

def get_positions(CST, XSEC, retry=True):
    #Alle offenen Positionen abfragen.
    url = f"{BASE_REST}/api/v1/positions"
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC,
        "Accept": "application/json"
    }
    r = requests.get(url, headers=headers)
    print(f"🧩 [DEBUG REST-Check] HTTP {r.status_code} → {r.text[:200]}") # debug 22.10.2025
    if r.status_code == 401 and retry:
        print("🔑 Session abgelaufen → erneuter Login (get_positions) ...")
        new_CST, new_XSEC = capital_login()
        return get_positions(new_CST, new_XSEC, retry=False)

    if r.status_code != 200:
        print("⚠️ Fehler beim Abrufen der Positionen:", r.status_code, r.text)
        return []

    try:
        data = r.json()
    except Exception:
        return []

    positions = data.get("positions", [])
    if not isinstance(positions, list):
        return []

    return positions


def open_position(CST, XSEC, epic, direction, size, entry_price, retry=True):
    # Neue Position eröffnen (Market-Order), liefert Response-Objekt zurück.
    url = f"{BASE_REST}/api/v1/positions"
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC,
        "Content-Type": "application/json"
    }
    data = {
        "epic": epic,
        "direction": direction,
        "size": size,
        "orderType": "MARKET",
        "guaranteedStop": False
    }

    r = requests.post(url, headers=headers, json=data)

    if r.status_code == 401 and retry:
        print("🔑 Session abgelaufen → erneuter Login (open_position) ...")
        new_CST, new_XSEC = capital_login()
        CST, XSEC = new_CST, new_XSEC  # global aktualisieren
        raise RuntimeError("force_reconnect")

    print("📩 Order-Response:", r.status_code, r.text)
    print(f"🧩 [DEBUG] Vor Confirm: open_positions[{epic}] = {open_positions.get(epic)}") # debug 22.10.2025

    if r.status_code == 200:
        try:
            ref = r.json().get("dealReference")
            if ref:
                conf_url = f"{BASE_REST}/api/v1/confirms/{ref}"
                conf = requests.get(conf_url, headers=headers)
                if conf.status_code == 200:
                    conf_data = conf.json()
                    deal_id = None

                    affected = conf_data.get("affectedDeals")
                    if affected and isinstance(affected, list) and affected:
                        deal_id = affected[0].get("dealId")

                    if not deal_id and conf_data.get("dealId"):
                        deal_id = conf_data.get("dealId")

                    if deal_id:
                        # 1) Optional: Fill-Preis aus Confirm bevorzugen (falls vorhanden)
                        fill_price = None
                        try:
                            fill_price = conf_data.get("level") or conf_data.get("price")
                            if not fill_price:
                                affected = conf_data.get("affectedDeals")
                                if isinstance(affected, list) and affected:
                                    fill_price = affected[0].get("level") or affected[0].get("price")
                            fill_price = float(fill_price) if fill_price is not None else None
                        except Exception:
                            fill_price = None

                        # 2) Entry write-once: Confirm-Fill > übergebener Seitenpreis
                        final_entry = fill_price if isinstance(fill_price, (int, float)) else entry_price

                        # 3) Write-once speichern (falls schon vorhanden, nicht überschreiben)
                        prev = open_positions.get(epic)
                        if not isinstance(prev, dict) or prev.get("entry_price") is None:
                            open_positions[epic] = {
                                "direction": direction,
                                "dealId": deal_id,
                                "entry_price": final_entry,
                                "size": size,                 # <-- reale Stückzahl mitschreiben
                                "trailing_stop": None
                            }
                        else:
                            # nur Metadaten aktualisieren, Entry/Size unangetastet lassen
                            open_positions[epic].update({
                                "direction": direction,
                                "dealId": deal_id
                            })

                        print(f"🆕 [{epic}] Open erfolgreich → {direction} "
                            f"(dealId={open_positions[epic].get('dealId')}, entry={open_positions[epic].get('entry_price')})")

                    else:
                        print(f"⚠️ Keine dealId aus Confirm extrahiert für {epic}")
        except Exception as e:
            print("⚠️ Confirm-Check fehlgeschlagen:", e)
    return r


def close_position(CST, XSEC, epic, deal_id=None, retry=True):
    # Offene Position schließen über DELETE /positions/{dealId}
    if not deal_id:
        print(f"⚠️ close_position: kein dealId übergeben für {epic}")
        return None

    deal_id = str(deal_id)  # API erwartet string
    url = f"{BASE_REST}/api/v1/positions/{deal_id}"
    headers = {
        "X-CAP-API-KEY": API_KEY,
        "CST": CST,
        "X-SECURITY-TOKEN": XSEC,
        "Accept": "application/json"
    }

    print(f"🔎 Versuche Close mit DELETE {url} ...")
    r = requests.delete(url, headers=headers)

    if r is None:
        print("⚠️ Close-Request hat keine Antwort geliefert!")
        return None

    if r.status_code == 401 and retry:
        print("🔑 Session abgelaufen → erneuter Login (close_position) ...")
        new_CST, new_XSEC = capital_login()
        CST, XSEC = new_CST, new_XSEC  # global aktualisieren
        raise RuntimeError("force_reconnect")

    print(f"📩 Close-Response: {r.status_code} {r.text}")
    return r


# ==============================
# SIGNAL-LOGIK (Zelle D)
# ==============================

def on_candle_forming(epic, bar, ts_ms):
    # Wird bei jedem Tick innerhalb einer Kerze aufgerufen (noch nicht geschlossen).
    # Verwende Mid-Preis für die laufende Candle (technische Analyse)
    close_bid = bar.get("close_bid")
    close_ask = bar.get("close_ask")
    mid_price = (close_bid + close_ask) / 2 if close_bid and close_ask else None
    closes = list(candle_history[epic]) + [mid_price]

    # Ringpuffer füttern (Mid über close_bid/close_ask des aktuellen Ticks)
    if mid_price is not None:
        dq = TICK_RING.setdefault(epic, deque(maxlen=TICK_RING_MAXLEN))
        dq.append((int(ts_ms), float(mid_price)))

    # 🔧 Spread auf Basis echter Marktseiten (Ask–Bid)
    high_ask = bar.get("high_ask")
    low_bid = bar.get("low_bid")

    if high_ask is not None and low_bid is not None:
        spread = (close_ask - close_bid) if (close_ask is not None and close_bid is not None) else 0.0
    else:
        spread = 0.0

    trend = evaluate_trend_signal(epic, closes, spread)

    # Zeit konvertieren
    local_dt = to_local_dt(ts_ms)
    local_time = local_dt.strftime("%d.%m.%Y %H:%M:%S %Z")

    # Nur letzten Tick pro Sekunde ausgeben
    sec_key = local_dt.replace(microsecond=0)
    if last_printed_sec[epic] == sec_key:
        return
    last_printed_sec[epic] = sec_key
   
    open_bid = bar.get("open_bid")
    open_ask = bar.get("open_ask")
    close_bid = bar.get("close_bid")
    close_ask = bar.get("close_ask")

    mid_open = (open_bid + open_ask) / 2 if open_bid and open_ask else None
    mid_close = (close_bid + close_ask) / 2 if close_bid and close_ask else None

    # Hinweis:
    # Diese BUY/SELL-Anzeige basiert auf Mid-Preisen (Durchschnitt aus Bid/Ask)
    # Sie dient nur der Visualisierung / Trendanzeige, nicht der Handelsentscheidung.
    if mid_close > mid_open:
        instant = "BUY ✅"
    elif mid_close < mid_open:
        instant = "SELL ⛔"
    else:
        instant = "NEUTRAL ⚪"
  
    # Offene Position abrufen für terminal ausgabe
    pos = open_positions.get(epic)
    sl = tp = ts = None
    entry = None

    if isinstance(pos, dict):
        entry = pos.get("entry_price")
        direction = pos.get("direction")
        stop = pos.get("trailing_stop")

        if entry and direction == "BUY":
            sl = entry * (1 - STOP_LOSS_PCT)
            # tp = None
            tp = entry * (1 + TAKE_PROFIT_PCT) # testweise kommentiert 19.10.2025
        elif entry and direction == "SELL":
            sl = entry * (1 + STOP_LOSS_PCT)
            # tp = None
            tp = entry * (1 - TAKE_PROFIT_PCT) # testweise kommentiert 19.10.2025

        ts = stop  # aktueller Trailing-Stop (falls gesetzt)

    sl_str = f"{sl:.2f}" if isinstance(sl, (int, float)) else "-"
    ts_str = f"{ts:.2f}" if isinstance(ts, (int, float)) else "-"
    tp_str = f"{tp:.2f}" if isinstance(tp, (int, float)) else "-"

    # 🧾 Konsistente Ausgabe mit Bid/Ask-Werten
    open_bid = bar.get("open_bid")
    open_ask = bar.get("open_ask")
    close_bid = bar.get("close_bid")
    close_ask = bar.get("close_ask")

    # Midpreise nur für visuelle Ausgabe berechnen
    mid_open = (open_bid + open_ask) / 2 if open_bid and open_ask else None
    mid_close = (close_bid + close_ask) / 2 if close_bid and close_ask else None

    if mid_open and mid_close:
        print(
            f"[{epic}] {local_time} - "
            f"O:{mid_open:.2f} C:{mid_close:.2f} (tks:{bar['ticks']}) → {instant} | Trend: {trend} "
            f"- sl={sl_str} ts={ts_str} tp={tp_str}"
        )

    else:
        print(
            f"[{epic}] {local_time} - "
            f"O:{open_ask:.2f}/{open_bid:.2f}  C:{close_ask:.2f}/{close_bid:.2f} "
            f"(tks:{bar['ticks']}) → {instant} | Trend: {trend}"
        )

    # Hook🧩 Chart aktualisieren – nur gültige Marktseitendaten übergeben
    charts.update(
    epic,
    ts_ms,
    {
        "open_bid": bar.get("open_bid"),
        "open_ask": bar.get("open_ask"),
        "high_bid": bar.get("high_bid"),
        "low_bid": bar.get("low_bid"),
        "high_ask": bar.get("high_ask"),
        "low_ask": bar.get("low_ask"),
        "close_bid": bar.get("close_bid"),
        "close_ask": bar.get("close_ask"),
        "ticks": bar.get("ticks", 0)
    },
    open_positions.get(epic, {}),
    entry=entry, sl=sl, tp=tp, ts=ts,
    trend=trend   # 🧭 Trend-String mitgeben für Pfeil im Titel
)

# ==============================
# Horizontalität berechnen (0-1)
# ==============================

def directionality_factor(epic: str, window_sec: int = 180, min_samples: int = 40) -> float:
    # Vertikalitäts-Faktor ∈ [0, 1] für ein Instrument.
    #   0.0 = horizontal / seitwärts
    #   1.0 = starker Trend
    #  -1.0 = kein Buffer / keine Datenzu / wenig Daten → Sentinel
    dq = TICK_RING.get(epic)
    if not dq:
        return -1.0  # kein Buffer / keine Daten → Sentinel

    newest_ts = dq[-1][0]
    cut_ts = newest_ts - int(window_sec * 1000)

    # Von hinten sammeln, None-Werte filtern, deque NICHT verändern
    seg_prices_rev = []
    for ts, mid in reversed(dq):
        if ts < cut_ts:
            break
        if mid is not None:
            seg_prices_rev.append(float(mid))

    if len(seg_prices_rev) < min_samples:
        return -1.0  # zu wenig Daten → Sentinel

    prices = list(reversed(seg_prices_rev))

    # Trend/Chop-Heuristik
    diffs = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    chop = sum(abs(d) for d in diffs)
    if chop <= 0:
        return 0.0  # komplett flach → kein Trend

    trend = abs(sum(diffs))
    v = trend / chop  # 0..1

    # clamp
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)



def on_candle_close(epic, bar):
    # Wird bei Abschluss jeder 1m-Kerze aufgerufen.

    # === 1️⃣ Mid-Preis nur für technische Indikatoren (EMA/HMA)
    #       Hinweis: Wird ausschließlich für gleitende Durchschnitte verwendet,
    #       nicht für Handetake_profit_level = entry * (1 - (TAKE_PROFIT_PCT + spread_pct))sentscheidungen.
    close_bid = bar.get("close_bid")
    close_ask = bar.get("close_ask")
    # Mid-Preis aus Bid / Ask (keine Fallbacks mehr erforderlich)
    if close_bid is not None and close_ask is not None:
        mid_price = (close_bid + close_ask) / 2.0
    else:
        mid_price = None


    candle_history[epic].append(mid_price)

    # === 2️⃣ Spread berechnen (reale Marktspanne) ===
    spread = (bar.get("close_ask") - bar.get("close_bid")) if (bar.get("close_ask") is not None and bar.get("close_bid") is not None) else 0.0

    # === 3️⃣ Handelssignal auswerten ===
    signal = evaluate_trend_signal(epic, list(candle_history[epic]), spread)

    print(
        f"📊 Trend-Signal [{epic}] — "
        f"O:{bar.get('open_ask', 0):.2f}/{bar.get('open_bid', 0):.2f} "
        f"C:{bar.get('close_ask', 0):.2f}/{bar.get('close_bid', 0):.2f} "
        f"→ {signal}"
    )

    # === 4️⃣ Marktseitig korrekten Entry-Preis bestimmen ===
    if signal.startswith("BEREIT: BUY"):
        entry_price = close_ask   # BUY zum Ask-Preis
    elif signal.startswith("BEREIT: SELL"):
        entry_price = close_bid   # SELL zum Bid-Preis
    else:
        entry_price = mid_price   # kein Trade → Mid-Preis als Dummy

    # 🧩 PARAM Reload pro Candle-Close – aber nur wenn kein Trade offen ist
    pos = open_positions.get(epic)
    in_trade = isinstance(pos, dict) and pos.get("direction") and pos.get("entry_price") is not None
    if not in_trade:
        load_parameters(f"before_decision:{epic}")

    decide_and_trade(CST, XSEC, epic, signal, entry_price)

    # === 5️⃣ Nur mit ausreichender Historie EMA/HMA berechnen ===
    closes = [v for v in candle_history[epic] if v is not None]
    if len(closes) >= EMA_SLOW:
        pos = open_positions.get(epic, {})
        entry = pos.get("entry_price") if isinstance(pos, dict) else None
        direction = pos.get("direction") if isinstance(pos, dict) else None
        stop = pos.get("trailing_stop") if isinstance(pos, dict) else None

        # Berechnung Stop/TP
        if entry and direction == "BUY":
            sl = entry * (1 - STOP_LOSS_PCT)
            # tp = None
            tp = entry * (1 + TAKE_PROFIT_PCT) # testweise kommentiert 19.10.2025
        elif entry and direction == "SELL":
            sl = entry * (1 + STOP_LOSS_PCT)
            # tp = None
            tp = entry * (1 - TAKE_PROFIT_PCT) # testweise kommentiert 19.10.2025
        else:
            sl = tp = None

        ts = stop
        be = pos.get("break_even_level") if isinstance(pos, dict) else None

        # === 6️⃣ Chart-Update mit neuen Bid/Ask-Werten ===
        charts.update(
            epic,
            bar.get("timestamp") or int(datetime.now(timezone.utc).timestamp() * 1000),
            {
                "open_bid": bar.get("open_bid"),
                "open_ask": bar.get("open_ask"),
                "high_bid": bar.get("high_bid"),
                "low_bid": bar.get("low_bid"),
                "high_ask": bar.get("high_ask"),
                "low_ask": bar.get("low_ask"),
                "close_bid": bar.get("close_bid"),
                "close_ask": bar.get("close_ask"),
                "ticks": bar.get("ticks", 0),
                "sl": sl,
                "tp": tp,
                "ts": ts,
                "be": be,
            },
            open_positions.get(epic, {}),
            ema_fast=ema(closes, EMA_FAST),
            ema_slow=ema(closes, EMA_SLOW),
            hma_fast=hma(closes, EMA_FAST),
            hma_slow=hma(closes, EMA_SLOW),
        )

    else:
        print(f"[Chart Hook {epic}] Noch zu wenige Kerzen für EMA/HMA ({len(closes)}/{EMA_SLOW})")

# ==============================
# EMA BERECHNUNG
# ==============================

def ema(values, period: int):
    #Einfache EMA-Berechnung auf einer Liste von Werten.
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = values[0]
    for v in values[1:]:
        ema_val = v * k + ema_val * (1 - k)
    return ema_val

# ==============================
# WMA & HMA BERECHNUNG
# ==============================

def wma(values, period: int):
    # Weighted Moving Average
    if len(values) < period:
        return None
    weights = list(range(1, period + 1))
    return sum(v * w for v, w in zip(values[-period:], weights)) / sum(weights)

def hma(values, period: int):
    # Hull Moving Average
    if len(values) < period:
        return None

    half_len = period // 2
    sqrt_len = int(period ** 0.5)

    # Serie der "raw"-Werte
    raw_series = []
    for i in range(period, len(values) + 1):
        segment = values[i - period:i]
        wma_half = wma(segment, half_len)
        wma_full = wma(segment, period)
        if wma_half is not None and wma_full is not None:
            raw_series.append(2 * wma_half - wma_full)

    if len(raw_series) < sqrt_len:
        return None

    # finale Glättung
    return wma(raw_series, sqrt_len)


# ==============================
#  TRADE-SIGNAL mit EMA / HMA
# ==============================
# Bewertet Trendrichtung und Signalstärke anhand gleitender Durchschnitte.
# Kombination aus EMA- und HMA-Varianten für unterschiedliche Glättung.
# Enthält Filter zur Vermeidung überdehnter oder träger Trends.

def evaluate_trend_signal(epic, closes, spread):
    # ------------------------------
    #  1️⃣ Berechnung der gleitenden Mittelwerte
    # Immer beide berechnen
    # ------------------------------
    ema_fast = ema(closes, EMA_FAST)
    ema_slow = ema(closes, EMA_SLOW)
    hma_fast = hma(closes, EMA_FAST)
    hma_slow = hma(closes, EMA_SLOW)

    # Auswahl, ob HMA oder EMA aktiv verwendet wird
    if USE_HMA:
        ma_fast, ma_slow, ma_type = hma_fast, hma_slow, "HMA"
    else:
        ma_fast, ma_slow, ma_type = ema_fast, ema_slow, "EMA"

    # Wenn noch nicht genug Kerzen vorhanden → kein valides Signal
    if ma_fast is None or ma_slow is None:
        return f"HOLD (zu wenig Daten: {len(closes)}/{EMA_SLOW})"

    last_close = closes[-1]
    prev_close = closes[-2]

    # ======================================================
    #  2️⃣ ENTRY-FILTER: Vermeide späte oder schwache Signale
    # ======================================================

    # --- Preis-vs-MA-Filter: Verhindert Einstiege bei überdehnten Bewegungen / wenn der Kurs zu weit vom MA entfernt ist
    #
    # Ziel:
    # Kein Entry, wenn der aktuelle Kurs (last_close) zu weit
    # vom kurzfristigen gleitenden Durchschnitt (ma_fast) entfernt liegt.
    #
    # Hintergrund:
    # - Wenn der Kurs stark über oder unter dem MA liegt,
    #   befindet sich der Markt meist am "Wellenkamm" oder "Boden".
    # - In solchen Phasen kommt es häufig zu kurzfristigen Gegenbewegungen (Pullbacks).
    # - Der Filter soll daher nur Einstiege erlauben,
    #   solange der Kurs sich noch in vertretbarer Nähe zum Trendmittelwert bewegt.
    #
    # Berechnung:
    # distance = absolute Abweichung zwischen Kurs und MA
    # max_distance = zulässige maximale Abweichung, proportional zur aktuellen Spanne (spread)
    #
    # Ist die Abweichung größer als max_distance → kein Einstieg.
    #
    # Hinweis:
    # Der Faktor ist aktuell extrem hoch (100), um den Filter faktisch zu deaktivieren.
    # Realistisch wäre z. B. 1.0–2.0 für einen wirksamen Schutz vor Spät-Entries.
    # distance misst, wie weit der Kurs vom gleitenden Durchschnitt entfernt ist.
    # max_distance ist die erlaubte maximale Abweichung.
    # Wenn der Kurs weiter weg ist als max_distance, wird kein Trade gemacht („überdehnt“).
    # 1.0	sehr vorsichtig	nur Entries nah am MA erlaubt
    # 2.0	moderat	kleine Überdehnungen noch erlaubt
    # 4.0	locker	Kurs darf deutlich vom MA entfernt sein
    # 100	praktisch deaktiviert	Kursabstand spielt keine Rolle
    distance = abs(last_close - ma_fast)
    max_distance = spread * SIGNAL_MAX_PRICE_DISTANCE_SPREADS   # 8 Faktor anpassbar (1.0–2.0 typisch)

    if distance > max_distance:
        now_ms = int((time.time() * 1000) % 1000)  # Millisekunden-Anteil der Sekunde
        if 980 <= now_ms <= 999:
            print(f"[{epic}] Preis zu weit vom {ma_type} entfernt "
                f"(dist={distance:.5f}) → kein Entry")
        return f"HOLD (überdehnt, {ma_type})"

    # --- Momentum-Filter: prüft Beschleunigung der Kursbewegung
    # Wenn der gleitende Durchschnitt (MA) einen Trend anzeigt,
    # soll die aktuelle Preisbewegung (momentum_now) diesen Trend bestätigen.
    # Nur handeln, wenn aktueller MA sich schneller bewegt als zuvor / wenn aktuelle Bewegung zunimmt
    # → Annäherung über Differenz zweier aufeinanderfolgender Closes

    # momentum_now  = letzte Preisänderung (aktueller Impuls)
    # momentum_prev = vorherige Preisänderung (vorheriger Impuls)
    momentum_now = last_close - prev_close
    momentum_prev = prev_close - closes[-3]

    # Idee:
    # - Bei steigendem Trend (ma_fast > ma_slow):
    #     momentum_now sollte >= momentum_prev sein.
    #     Wenn momentum_now deutlich kleiner ist, flacht der Trend ab → kein Entry.
    #
    # - Bei fallendem Trend (ma_fast < ma_slow):
    #     momentum_now sollte <= momentum_prev sein.
    #     Wenn momentum_now deutlich größer ist, verliert der Abwärtstrend an Stärke → kein Entry.
    #
    # Die Faktoren (hier *-100 / *100) sind testweise extrem groß gewählt,
    # um den Filter faktisch zu deaktivieren (ursprünglich 0.1 = 10 % Schwächungstoleranz).
    # Mit realistischen Faktoren (z. B. 0.1 oder 0.2) reagiert der Filter sensibler
    # und unterdrückt Einstiege, wenn der Trend an Schwung verliert.
    # 0.05	Momentum_now < 5 % von Momentum_prev → sehr empfindlich	kaum Trades, sehr vorsichtig
    # 0.1	Momentum_now < 10 % → moderat	mittlere Tradefreudigkeit
    # 0.3	Momentum_now < 30 % → tolerant	häufiger Trades
    # 1.0	Momentum_now < 100 % → praktisch deaktiviert	fast jeder Trend erlaubt
    if ma_fast > ma_slow and momentum_now < momentum_prev * SIGNAL_MOMENTUM_TOLERANCE : # 0.1
        # print(f"[{epic}] LONG-Momentum schwächer → kein BUY")
        return f"HOLD (Momentum schwach, {ma_type})"

    if ma_fast < ma_slow and momentum_now > momentum_prev * SIGNAL_MOMENTUM_TOLERANCE : # 0.1
        # print(f"[{epic}] SHORT-Momentum schwächer → kein SELL")
        return f"HOLD (Momentum schwach, {ma_type})"

    # ======================================================
    #  3️⃣ SIGNAL-LOGIK (Kaufsignal / Verkaufssignal)
    # ======================================================

    # Signal-Logik (wie bisher, nur basierend auf aktivem MA-Typ)
    # Trend-Logik: Fast > Slow → Aufwärtstrend → BUY
    # Ein Trade wird nur dann als „BEREIT: BUY/SELL“ markiert, wenn die Änderung
    # zwischen zwei aufeinanderfolgenden Candle-Closes größer ist als 2×Spread:
    if ma_fast > ma_slow and (last_close - prev_close) > TRADE_BARRIER * spread:
        return f"BEREIT: BUY ✅ ({ma_type})"
    # Umgekehrt: Fast < Slow → Abwärtstrend → SELL
    elif ma_fast < ma_slow and (prev_close - last_close) > TRADE_BARRIER * spread:
        return f"BEREIT: SELL ⛔ ({ma_type})"
    # Kein klares Signal
    else:
        return f"UNSICHER ⚪ ({ma_type})"

# ==============================
# Hilfsfunktionen für robustes Open/Close
# ==============================

def safe_close(CST, XSEC, epic, deal_id=None):
    # Wrapper: Close-Order robust mit Retry und Reset in open_positions.
    # Holt sich dealId und Richtung aus open_positions oder notfalls via get_positions().

    direction = None
    full_position = None

    if epic in open_positions and isinstance(open_positions[epic], dict):
        direction = open_positions[epic].get("direction")
        if not deal_id:
            deal_id = open_positions[epic].get("dealId")

    # Fallback: API direkt fragen
    if not deal_id or not direction:
        positions = get_positions(CST, XSEC)
        if positions:
            for pos in positions:
                position = pos.get("position")
                if position and position.get("epic") == epic:
                    full_position = pos  # komplette Rohdaten merken
                    deal_id = position.get("dealId")
                    direction = position.get("direction")
                    print(f"🔎 safe_close Fallback get_positions({epic}) → dealId={deal_id}, direction={direction}")
                    break

    # Debug: komplette Positionsdaten dumpen
    if full_position:
        try:
            print("📋 Vollständige Positionsdaten:", json.dumps(full_position, indent=2))
        except Exception as e:
            print("⚠️ Dump der Positionsdaten fehlgeschlagen:", e)

    # Wenn immer noch nichts → Notlösung
    if not direction:
        direction = "SELL"

    # Gegenseite nur fürs Log bestimmen
    close_dir = "SELL" if direction == "BUY" else "BUY"

    print(f"📊 [{epic}] Versuche Close (dealId={deal_id}, position={direction} → close_dir={close_dir}) ...")

    # Close-Request starten (API erwartet dealId als string)
    if deal_id is not None:
        deal_id = str(deal_id)

    r = close_position(CST, XSEC, epic, deal_id=deal_id)
    ok = (r is not None and (
        r.status_code == 200 or
        (r.status_code == 404 and "not-found.dealId" in getattr(r, "text", ""))
    ))
    if r is not None and r.status_code == 404:
        print(f"ℹ️ [{epic}] Close 404 not-found.dealId → Position gilt als bereits geschlossen (idempotent).")

    if ok:
        open_positions[epic] = None
        print(f"✅ [{epic}] Close erfolgreich → open_positions reset")

        # Zusatz: nachprüfen, ob die Position wirklich weg ist
        try:
            positions = get_positions(CST, XSEC)
            ids = [p["position"]["dealId"] for p in positions if "position" in p]
            if deal_id and deal_id in ids:
                print(f"⚠️ [{epic}] Deal {deal_id} taucht nach Close noch in get_positions() auf!")
            else:
                print(f"✅ [{epic}] Deal {deal_id} ist aus get_positions() verschwunden.")
        except Exception as e:
            print(f"⚠️ [{epic}] Abgleich nach Close fehlgeschlagen:", e)

        load_parameters(f"after_close:{epic}")

    else:
        print(f"⚠️ [{epic}] Close fehlgeschlagen (dealId={deal_id})")

    return ok


def safe_open(CST, XSEC, epic, direction, size, entry_price):
    # Wrapper: Open-Order robust mit Retry + Ergänzen von Trailing Stop
    global open_positions

    r = open_position(CST, XSEC, epic, direction, size, entry_price)
    ok = (r is not None and r.status_code == 200)

    if ok and isinstance(open_positions.get(epic), dict):
        # Trailing Stop initial setzen
        if direction == "BUY":
            trailing_stop = entry_price * (1 - TRAILING_STOP_PCT)
        else:  # SELL
            trailing_stop = entry_price * (1 + TRAILING_STOP_PCT)

        # Nur Trailing Stop ergänzen
        open_positions[epic]["trailing_stop"] = trailing_stop
        print(f"🆕 [{epic}] Open erfolgreich → {direction} "
              f"(dealId={open_positions[epic].get('dealId')}, entry={entry_price}, trailing={trailing_stop})")

    return ok


# ==============================
# STOP LOSS & TRAILING STOP überwachen
# ==============================

def check_protection_rules(epic, bid, ask, spread, CST, XSEC):
    # Überwacht Stop-Loss, Take-Profit, Trailing-Stop und Break-Even.
    # Verwendet echte Marktseiten:
    #     - BUY  → Trigger = Bid (Verkaufsseite)
    #     - SELL → Trigger = Ask (Kaufseite)
    
    global open_positions
    pos = open_positions.get(epic)
    if not isinstance(pos, dict):
        return

    direction = pos.get("direction")
    deal_id   = pos.get("dealId")
    entry     = pos.get("entry_price")
    stop      = pos.get("trailing_stop")

    if not (direction and entry and bid is not None and ask is not None):
        return

    # --- Debounced Close helper (verhindert Mehrfach-Calls in kurzer Zeit)
    def _debounced_close():
        now = time.monotonic()
        if now - _last_close_ts.get(epic, 0.0) < CLOSE_COOLDOWN_SEC:
            return
        _last_close_ts[epic] = now
        safe_close(CST, XSEC, epic, deal_id=deal_id)

    # Spread in Prozent der Entry-Basis
    spread_pct = spread / entry
    price = bid if direction == "BUY" else ask

    # 🔇 Throttle: nur ca. 1×/Sek. loggen – wenn die Tick-Millis im Fenster 950–999 liegen
    ts_for_log = pos.get("last_tick_ms") or int(time.time() * 1000)  # falls kein Tick-Zeitstempel vorhanden
    now_sec = int(time.time())
    
    if _last_dirlog_sec.get(epic) != now_sec:
        print(f"🧭 [{epic}] directionality(60s) = {directionality_factor(epic):.2f}")
        _last_dirlog_sec[epic] = now_sec

    # === LONG ===
    if direction == "BUY":
        stop_loss_level = entry * (1 - STOP_LOSS_PCT)
        take_profit_level = entry * (1 + TAKE_PROFIT_PCT)

        # 🧭 Break-Even-Logik (mit Buffer)
        # Wird erst aktiviert, wenn Bid über Entry × (1 + BREAK_EVEN_STOP_PCT + BREAK_EVEN_BUFFER_PCT) liegt.
        if price >= entry * (1 + BREAK_EVEN_STOP_PCT + BREAK_EVEN_BUFFER_PCT):
            be_stop = entry * (1 + BREAK_EVEN_STOP_PCT)
            if stop is None or stop < be_stop:
                pos["trailing_stop"] = be_stop
                pos["break_even_active"] = True
                pos["break_even_level"] = be_stop
                print(f"🔒 [{epic}] Break-Even aktiviert bei {price:.2f} auf {be_stop:.2f}")


        # 🔧 Trailing-Stop nachziehen (nur bei echtem Fortschritt)
        if price > entry:
            new_trailing = price * (1 - TRAILING_STOP_PCT)

            # Nur aktualisieren, wenn der Kurs neue Hochs (LONG) bzw. Tiefs (SHORT) erreicht
            if stop is None:
                pos["trailing_stop"] = new_trailing
                print(f"🔧 [{epic}] Initialer Trailing Stop gesetzt: {new_trailing:.2f}")
            elif new_trailing > stop + (spread * TRAILING_SET_CALM_DOWN):
                pos["trailing_stop"] = new_trailing
                print(f"🔧 [{epic}] Trailing Stop nachgezogen auf {new_trailing:.2f}")

        # 🛡️ Break-Even-Schutz prüfen
        if pos.get("break_even_active") and "break_even_level" in pos:
            be = pos["break_even_level"]
            if stop is not None and pos["trailing_stop"] < be:
                pos["trailing_stop"] = be
                print(f"🛡️ [{epic}] Trailing-Stop angehoben (Break-Even aktiv)")

        # Stops prüfen
        if price <= stop_loss_level or (stop is not None and price <= stop):
            print(f"⛔ [{epic}] Stop ausgelöst (Bid={price:.2f}) → schließe LONG")
            _debounced_close()
        elif price >= take_profit_level:
            print(f"✅ [{epic}] Take-Profit erreicht (Bid={price:.2f}) → schließe LONG")
            _debounced_close()

    # === SHORT ===
    elif direction == "SELL":
        stop_loss_level = entry * (1 + STOP_LOSS_PCT)
        take_profit_level = entry * (1 - TAKE_PROFIT_PCT )

        # 🧭 Break-Even-Logik (mit Buffer)
        # Wird erst aktiviert, wenn Ask unter Entry × (1 − (BREAK_EVEN_STOP_PCT + BREAK_EVEN_BUFFER_PCT)) fällt.
        if price <= entry * (1 - (BREAK_EVEN_STOP_PCT + BREAK_EVEN_BUFFER_PCT)):
            be_stop = entry * (1 - BREAK_EVEN_STOP_PCT)
            if stop is None or stop > be_stop:
                pos["trailing_stop"] = be_stop
                pos["break_even_active"] = True
                pos["break_even_level"] = be_stop
                print(f"🔒 [{epic}] Break-Even aktiviert bei {price:.2f} auf {be_stop:.2f}")


        # 🔧 Trailing-Stop nachziehen (nur bei echtem Fortschritt)
        if price < entry:
            new_trailing = price * (1 + TRAILING_STOP_PCT)

            if stop is None:
                pos["trailing_stop"] = new_trailing
                print(f"🔧 [{epic}] Initialer Trailing Stop gesetzt: {new_trailing:.2f}")
            elif new_trailing < stop - (spread * TRAILING_SET_CALM_DOWN):
                pos["trailing_stop"] = new_trailing
                print(f"🔧 [{epic}] Trailing Stop nachgezogen auf {new_trailing:.2f}")

        # 🛡️ Break-Even-Schutz prüfen
        if pos.get("break_even_active") and "break_even_level" in pos:
            be = pos["break_even_level"]
            if stop is not None and pos["trailing_stop"] > be:
                pos["trailing_stop"] = be
                print(f"🛡️ [{epic}] Trailing-Stop gesenkt (Break-Even aktiv)")

        # Stops prüfen
        if price >= stop_loss_level or (stop is not None and price >= stop):
            print(f"⛔ [{epic}] Stop ausgelöst (Ask={price:.2f}) → schließe SHORT")
            _debounced_close()
        elif price <= take_profit_level:
            print(f"✅ [{epic}] Take-Profit erreicht (Ask={price:.2f}) → schließe SHORT")
            _debounced_close()


# ==============================
# DECISION-MANAGER (mit Schutz + Farben)
# ==============================

# Farben (ANSI-Codes)
RESET  = "\033[0m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"

open_positions = {epic: None for epic in INSTRUMENTS}  # Merker: None | "BUY" | "SELL"

def decide_and_trade(CST, XSEC, epic, signal, current_price):
    # Entscheidet basierend auf Signal + aktueller Position mit Schutz-Logik + Farben.
    global open_positions

    pos = open_positions.get(epic)
    current = pos.get("direction") if isinstance(pos, dict) else None
    deal_id = pos.get("dealId") if isinstance(pos, dict) else None

    # ===========================
    # LONG-SIGNAL
    # ===========================
    if signal.startswith("BEREIT: BUY"):
        if current == "BUY":
            print(Fore.GREEN + f"⚖️ [{epic}] Bereits LONG, nichts tun.")
        elif current == "SELL":
            # Flip unterdrückt → nur Info ausgeben
            print(Fore.YELLOW + f"🔒 [{epic}] Flip SELL→BUY ignoriert, SHORT bleibt offen.")
        elif current is None:
            print(f"{Fore.YELLOW}🚀 [{epic}] Long eröffnen{Style.RESET_ALL}")

            # ✅ Marktseitig korrekter Entry wird übergeben (Ask bei BUY)
            safe_open(CST, XSEC, epic, "BUY", calc_trade_size(CST, XSEC, epic), current_price)


    # ===========================
    # SHORT-SIGNAL
    # ===========================
    elif signal.startswith("BEREIT: SELL"):
        if current == "SELL":
            print(f"{Fore.RED}⚖️ [{epic}] Bereits SHORT, nichts tun. → {signal}{Style.RESET_ALL}")
        elif current == "BUY":
            # Flip unterdrückt → nur Info ausgeben
            print(Fore.YELLOW + f"🔒 [{epic}] Flip BUY→SELL ignoriert, LONG bleibt offen.")
        elif current is None:
            print(f"{Fore.YELLOW}🚀 [{epic}] Short eröffnen{Style.RESET_ALL}")

            # ✅ Marktseitig korrekter Entry wird übergeben (Bid bei SELL)
            safe_open(CST, XSEC, epic, "SELL", calc_trade_size(CST, XSEC, epic), current_price)



    # ===========================
    # KEIN KLARES SIGNAL
    # ===========================
    else:
        if current == "BUY":
            print(f"{Fore.GREEN}🤔 [{epic}] LONG offen → Signal = {signal}{Style.RESET_ALL}")
        elif current == "SELL":
            print(f"{Fore.RED}🤔 [{epic}] SHORT offen → Signal = {signal}{Style.RESET_ALL}")
        else:
            print(f"{Fore.YELLOW}🤔 [{epic}] Kein Trade offen → Signal = {signal}{Style.RESET_ALL}")


# ==============================
# CANDLE-AGGREGATOR (Zelle C)
# ==============================

def local_minute_floor(ts_ms: int) -> datetime:
    dt_local = to_local_dt(ts_ms)
    return dt_local.replace(second=0, microsecond=0)

async def run_candle_aggregator_per_instrument():
    global CST, XSEC

    invalid_token_streak = 0  # 🧩 Zähler für aufeinanderfolgende Tokenfehler

    while True:  # Endlosschleife mit Reconnect & Token-Refresh
        if not CST or not XSEC:
            try:
                CST, XSEC = capital_login()
                invalid_token_streak = 0  # Reset nach erfolgreichem Login
            except requests.exceptions.RequestException as e:
                print(f"❌ Login fehlgeschlagen: {e}\n⏳ {RECONNECT_DELAY}s warten und erneut versuchen …")
                await asyncio.sleep(RECONNECT_DELAY)
                continue  # zurück an den Schleifenanfang, ohne zu crashen

        ws_url = f"{BASE_STREAM}?CST={CST}&X-SECURITY-TOKEN={XSEC}"
        subscribe = {
            "destination": "marketData.subscribe",
            "correlationId": "candles",
            "cst": CST,
            "securityToken": XSEC,
            "payload": {"epics": INSTRUMENTS},
        }

        states = {epic: {"minute": None, "bar": None} for epic in INSTRUMENTS}

        print("🔌 Verbinde:", ws_url)
        await asyncio.sleep(RECONNECT_DELAY)  # 🧭 kleiner Cooldown vor Neuverbindung, vermeidet Hektik bei Reconnects
        try:
            async with websockets.connect(ws_url, ping_interval=None) as ws:
                await ws.send(json.dumps(subscribe))
                print("✅ Subscribed:", INSTRUMENTS)

                # 🧭 Nach Reconnect: offene Positionen mit Server abgleichen
                try:
                    print(f"🧩 [DEBUG REST-Check] Tokens → CST: {bool(CST)}, XSEC: {bool(XSEC)}")

                    # 🕒 Kurze Pause nach Login, damit Capital-Server neue Tokens intern synchronisiert
                    await asyncio.sleep(RECONNECT_DELAY)

                    positions = get_positions(CST, XSEC)

                    # 🧠 Schutz: Wenn Server noch keine Daten liefert (z. B. direkt nach Token-Refresh)
                    if not positions or not isinstance(positions, list):
                        print("🕒 Server liefert keine Positionsdaten (wahrscheinlich frischer Token) – überspringe diesen Check einmalig.")
                        await asyncio.sleep(RECONNECT_DELAY)
                    else:
                        print(f"🧩 [DEBUG REST-Check] get_positions() Rückgabe: {type(positions)} / Länge: {len(positions)}")

                        active_epics = [p["market"]["epic"] for p in positions if p.get("position")]
                        for epic in list(open_positions.keys()):
                            if epic not in active_epics:
                                print(f"⚠️ {epic}: laut Server keine offene Position mehr → lokal schließen")
                                open_positions[epic] = None

                except Exception as e:
                    msg = str(e).lower()

                    # 🧩 Tokenfehler: Session ist ungültig
                    if "invalid.session.token" in msg or "error.invalid.session.token" in msg:
                        invalid_token_streak += 1
                        print(f"⚠️ Ungültiges Token (Versuch {invalid_token_streak}) → warte 5 Sekunden ...")

                        # 🧠 Wenn zu viele Fehlversuche, Tokens hart zurücksetzen
                        if invalid_token_streak >= 5:
                            print("🚨 Zu viele Tokenfehler hintereinander → Session vollständig neu aufbauen.")
                            CST = None
                            XSEC = None
                            invalid_token_streak = 0
                            await asyncio.sleep(RECONNECT_DELAY)
                        else:
                            await asyncio.sleep(RECONNECT_DELAY)
                        continue

                    # 🧩 Allgemeiner Fehler
                    else:
                        print(f"⚠️ Positionsabgleich nach Reconnect fehlgeschlagen: {e}")
                        invalid_token_streak = 0
                        await asyncio.sleep(RECONNECT_DELAY)

                last_ping = time.time()

                while True:
                    now = time.time()

                    # --- alle PING_INTERVAL Sekunden ein Ping ---
                    if now - last_ping > PING_INTERVAL:
                        try:
                            await ws.ping()
                            # print("📡 Ping gesendet")
                            last_ping = now

                            # 💓 REST-Session aktiv halten (Ping)
                            try:
                                requests.get(
                                    f"{BASE_REST}/api/v1/ping",
                                    headers={"CST": CST, "X-SECURITY-TOKEN": XSEC},
                                    timeout=5
                                )
                            except Exception as e:
                                print(f"⚠️ REST-Ping fehlgeschlagen: {e}")

                        except Exception as e:
                            print("⚠️ Ping fehlgeschlagen:", e)
                            break

                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
                        msg = json.loads(raw)
                    except asyncio.TimeoutError:
                        print("⚠️ Timeout → reconnect ...")
                        break
                    except Exception as e:
                        print("⚠️ Fehler beim Empfangen:", e)
                        if "invalid.session.token" in str(e).lower() or "force_reconnect" in str(e).lower():
                            CST, XSEC = None, None

                        # 🔧 Falls Verbindung unerwartet endet → Socket explizit schließen
                        try:
                            if "ws" in locals() and ws.open:
                                await ws.close()
                                print("🔌 WebSocket sauber geschlossen (nach Fehler)")
                        except Exception:
                            pass

                        break


                    # # 🧩 Debug: Zeige jede empfangene WebSocket-Nachricht (Rohdaten)
                    # if "payload" in msg:
                    #     try:
                    #         epic = msg["payload"].get("epic", "N/A")
                    #         print(f"\n📡 RAW MESSAGE [{epic}] → destination={msg.get('destination')}")
                    #         print(json.dumps(msg["payload"], indent=2))
                    #     except Exception as e:
                    #         print("⚠️ Debug-Dump fehlgeschlagen:", e)
                    
                    if msg.get("destination") != "quote":
                        continue

                    p = msg.get("payload", {})
                    epic = p.get("epic")
                    if not epic or epic not in states:
                        continue

                    # --- Parse Tick-Felder robust ---
                    try:
                        bid   = float(p["bid"])
                        ask   = float(p["ofr"])
                        ts_ms = int(p["timestamp"])
                        # if 980 <= (ts_ms % 1000) <= 999: # aktuelle tick zeit in local ausgebe
                        #     print(f"[SK1 tick] ts_ms={ts_ms}  local={to_local_dt(ts_ms).strftime('%H:%M:%S.%f')[:-3]}")

                    except Exception:
                        continue

                    # --- Live-PnL nur im Tickpfad berechnen ---
                    pos = open_positions.get(epic)
                    if isinstance(pos, dict) and pos.get("direction") and pos.get("entry_price") is not None:
                        entry = float(pos["entry_price"])
                        qty   = float(pos.get("size") or MANUAL_TRADE_SIZE)

                        if pos["direction"] == "BUY":
                            mark = bid              # LONG → Bewertung am Bid
                            pnl  = (mark - entry) * qty
                        else:  # SELL
                            mark = ask              # SHORT → Bewertung am Ask
                            pnl  = (entry - mark) * qty

                        # In-place aktualisieren: Chart liest nur noch diese Felder
                        pos["mark_price"]     = mark
                        pos["unrealized_pnl"] = pnl
                        pos["last_tick_ms"]   = ts_ms

                    # ticks in datei schreiben
                    filename = f"ticks_{epic}.csv"
                    try:
                        # Position offen? -> volle Tickauflösung beibehalten
                        in_trade = isinstance(pos, dict) and pos.get("direction") and pos.get("entry_price") is not None

                        # Optional: letzter 1s jeder Minute auch voll loggen (für Candle-Close-Fidelity)
                        full_log = in_trade or ((ts_ms % 60000) >= 59000)

                        full_log = True  # TEMP: alle Ticks loggen

                        do_write = False
                        if full_log:
                            do_write = True
                        else:
                            sec = ts_ms // 1000
                            last_sec = _last_ticklog_sec.get(epic)
                            if last_sec != sec:
                                _last_ticklog_sec[epic] = sec
                                do_write = True

                        if do_write:
                            with open(filename, "a", encoding="utf-8", newline="") as f:
                                f.write(f"{ts_ms};{bid};{ask}\n")

                    except Exception as e:
                        print(f"⚠️ Tick-Log-Fehler {epic}: {e}")
                    # datei ende

                    mid_price = (bid + ask) / 2.0
                    spread = ask - bid
                    minute_key = local_minute_floor(ts_ms)
                    st = states[epic]

                    # Hook: 🧩 Live-Chart-Update auf Tick-Ebene
                    if st.get("bar") is not None:
                        #print(f"[DEBUG Chart-Hook] {epic} | bid={bid:.2f} ask={ask:.2f} ts={ts_ms}")
                        charts.update(
                            epic,
                            ts_ms,
                            {
                                "bid": bid,
                                "ask": ask,
                                "open_bid": st["bar"]["open_bid"],
                                "open_ask": st["bar"]["open_ask"],
                                "high_bid": st["bar"]["high_bid"],
                                "high_ask": st["bar"]["high_ask"],
                                "low_bid": st["bar"]["low_bid"],
                                "low_ask": st["bar"]["low_ask"],
                                "close_bid": bid,
                                "close_ask": ask,
                                "ticks": st["bar"]["ticks"],
                            },
                            open_positions.get(epic, {})
                        )

                    # 🕒 Candle-Handling mit echten Marktseiten (Bid/Ask)
                    if st["minute"] is not None and minute_key > st["minute"] and st["bar"] is not None:
                        bar = st["bar"]

                        # Letzte Werte der alten Minute übernehmen
                        bar["close_bid"] = bid
                        bar["close_ask"] = ask

                        print(
                            f"\n✅ [{epic}] Closed 1m  {st['minute'].strftime('%d.%m.%Y %H:%M:%S %Z')}  "
                            f"O:{bar['open_ask']:.2f}/{bar['open_bid']:.2f}  "
                            f"H:{bar['high_ask']:.2f}/{bar['high_bid']:.2f}  "
                            f"L:{bar['low_ask']:.2f}/{bar['low_bid']:.2f}  "
                            f"C:{bar['close_ask']:.2f}/{bar['close_bid']:.2f}  "
                            f"tks:{bar['ticks']}"
                        )

                        # Candle schließen
                        bar_to_close = st["bar"].copy()          # ← Kopie, keine spätere Nebenwirkung
                        bar_to_close.setdefault("timestamp", ts_ms)

                        if 980 <= (ts_ms % 1000) <= 999:
                            print(f"[SK3 close] minute={st['minute'].strftime('%H:%M:%S')}  use_ts_ms={ts_ms}  bar_ts={bar_to_close.get('timestamp')}")

                        on_candle_close(epic, bar_to_close)

                        # Neue Minute starten
                        st["minute"] = minute_key
                        st["bar"] = {
                            "open_bid": bid, "open_ask": ask,
                            "high_bid": bid, "low_bid": bid,
                            "high_ask": ask, "low_ask": ask,
                            "close_bid": bid, "close_ask": ask,
                            "ticks": 1,
                            "timestamp": ts_ms
                        }

                    else:
                        # Neue Candle starten, falls noch keine existiert
                        if st["minute"] is None:
                            st["minute"] = minute_key
                            st["bar"] = {
                                "open_bid": bid, "open_ask": ask,
                                "high_bid": bid, "low_bid": bid,
                                "high_ask": ask, "low_ask": ask,
                                "close_bid": bid, "close_ask": ask,
                                "ticks": 1,
                                "timestamp": ts_ms
                            }
                        else:
                            # Laufende Candle aktualisieren
                            b = st["bar"]
                            b["high_bid"] = max(b["high_bid"], bid)
                            b["low_bid"] = min(b["low_bid"], bid)
                            b["close_bid"] = bid
                            b["high_ask"] = max(b["high_ask"], ask)
                            b["low_ask"] = min(b["low_ask"], ask)
                            b["close_ask"] = ask
                            b["ticks"] += 1
                            b["timestamp"] = ts_ms

                        # Während der Minute Trend- und Chartdaten aktualisieren
                        on_candle_forming(epic, st["bar"], ts_ms)

                        # 🛡️ Schutz-Regeln prüfen (Stop-Loss, Trailing, BE, TP)
                        try:
                            # Echtzeitwerte verwenden (nie aus bar, sondern Live-Tick)
                            if bid is None or ask is None:
                                print(f"⚠️ [{epic}] Kein gültiger Bid/Ask empfangen – Überspringe Schutzprüfung.")
                                continue

                            # Spread immer live berechnen
                            spread = ask - bid if ask and bid else 0.0

                            # 🔍 Debug-Log (optional)
                            # print(f"[DEBUG] check_protection_rules({epic}) → bid={bid:.2f}, ask={ask:.2f}, spread={spread:.5f}")

                            check_protection_rules(epic, bid, ask, spread, CST, XSEC)

                        except Exception as e:
                            print(f"⚠️ [{epic}] Fehler in check_protection_rules: {e}")

                # 🧠 Sauberer Abbruch per STRG + C
        except KeyboardInterrupt:
            print("🛑 Abbruch durch Benutzer (CTRL+C)")
            break

        except Exception as e:
            print("❌ Verbindungsfehler:", e)

            # Falls Session ungültig → Tokens zurücksetzen
            if "invalid.session.token" in str(e).lower() or "force_reconnect" in str(e).lower():
                CST, XSEC = None, None

            # 🔧 WebSocket sauber schließen, damit kein Zombie-Task hängen bleibt
            try:
                if "ws" in locals() and ws:
                    await ws.close()
            except Exception:
                pass

            # 🔁 Wartezeit vor Neuverbindung
            CST, XSEC = None, None  # Token sicher invalidieren
            print(f"⏳ {RECONNECT_DELAY}s warten, dann neuer Versuch mit neuem Login ...")
            await asyncio.sleep(RECONNECT_DELAY)
            continue




# ==============================
# MAIN
# ==============================

if __name__ == "__main__":
    try:
        print("Startup sanity:")
        print("  Local now  :", datetime.now(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M:%S %Z"))
        print("  UTC now    :", datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC"))
        test_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        print("  to_local_dt:", to_local_dt(test_ms).strftime("%d.%m.%Y %H:%M:%S %Z"))

        load_parameters("startup")

        asyncio.run(run_candle_aggregator_per_instrument())
    except KeyboardInterrupt:
        print("\n🛑 Manuell abgebrochen (Ctrl+C erkannt)")
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
        except Exception:
            pass
        os._exit(0)


