import threading
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import datetime as dt
from collections import deque
import time

# ===============================================
#   ChartManager – Live-Visualisierung pro Instrument
# ===============================================

class ChartManager:
    def __init__(self, window_size_sec=300):
        self.window = window_size_sec  # z. B. 5 Minuten
        self.data = {}  # {epic: deque([...])}
        self.lines = {}  # {epic: dict(matplotlib handles)}
        self.lock = threading.Lock()

        # Hintergrundthread für Live-Redraw
        self._stop_flag = False
        self.thread = threading.Thread(target=self._redraw_loop, daemon=True)
        self.thread.start()

    # -------------------------------------------------------
    #   Öffentliche Methode zum Aktualisieren eines Charts
    # -------------------------------------------------------
    def update(self, epic, ts_ms, bar, trend, pos):
        with self.lock:
            if epic not in self.data:
                self._init_chart(epic)

            dq = self.data[epic]
            now = dt.datetime.fromtimestamp(ts_ms / 1000.0)
            dq.append({
                "time": now,
                "close": bar["close"],
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "trend": trend,
                "entry": pos.get("entry_price"),
                "sl": pos.get("entry_price") * (1 - 0.0018) if pos.get("entry_price") else None,
                "tp": pos.get("entry_price") * (1 + 0.0050) if pos.get("entry_price") else None,
                "ts": pos.get("trailing_stop"),
                "be": pos.get("break_even_level")
            })

            # Rolling Window begrenzen
            while len(dq) > 2 and (now - dq[0]["time"]).total_seconds() > self.window:
                dq.popleft()

    # -------------------------------------------------------
    #   Chart initialisieren (neues Instrument)
    # -------------------------------------------------------
    def _init_chart(self, epic):
        dq = deque(maxlen=600)
        self.data[epic] = dq

        fig, ax = plt.subplots()
        ax.set_title(f"Live Chart – {epic}")
        ax.set_xlabel("Zeit")
        ax.set_ylabel("Preis")
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M:%S'))

        # Linien vorbereiten
        lines = {
            "price": ax.plot([], [], label="Close", color="blue")[0],
            "entry": ax.plot([], [], label="Entry", color="gray", linestyle="--")[0],
            "sl": ax.plot([], [], label="Stop-Loss", color="red", linestyle=":")[0],
            "ts": ax.plot([], [], label="Trailing", color="orange", linestyle="--")[0],
            "tp": ax.plot([], [], label="Take-Profit", color="green", linestyle=":")[0],
            "be": ax.plot([], [], label="Break-Even", color="purple", linestyle="-.")[0],
        }

        ax.legend(loc="upper left")
        plt.ion()
        plt.show(block=False)

        self.lines[epic] = {"fig": fig, "ax": ax, **lines}

    # -------------------------------------------------------
    #   Hintergrundthread – regelmäßige Redraws
    # -------------------------------------------------------
    def _redraw_loop(self):
        while not self._stop_flag:
            time.sleep(1.0)
            with self.lock:
                for epic in list(self.data.keys()):
                    self._refresh_chart(epic)

    # -------------------------------------------------------
    #   Einzelnes Chart updaten
    # -------------------------------------------------------
    def _refresh_chart(self, epic):
        dq = self.data[epic]
        if len(dq) < 2:
            return

        lines = self.lines[epic]
        times = [d["time"] for d in dq]
        closes = [d["close"] for d in dq]

        lines["price"].set_data(times, closes)

        # Entry
        entry_vals = [d["entry"] for d in dq if d["entry"]]
        if entry_vals:
            lines["entry"].set_data(times[-len(entry_vals):], [entry_vals[-1]] * len(entry_vals))
        else:
            lines["entry"].set_data([], [])

        # Stop Loss / Take Profit / Trailing / Break Even
        for key in ["sl", "tp", "ts", "be"]:
            vals = [d[key] for d in dq if d[key]]
            if vals:
                lines[key].set_data(times[-len(vals):], [vals[-1]] * len(vals))
            else:
                lines[key].set_data([], [])

        # Achsen skalieren
        ax = lines["ax"]
        ax.relim()
        ax.autoscale_view()

        # Redraw
        lines["fig"].canvas.draw_idle()
        lines["fig"].canvas.flush_events()

    # -------------------------------------------------------
    #   Stoppen
    # -------------------------------------------------------
    def stop(self):
        self._stop_flag = True
