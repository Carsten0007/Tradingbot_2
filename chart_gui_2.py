import threading
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import datetime as dt
import time
from collections import deque
from matplotlib.dates import DateFormatter
from zoneinfo import ZoneInfo

# Einheitliche TZ & Matplotlib-Epoch setzen
LOCAL_TZ = ZoneInfo("Europe/Berlin")
mdates.set_epoch('1970-01-01T00:00:00+00:00')

class ChartManager:
    def __init__(self, window_size_sec=300):
        self._title_cache = {}   # epic -> {"text": str, "color": Any}
        self.window = window_size_sec
        self.tz = ZoneInfo("Europe/Berlin")
        self.draw_throttle_ms = 200   # Ziel: ~5 FPS pro Instrument
        self._last_draw_ms = {}       # epic -> letzter Draw-Timestamp in ms
        self.data = {}      # {epic: deque([...])}
        self.lines = {}     # {epic: dict(matplotlib handles)}
        self.lock = threading.Lock()
        self.last_trade_state = {}
        self.flush_min_interval_ms = 200   # nur alle ≥200 ms flushen
        self._last_flush_ms = {}           # epic -> letzter flush (ms, monotonic)
        self.ffill_max_gap_ms = 900   # nach 0.9s ohne neuen Tick NICHT mehr halten
        self._ylim_cache = {}   # epic -> (ymin, ymax)

        plt.ion()  # Interaktiver Modus aktiv

    # -------------------------------------------------------
    #   Öffentliche Methode zum Aktualisieren
    # -------------------------------------------------------
    def update(
        self, epic, ts_ms, bar, pos,
        ema_fast=None, ema_slow=None, hma_fast=None, hma_slow=None,
        entry=None, sl=None, tp=None, ts=None, trend=None
    ):

        with self.lock:
            if pos is None:
                pos = {}

            if epic not in self.data:
                self._init_chart(epic)

            dq = self.data[epic]
            fig = self.lines[epic]["fig"]
            ax = fig.axes[0]
            # Einheitliche lokale Zeit (identisch zu tradeingbot)
            try:
                from tradingbot_2 import to_local_dt
                now = to_local_dt(ts_ms)
                # skew = (dt.datetime.now(LOCAL_TZ) - now).total_seconds()
                # if abs(skew) > 1 and 980 <= (ts_ms % 1000) <= 999:
                #     print(f"[Chart DEBUG] Clock skew vs tick: {skew:+.2f}s")

            except ImportError:
                # Fallback trotzdem TZ-aware (lokale Europe/Berlin)
                now = dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=LOCAL_TZ)

            # Bid / Ask übernehmen
            # 🧩 Sicherstellen, dass Bid/Ask immer float sind
            bid = bar.get("bid")
            ask = bar.get("ask")
            # Falls gültige Werte vorhanden → in float umwandeln
            bid = float(bid) if bid not in (None, "None") else None
            ask = float(ask) if ask not in (None, "None") else None



            # 🧠 Letzten Datensatz übernehmen, falls neue Werte fehlen
            last = dq[-1] if dq else {}
            bid_time = now if bid is not None else last.get("bid_time")
            ask_time = now if ask is not None else last.get("ask_time")

            dq.append({
                "time": now,
                "bid": bid,
                "ask": ask,
                "close": bar.get("close"),

                # ⬇️ NEU: Zeitpunkte letzter REALER Updates / 16.11.2025 00:44, diagramm fix 7a+
                "bid_time": bid_time,
                "ask_time": ask_time,

                # Entry bleibt persistent, wenn kein neuer Wert kommt
                "entry": pos.get("entry_price") or last.get("entry"),

                # Stop-Loss
                "sl": float(bar.get("sl") or pos.get("stop_loss") or last.get("sl") or 0)
                    if (bar.get("sl") or pos.get("stop_loss") or last.get("sl")) not in (None, "None")
                    else None,

                # Take-Profit
                "tp": float(bar.get("tp") or pos.get("take_profit") or last.get("tp") or 0)
                    if (bar.get("tp") or pos.get("take_profit") or last.get("tp")) not in (None, "None")
                    else None,

                # Trailing
                "ts": float(bar.get("ts") or pos.get("trailing_stop") or last.get("ts") or 0)
                    if (bar.get("ts") or pos.get("trailing_stop") or last.get("ts")) not in (None, "None")
                    else None,

                # Break-Even
                "be": float(bar.get("be") or pos.get("break_even_level") or last.get("be") or 0)
                    if (bar.get("be") or pos.get("break_even_level") or last.get("be")) not in (None, "None")
                    else None,

                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "hma_fast": hma_fast,
                "hma_slow": hma_slow,
                "direction": pos.get("direction"),
            })

            # ✅ Safe Guard: Datenpuffer auf sichtbares Fenster begrenzen (unsichtbare Altlasten entfernen)
            cutoff = now - dt.timedelta(seconds=self.window + 5)  # +5s Puffer
            while len(dq) > 2 and dq[0]["time"] < cutoff:
                dq.popleft()

            # 🕒 Time-Sync-Fix – Datenpunkte chronologisch halten
            # Falls ein verspäteter Tick kommt, sortieren wir die deque neu
            if len(dq) > 2 and dq[-1]["time"] < dq[-2]["time"]:
                dq = deque(sorted(dq, key=lambda x: x["time"]), maxlen=dq.maxlen)
                self.data[epic] = dq


            # # Rolling Window begrenzen
            # while len(dq) > 2 and (now - dq[0]["time"]).total_seconds() > self.window:
            #     dq.popleft()

           # 🧠 Trade-Zustand prüfen
            trade_open = bool(pos.get("entry_price"))
            last_state = self.last_trade_state.get(epic)

            if trade_open:
                # Trade aktiv → Entry immer aktualisieren (Marker bleibt sichtbar)
                self._mark_entry(epic, pos.get("entry_price"))

            elif not trade_open and last_state:
                # Trade wurde beendet – jetzt wirklich löschen
                print(f"[Chart] {epic}: Trade beendet, lösche Linien ...")
                self._clear_trade_lines(epic)

                # Trade beendet – auch Datenpuffer (dq) leeren, damit keine alten Stops/Trailing neu erscheinen
                for d in dq:
                    for k in ["sl", "tp", "ts", "be", "entry"]:
                        d[k] = None

                # 🧩 Auch visuelle Linien sofort löschen
                for key in ["sl", "tp", "ts", "be"]:
                    self.lines[epic][key].set_data([], [])
                self.lines[epic]["entry_marker"].set_data([], [])
                self.lines[epic]["entry"].set_data([], [])

                # Stelle sicher, dass direction zurückgesetzt wird
                for d in dq:
                    d["direction"] = None


            # elif not trade_open and not last_state:
                # Kein Trade aktiv → sicherheitshalber auch alle Linien leeren
                # self._clear_trade_lines(epic)

            # Status speichern
            self.last_trade_state[epic] = trade_open

            # -------------------------------------------------------
            #   Diagrammtitel dynamisch anpassen (Trade-Zustand + Balance)
            # -------------------------------------------------------
            balance_val = None

            if pos and isinstance(pos, dict) and pos.get("direction") and pos.get("entry_price"):
                # 1) Primär: Live-PnL aus dem Tickpfad verwenden
                balance_val = pos.get("unrealized_pnl")

                # 2) Fallback (nur falls noch nicht gesetzt): alte Berechnung
                if balance_val is None:
                    bid_now = dq[-1].get("bid")
                    ask_now = dq[-1].get("ask")
                    from tradingbot_2 import MANUAL_TRADE_SIZE
                    size = pos.get("size") if pos.get("size") else MANUAL_TRADE_SIZE

                    if pos["direction"] == "BUY" and bid_now is not None:
                        balance_val = (bid_now - pos["entry_price"]) * size
                    elif pos["direction"] == "SELL" and ask_now is not None:
                        balance_val = (pos["entry_price"] - ask_now) * size

                if balance_val is None:
                    return

                color = "green" if balance_val > 0 else "red" if balance_val < 0 else "black"
                title = f"{'LONG' if pos['direction']=='BUY' else 'SHORT'} Trade offen | Δ {balance_val:+.2f} $"
            else:
                title = "Aktuell kein Trade"
                color = "black"

            # Beispiel: vorhandene Variablen verwenden, falls vorhanden
            title_text  = title   # ← das oben berechnete
            title_color = color

            cached = self._title_cache.get(epic)
            if (cached is None 
                or cached.get("text") != title_text 
                or cached.get("color") != title_color):
                ax.set_title(title_text, color=title_color)  # 🔁 nur wenn nötig
                self._title_cache[epic] = {"text": title_text, "color": title_color}

            # 🔇 Throttle: nicht bei jedem Tick rendern
            last = self._last_draw_ms.get(epic)
            if last is not None and (ts_ms - last) < self.draw_throttle_ms:
                return
            self._last_draw_ms[epic] = ts_ms

            # 🔁 Refresh
            self._refresh_chart(epic)
            # (Kein zusätzlicher Draw/Flush hier – _refresh_chart erledigt das bereits)




    # -------------------------------------------------------
    #   Neues Instrument initialisieren
    # -------------------------------------------------------
    def _init_chart(self, epic):
        dq = deque(maxlen=2000)
        self.data[epic] = dq

        fig, ax = plt.subplots()
        fig.canvas.manager.window.attributes('-topmost', 0)
        fig.canvas.manager.set_window_title(f"{epic} – Live Chart")
        ax.set_title(f"Live Chart – {epic}")
        self._title_cache[epic] = {"text": f"Live Chart – {epic}", "color": None}
        self._ylim_cache[epic] = None
        ax.set_xlabel("Zeit")
        ax.set_ylabel("Preis")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=self.tz))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())

        # 🧭 Initial-Skalierung – 5 Minuten Fenster und Dummy-Y-Range
        now = dt.datetime.now(LOCAL_TZ)
        ax.set_xlim(now - dt.timedelta(seconds=self.window), now)

        # Temporärer Y-Bereich, damit nichts flackert (z. B. ±1 % um 4000)
        # 🧠 Dynamischer Startbereich – später auto-angepasst
        ax.set_ylim(3800, 4100)   # breiter Startbereich, damit Kurs sofort sichtbar ist



        # Linien vorbereiten – deutlicher & konsistenter
        lines = {
            "bid": ax.plot([], [], label="Bid", color="blue", linewidth=0.5, alpha=0.9)[0],
            "ask": ax.plot([], [], label="Ask", color="red", linewidth=0.5, alpha=0.9)[0],
            "entry": ax.plot([], [], label="Entry", color="gray", linestyle="--")[0],
            "sl": ax.plot([], [], label="Stop-Loss", color="darkorange", linestyle=":", linewidth=1.1)[0],
            "ts": ax.plot([], [], label="Trailing", color="orange", linestyle="--", linewidth=1.1)[0],
            "tp": ax.plot([], [], label="Take-Profit", color="green", linestyle=":", linewidth=1.1)[0],
            "be": ax.plot([], [], label="Break-Even", color="brown", linestyle="-.", linewidth=1.1)[0],
            "ema_fast": ax.plot([], [], label="EMA Fast", color="darkgrey", linewidth=0.5, alpha=0.5)[0],
            "ema_slow": ax.plot([], [], label="EMA Slow", color="darkgrey", linestyle="--", linewidth=0.5, alpha=0.5)[0],
            "hma_fast": ax.plot([], [], label="HMA Fast", color="darkgreen", linewidth=0.9, alpha=0.6)[0],
            "hma_slow": ax.plot([], [], label="HMA Slow", color="darkgreen", linestyle="--", linewidth=0.9, alpha=0.6)[0],
        }

        # Marker für Entry
        lines["entry_marker"] = ax.plot([], [], "go", markersize=4, label="Entry")[0]

        ax.legend(loc="upper left", fontsize="small")
        plt.show(block=False)
        self.lines[epic] = {"fig": fig, "ax": ax, **lines}

    # -------------------------------------------------------
    #   Chart-Redraw
    # -------------------------------------------------------
    def _refresh_chart(self, epic):
        dq = self.data[epic]
        if len(dq) < 1:
            return

        lines = self.lines[epic]
        times = [d["time"] for d in dq]


        # 🚫 Falls kein Trade offen → BE-, SL-, TP-, TS-Linien löschen
        if not any(d.get("direction") for d in dq if d.get("direction")):
            for key in ["sl", "tp", "ts", "be"]:
                lines[key].set_data([], [])


        # 🔧 Forward-Fill nur bis max ffill_max_gap_ms (sonst Lücke = None)
        bids, asks = [], []
        last_bid = last_ask = None
        last_bid_time = last_ask_time = None
        # ⬇️ Level-Cache bleibt erhalten
        last_sl = last_tp = last_ts = last_be = last_entry = None

        for d in dq:
            # Reale Updates übernehmen
            if d.get("bid") is not None:
                last_bid = d["bid"]
                last_bid_time = d.get("bid_time")  # kommt aus update()
            if d.get("ask") is not None:
                last_ask = d["ask"]
                last_ask_time = d.get("ask_time")  # kommt aus update()

            # Level-Cache aktualisieren (unverändert)
            v = d.get("sl");    last_sl    = v if isinstance(v, (int, float)) else last_sl
            v = d.get("tp");    last_tp    = v if isinstance(v, (int, float)) else last_tp
            v = d.get("ts");    last_ts    = v if isinstance(v, (int, float)) else last_ts
            v = d.get("be");    last_be    = v if isinstance(v, (int, float)) else last_be
            v = d.get("entry"); last_entry = v if isinstance(v, (int, float)) else last_entry

            # Zeitpunkt dieses Samples
            t = d["time"]

            # Bid nur gültig, wenn seit letztem echten Bid-Update ≤ TTL
            if last_bid is not None and last_bid_time is not None:
                age_ms_bid = (t - last_bid_time).total_seconds() * 1000.0
                b_val = last_bid if age_ms_bid <= self.ffill_max_gap_ms else None
            else:
                b_val = None

            # Ask dito
            if last_ask is not None and last_ask_time is not None:
                age_ms_ask = (t - last_ask_time).total_seconds() * 1000.0
                a_val = last_ask if age_ms_ask <= self.ffill_max_gap_ms else None
            else:
                a_val = None

            bids.append(b_val)
            asks.append(a_val)


        # 📊 Bid/Ask unabhängig behandeln (keine Erzwingung „beide Seiten“)
        bid_pts = [(t, b) for t, b in zip(times, bids) if b is not None]
        ask_pts = [(t, a) for t, a in zip(times, asks) if a is not None]
        if not bid_pts and not ask_pts:
            return

        # Zeitfenster am rechten Fensterrand ausrichten (letzter Timestamp des Puffers)
        max_time = times[-1]
        min_time = max_time - dt.timedelta(seconds=self.window)
        ax = self.lines[epic]["ax"]
        ax.set_xlim(min_time, max_time)

        # Punkte im Fenster
        bid_w = [(t, b) for (t, b) in bid_pts if (min_time <= t <= max_time)]
        ask_w = [(t, a) for (t, a) in ask_pts if (min_time <= t <= max_time)]

        # Linien setzen – jede Seite separat
        if bid_w:
            t_b, b_vals = zip(*bid_w)
            lines["bid"].set_data(t_b, b_vals)
        else:
            b_vals = []  # leer für Y-Scale unten
            lines["bid"].set_data([], [])

        if ask_w:
            t_a, a_vals = zip(*ask_w)
            lines["ask"].set_data(t_a, a_vals)
        else:
            a_vals = []
            lines["ask"].set_data([], [])


        # 📏 Dynamische Initial-Skalierung
        ax = self.lines[epic]["ax"]
        # X-Achse: lokale Zeit anzeigen (z. B. Europe/Berlin)
        # ax.xaxis.set_major_formatter(
        #     DateFormatter("%H:%M:%S", tz=ZoneInfo("Europe/Berlin"))
        # )
        
        # 📈 EMA/HMA-Linien mit Sanity-Check (nur wenn genügend gültige Werte)
        for key in ["ema_fast", "ema_slow", "hma_fast", "hma_slow"]:
            pts = [
                (d["time"], d.get(key))
                for d in dq
                if isinstance(d.get(key), (int, float)) and (min_time <= d["time"] <= max_time)
            ]
            if len(pts) >= 3:
                t_ma, v_ma = zip(*pts)
                lines[key].set_data(t_ma, v_ma)
            else:
                lines[key].set_data([], [])
        
        # 🔹 TS (Trailing) als Zeitreihe im Fenster
        ts_pts = [
            (d["time"], d.get("ts"))
            for d in dq
            if isinstance(d.get("ts"), (int, float)) and (min_time <= d["time"] <= max_time)
        ]
        if ts_pts:
            t_ts, v_ts = zip(*ts_pts)
            lines["ts"].set_data(t_ts, v_ts)
        else:
            lines["ts"].set_data([], [])

        # 🔹 SL/TP/BE als horizontale Linien – aus Cache, kein dq-Re-Scan
        for key, val in (("sl", last_sl), ("tp", last_tp), ("be", last_be)):
            if val is not None:
                lines[key].set_data([min_time, max_time], [val, val])
            else:
                lines[key].set_data([], [])


        # 🕒 X-Achse immer als rollendes Fenster mit fixer Breite (self.window Sekunden)
        ax = lines["ax"]

        # 2️⃣ Y-Achse: aus aktuell geplotteten Daten (+5 % Puffer), robust bei leeren Seiten
        ymins, ymaxs = [], []

        # Bid/Ask aus den im Fenster geplotteten Arrays
        if 'b_vals' in locals() and b_vals:
            ymins.append(min(b_vals)); ymaxs.append(max(b_vals))
        if 'a_vals' in locals() and a_vals:
            ymins.append(min(a_vals)); ymaxs.append(max(a_vals))

        # EMA/HMA/TS/Horizontale Level/Entry mit berücksichtigen
        for key in ["ema_fast", "ema_slow", "hma_fast", "hma_slow", "ts", "sl", "tp", "be", "entry"]:
            ydata = lines[key].get_ydata()
            if len(ydata):
                ymins.append(min(ydata))
                ymaxs.append(max(ydata))

        # Nur wenn wir irgendetwas haben, Limits setzen
        if ymins:
            ymin, ymax = min(ymins), max(ymaxs)
            padding = (ymax - ymin) * 0.05 if ymax > ymin else 0.01
            if ymins:
                ymin, ymax = min(ymins), max(ymaxs)
                padding = (ymax - ymin) * 0.05 if ymax > ymin else 0.01

                new_ylim = (ymin - padding, ymax + padding)
                old_ylim = self._ylim_cache.get(epic)

                # kleine Toleranz, damit nicht wegen Rundungsrauschen ständig gesetzt wird
                eps = 1e-6
                if (old_ylim is None
                    or abs(new_ylim[0] - old_ylim[0]) > eps
                    or abs(new_ylim[1] - old_ylim[1]) > eps):
                    ax.set_ylim(new_ylim[0], new_ylim[1])
                    self._ylim_cache[epic] = new_ylim

        # 🟩 Entry-Linie über das aktuelle Fenster – aus Cache
        if isinstance(last_entry, (int, float)):
            lines["entry"].set_data([min_time, max_time], [last_entry, last_entry])
        else:
            lines["entry"].set_data([], [])
        
        # 🔁 Refresh
        lines["fig"].canvas.draw_idle()

        # ⏱️ Rate-Limit fürs Flush (pro Epic)
        now_ms = int(time.monotonic() * 1000)
        last = self._last_flush_ms.get(epic, 0)
        if (now_ms - last) >= self.flush_min_interval_ms:
            lines["fig"].canvas.flush_events()
            self._last_flush_ms[epic] = now_ms


    # -------------------------------------------------------
    #   Entry-Marker
    # -------------------------------------------------------
    def _mark_entry(self, epic, price):
        if not price or epic not in self.lines:
            return

        # Nur markieren, wenn noch kein Entry vorhanden ist
        existing_x, existing_y = self.lines[epic]["entry_marker"].get_data()
        if existing_x and existing_y:
            return  # bereits gesetzt

        # Zeitpunkt des letzten gültigen Datensatzes
        t = self.data[epic][-1]["time"] if self.data.get(epic) else dt.datetime.now(LOCAL_TZ)

        # Marker + Linie setzen
        self.lines[epic]["entry_marker"].set_data([t], [price])
        self.lines[epic]["entry"].set_data([t - dt.timedelta(seconds=1), t + dt.timedelta(seconds=1)], [price, price])

        print(f"[Chart Debug] Entry fixiert {epic} @ {price:.2f}")


    # -------------------------------------------------------
    #   Linien löschen nach Trade-Ende
    # -------------------------------------------------------
    def _clear_trade_lines(self, epic):
        if epic not in self.lines:
            return

        for key in ["entry", "sl", "tp", "ts", "be", "entry_marker"]:
            self.lines[epic][key].set_data([], [])

        # Reset des letzten Zustands
        self.last_trade_state[epic] = False

        print(f"[Chart] Trade beendet → Linien für {epic} entfernt, Bid/Ask bleiben erhalten")

