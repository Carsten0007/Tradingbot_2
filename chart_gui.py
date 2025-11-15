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
        self.window = window_size_sec
        self.tz = ZoneInfo("Europe/Berlin")
        self.draw_throttle_ms = 200   # Ziel: ~5 FPS pro Instrument
        self._last_draw_ms = {}       # epic -> letzter Draw-Timestamp in ms
        self.data = {}      # {epic: deque([...])}
        self.lines = {}     # {epic: dict(matplotlib handles)}
        self.lock = threading.Lock()
        self.last_trade_state = {}

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
                from tradeingbot import to_local_dt
                now = to_local_dt(ts_ms)
                skew = (dt.datetime.now(LOCAL_TZ) - now).total_seconds()
                if abs(skew) > 1 and 980 <= (ts_ms % 1000) <= 999:
                    print(f"[Chart DEBUG] Clock skew vs tick: {skew:+.2f}s")

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

            dq.append({
                "time": now,
                "bid": bid,
                "ask": ask,
                "close": bar.get("close"),

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
                self.lines[epic]["fig"].canvas.draw_idle()

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
                    from tradeingbot import MANUAL_TRADE_SIZE
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

            ax.set_title(title, color=color)

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
            "ema_fast": ax.plot([], [], label="EMA Fast", color="lightgrey", linewidth=0.5, alpha=0.6)[0],
            "ema_slow": ax.plot([], [], label="EMA Slow", color="darkgrey", linewidth=0.5, alpha=0.6)[0],
            "hma_fast": ax.plot([], [], label="HMA Fast", color="lightgreen", linewidth=0.9, alpha=0.6)[0],
            "hma_slow": ax.plot([], [], label="HMA Slow", color="darkgreen", linewidth=0.9, alpha=0.6)[0],
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


        # 🔧 Immer letzten bekannten Wert übernehmen (statt leere Stellen)
        bids, asks = [], []
        last_bid = last_ask = None
        for d in dq:
            if d.get("bid") is not None:
                last_bid = d["bid"]
            if d.get("ask") is not None:
                last_ask = d["ask"]
            bids.append(last_bid)
            asks.append(last_ask)

        # 📊 Nur gültige Punkte verwenden
        valid_points = [(t, b, a) for t, b, a in zip(times, bids, asks) if b is not None and a is not None]
        if not valid_points:
            return  # kein einziger Tick → nichts zeichnen

        # Zeitfenster zuerst bestimmen (am letzten gültigen Tick)
        last_t = valid_points[-1][0]
        max_time = last_t
        min_time = max_time - dt.timedelta(seconds=self.window)
        ax = self.lines[epic]["ax"]
        ax.set_xlim(min_time, max_time)

        # ur Punkte im Fenster zeichnen
        window_pts = [(t, b, a) for (t, b, a) in valid_points if (min_time <= t <= max_time)]
        if not window_pts:
            return
        t_vals, b_vals, a_vals = zip(*window_pts)
        lines["bid"].set_data(t_vals, b_vals)
        lines["ask"].set_data(t_vals, a_vals)

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

        # 🔹 SL/TP/BE weiterhin als horizontale Linien über das aktuelle Fenster
        for key in ["sl", "tp", "be"]:
            val = next((d.get(key) for d in reversed(dq) if isinstance(d.get(key), (int, float))), None)
            if val is not None:
                lines[key].set_data([min_time, max_time], [val, val])
            else:
                lines[key].set_data([], [])

        # 🕒 X-Achse immer als rollendes Fenster mit fixer Breite (self.window Sekunden)
        ax = lines["ax"]

        if bids or asks:
            # # 1️⃣ Zeitfenster: immer genau self.window Sekunden breit
            # max_time = times[-1]
            # min_time = max_time - dt.timedelta(seconds=self.window)
            # ax.set_xlim(min_time, max_time)

            # 2️⃣ Y-Achse: automatisch auf relevante Werte im Fenster skalieren (+5 % Puffer)
            values = list(b_vals) + list(a_vals)

            for key in ["sl", "tp", "ts", "be", "entry"]:
                val = next((d.get(key) for d in reversed(dq) if isinstance(d.get(key), (int, float))), None)
                if val is not None:
                    values.append(val)

            if values:
                ymin, ymax = min(values), max(values)
                padding = (ymax - ymin) * 0.05 if ymax > ymin else 0.01
                ax.set_ylim(ymin - padding, ymax + padding)


        # 🟩 Entry-Linie über das aktuelle Fenster
        entry_vals = [d.get("entry") for d in dq if isinstance(d.get("entry"), (int, float))]
        if entry_vals:
            entry_price = entry_vals[-1]
            lines["entry"].set_data([min_time, max_time], [entry_price, entry_price])
        else:
            lines["entry"].set_data([], [])
        
        # 🔁 Refresh
        lines["fig"].canvas.draw_idle()
        lines["fig"].canvas.flush_events()

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
        self.lines[epic]["fig"].canvas.draw_idle()

