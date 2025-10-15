import threading
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import datetime as dt
from collections import deque
import time


class ChartManager:
    def __init__(self, window_size_sec=300):
        self.window = window_size_sec
        self.data = {}      # {epic: deque([...])}
        self.lines = {}     # {epic: dict(matplotlib handles)}
        self.lock = threading.Lock()
        self.last_trade_state = {}

        plt.ion()  # Interaktiver Modus aktiv

    # -------------------------------------------------------
    #   Öffentliche Methode zum Aktualisieren
    # -------------------------------------------------------
    def update(self, epic, ts_ms, bar, trend, pos,
               ema_fast=None, ema_slow=None, hma_fast=None, hma_slow=None):
        with self.lock:
            if epic not in self.data:
                self._init_chart(epic)

            dq = self.data[epic]
            now = dt.datetime.fromtimestamp(ts_ms / 1000.0)

            # Positionsdaten sicher lesen
            entry_price = pos.get("entry_price") if isinstance(pos, dict) else None
            trailing_stop = pos.get("trailing_stop") if isinstance(pos, dict) else None
            break_even = pos.get("break_even_level") if isinstance(pos, dict) else None
            direction = pos.get("direction") if isinstance(pos, dict) else None

            # Bid / Ask übernehmen
            bid = bar.get("bid")
            ask = bar.get("ask")

            dq.append({
                "time": now,
                "bid": bid,
                "ask": ask,
                "close": bar.get("close"),
                "trend": trend,
                "entry": entry_price,
                "sl": pos.get("stop_loss") if isinstance(pos, dict) else None,
                "tp": pos.get("take_profit") if isinstance(pos, dict) else None,
                "ts": trailing_stop,
                "be": break_even,
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "hma_fast": hma_fast,
                "hma_slow": hma_slow,
                "direction": direction
            })

            # Rolling Window begrenzen
            while len(dq) > 2 and (now - dq[0]["time"]).total_seconds() > self.window:
                dq.popleft()

            # 🧠 Trade-Zustand prüfen
            trade_open = bool(direction)
            last_state = self.last_trade_state.get(epic)

            if trade_open and not last_state:
                self._mark_entry(epic, entry_price)
            elif not trade_open and last_state:
                self._clear_trade_lines(epic)

            self.last_trade_state[epic] = trade_open

            # 🔁 Refresh
            self._refresh_chart(epic)
            plt.pause(0.001)

    # -------------------------------------------------------
    #   Neues Instrument initialisieren
    # -------------------------------------------------------
    def _init_chart(self, epic):
        dq = deque(maxlen=600)
        self.data[epic] = dq

        fig, ax = plt.subplots()
        fig.canvas.manager.window.attributes('-topmost', 0)
        ax.set_title(f"Live Chart – {epic}")
        ax.set_xlabel("Zeit")
        ax.set_ylabel("Preis")
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))

        # Linien vorbereiten – deutlicher & konsistenter
        lines = {
            "bid": ax.plot([], [], label="Bid", color="lightblue", linewidth=0.8, alpha=0.8)[0],
            "ask": ax.plot([], [], label="Ask", color="lightcoral", linewidth=0.8, alpha=0.8)[0],
            "entry": ax.plot([], [], label="Entry", color="gray", linestyle="--")[0],
            "sl": ax.plot([], [], label="Stop-Loss", color="red", linestyle=":", linewidth=1.1)[0],
            "ts": ax.plot([], [], label="Trailing", color="orange", linestyle="--", linewidth=1.1)[0],
            "tp": ax.plot([], [], label="Take-Profit", color="green", linestyle=":", linewidth=1.1)[0],
            "be": ax.plot([], [], label="Break-Even", color="purple", linestyle="-.", linewidth=1.1)[0],
            "ema_fast": ax.plot([], [], label="EMA Fast", color="cyan", linewidth=0.9, alpha=0.8)[0],
            "ema_slow": ax.plot([], [], label="EMA Slow", color="magenta", linewidth=0.9, alpha=0.8)[0],
            "hma_fast": ax.plot([], [], label="HMA Fast", color="deepskyblue", linewidth=0.9, alpha=0.8)[0],
            "hma_slow": ax.plot([], [], label="HMA Slow", color="violet", linewidth=0.9, alpha=0.8)[0],
        }

        # Marker für Entry
        lines["entry_marker"] = ax.plot([], [], "go", markersize=8, label="Entry")[0]

        ax.legend(loc="upper left", fontsize="small")
        plt.show(block=False)
        self.lines[epic] = {"fig": fig, "ax": ax, **lines}

    # -------------------------------------------------------
    #   Chart-Redraw
    # -------------------------------------------------------
    def _refresh_chart(self, epic):
        dq = self.data[epic]
        if len(dq) < 2:
            return

        lines = self.lines[epic]
        times = [d["time"] for d in dq]

        # Bid / Ask nur zeichnen, wenn Werte vorhanden
        bids = [d["bid"] for d in dq if d["bid"] is not None]
        asks = [d["ask"] for d in dq if d["ask"] is not None]
        if bids:
            lines["bid"].set_data(times[-len(bids):], bids[-len(bids):])
        if asks:
            lines["ask"].set_data(times[-len(asks):], asks[-len(asks):])

        # Stops, Trailing, etc.
        for key in ["entry", "sl", "tp", "ts", "be"]:
            vals = [d[key] for d in dq if d[key] is not None]
            if vals:
                lines[key].set_data(times[-len(vals):], vals[-len(vals):])
            else:
                lines[key].set_data([], [])

        # EMA/HMA Linien
        for key in ["ema_fast", "ema_slow", "hma_fast", "hma_slow"]:
            vals = [d[key] for d in dq if d[key] is not None]
            if vals:
                lines[key].set_data(times[-len(vals):], vals[-len(vals):])
            else:
                lines[key].set_data([], [])

        # Achsen aktualisieren (nur, wenn Werte vorhanden)
        ax = lines["ax"]
        if bids or asks:
            ax.relim()
            ax.autoscale_view()

        lines["fig"].canvas.draw_idle()
        lines["fig"].canvas.flush_events()

    # -------------------------------------------------------
    #   Entry-Marker
    # -------------------------------------------------------
    def _mark_entry(self, epic, price):
        if epic in self.lines:
            t = dt.datetime.now()
            self.lines[epic]["entry_marker"].set_data([t], [price])

    # -------------------------------------------------------
    #   Linien löschen nach Trade-Ende
    # -------------------------------------------------------
    def _clear_trade_lines(self, epic):
        if epic not in self.lines:
            return
        dq = self.data.get(epic, deque())
        dq.clear()  # <- leert die Datenpunkte
        for key in ["entry", "sl", "tp", "ts", "be", "entry_marker"]:
            self.lines[epic][key].set_data([], [])
        self.lines[epic]["fig"].canvas.draw_idle()
