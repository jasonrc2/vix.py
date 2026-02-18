import requests
import time
import json
from collections import deque
from datetime import datetime, timedelta

#########################################
# CONFIG
#########################################

TELEGRAM_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
CHAT_ID = "YOUR_CHAT_ID"

CHECK_INTERVAL = 600  # seconds
EARLY_WARNING_THRESHOLD = -0.25
SMOOTHING_DAYS = 3
TREND_LENGTH = 5
WEEKLY_LENGTH = 7  # store last 7 readings

HISTORY_FILE = "history.json"

#########################################
# TELEGRAM HELPER
#########################################

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": message})

#########################################
# DATA FETCH
#########################################

def get_price(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(url).json()
    return r["chart"]["result"][0]["meta"]["regularMarketPrice"]

def fetch_market_data():
    data = {}
    data["vix"] = get_price("^VIX")
    data["vvix"] = get_price("^VVIX")
    data["spx"] = get_price("^GSPC")
    data["vx1"] = get_price("VIXY")    # ETF proxies
    data["vx2"] = get_price("UVXY")
    data["vx3"] = data["vx2"] * 1.01
    return data

#########################################
# STATE MEMORY
#########################################

previous = {"vix": None, "vvix": None, "spx": None, "spread": None, "regime": None}

history = {
    "vix_strength": deque(maxlen=SMOOTHING_DAYS),
    "vvix_strength": deque(maxlen=SMOOTHING_DAYS),
    "spread_strength": deque(maxlen=SMOOTHING_DAYS),
    "spx_strength": deque(maxlen=SMOOTHING_DAYS),
    "vix_trend": deque(maxlen=TREND_LENGTH),
    "vvix_trend": deque(maxlen=TREND_LENGTH),
    "spread_trend": deque(maxlen=TREND_LENGTH),
    "spx_trend": deque(maxlen=TREND_LENGTH),
    # weekly storage
    "vix_week": deque(maxlen=WEEKLY_LENGTH),
    "vvix_week": deque(maxlen=WEEKLY_LENGTH),
    "spread_week": deque(maxlen=WEEKLY_LENGTH),
    "spx_week": deque(maxlen=WEEKLY_LENGTH),
    "regime_week": deque(maxlen=WEEKLY_LENGTH),
    "date_week": deque(maxlen=WEEKLY_LENGTH)
}

#########################################
# JSON PERSISTENCE
#########################################

def load_history():
    try:
        with open(HISTORY_FILE) as f:
            data = json.load(f)
            for k in data:
                history[k] = deque(data[k], maxlen=history[k].maxlen)
        print("✅ Loaded history from JSON")
    except FileNotFoundError:
        print("⚠️ No history file found, starting fresh")

def save_history():
    out = {k: list(v) for k,v in history.items()}
    with open(HISTORY_FILE, "w") as f:
        json.dump(out, f)
    print("💾 History saved")

#########################################
# CALCULATIONS
#########################################

def compute_changes(data):
    changes = {}
    if previous["vix"]:
        changes["vix_change"] = (data["vix"] - previous["vix"]) / previous["vix"] * 100
        changes["vvix_change"] = (data["vvix"] - previous["vvix"]) / previous["vvix"] * 100
        changes["spx_change"] = (data["spx"] - previous["spx"]) / previous["spx"] * 100
    else:
        changes["vix_change"] = 0
        changes["vvix_change"] = 0
        changes["spx_change"] = 0

    spread = data["vx1"] - data["vx2"]
    changes["spread"] = spread
    changes["spread_trending_down"] = previous["spread"] is not None and spread < previous["spread"]
    return changes

def fake_spike(data, changes):
    return changes["vix_change"] > 0 and changes["vvix_change"] > 0 and data["vx1"] > data["vx2"] > data["vx3"]

def probability_score(data, changes):
    score = 0
    if abs(changes["vix_change"]) > 3: score += 20
    if data["vx1"] < data["vx2"]: score += 20
    if changes["spread"] < -0.5: score += 15
    if changes["spread_trending_down"]: score += 10
    if changes["vvix_change"] < 0: score += 15
    if changes["spx_change"] > 0 and changes["vix_change"] < 0: score += 10
    if data["vx1"] < data["vx2"] < data["vx3"]: score += 10
    return score

def classify(score):
    if score < 30: return "PANIC"
    elif score < 50: return "TRANSITION"
    elif score < 70: return "EARLY_PHASE_1"
    elif score < 85: return "CONFIRMED_PHASE_1"
    else: return "LATE_PHASE_1"

def should_alert(new, old):
    important = ["TRANSITION","EARLY_PHASE_1","CONFIRMED_PHASE_1"]
    return new != old and new in important

def should_early_warning_alert(changes):
    return EARLY_WARNING_THRESHOLD >= changes["spread"] > -0.5

#########################################
# OPTION GUIDANCE
#########################################

def option_guidance_live(regime, vix_value):
    if regime == "EARLY_PHASE_1":
        delta = "0.35–0.45"
        dte = "45–75"
        note = "Starter position allowed"
    elif regime == "CONFIRMED_PHASE_1":
        delta = "0.35–0.55"
        dte = "45–75"
        note = "Primary entry window"
    elif regime == "LATE_PHASE_1":
        delta = "n/a"
        dte = "n/a"
        note = "Scale out, avoid new entries"
    else:
        delta = "n/a"
        dte = "n/a"
        note = "Avoid new positions"

    suggested_strike = round(vix_value + 1.5, 2) if regime in ["EARLY_PHASE_1","CONFIRMED_PHASE_1"] else None
    breakeven = round(suggested_strike - 1.5,2) if suggested_strike else None

    guidance_text = f"""
Delta: {delta} | DTE: {dte} | Note: {note}
Suggested Strike: {suggested_strike if suggested_strike else '-'}
Estimated Breakeven: {breakeven if breakeven else '-'}
"""
    return guidance_text.strip()

#########################################
# STRENGTH & TRENDS
#########################################

def signal_strength(value, ideal_positive=True):
    if ideal_positive:
        return max(min(int(value*5+50),100),0)
    else:
        return max(min(int((0-value)*5+50),100),0)

def heatmap_symbol(score):
    if score > 70: return "✅"
    elif score > 40: return "⚠️"
    else: return "❌"

def smoothed_strength(name, current_score):
    history[name].append(current_score)
    return int(sum(history[name])/len(history[name]))

def bar_visual(score):
    blocks = int(score / 10)
    return "█" * blocks + "─" * (10 - blocks)

def trend_visual(trend_deque):
    chart = ""
    for val in trend_deque:
        chart += bar_visual(val) + "\n"
    return chart

#########################################
# WEEKLY DASHBOARD
#########################################

def send_weekly_dashboard():
    if len(history["vix_week"]) < 2: return

    avg_vix = sum(history["vix_week"])/len(history["vix_week"])
    avg_vvix = sum(history["vvix_week"])/len(history["vvix_week"])
    avg_spread = sum(history["spread_week"])/len(history["spread_week"])
    avg_spx = sum(history["spx_week"])/len(history["spx_week"])

    def trend_arrow(seq):
        if seq[-1] > seq[0]: return "↑"
        elif seq[-1] < seq[0]: return "↓"
        else: return "→"

    msg = "📅 Weekly VIX Dashboard (last 7 readings)\n\n"
    for i in range(len(history["vix_week"])):
        date = history["date_week"][i]
        msg += f"{date} | VIX: {bar_visual(history['vix_week'][i])} | VVIX: {bar_visual(history['vvix_week'][i])} | Spread: {bar_visual(history['spread_week'][i])} | SPX: {bar_visual(history['spx_week'][i])} | Regime: {history['regime_week'][i]}\n"

    msg += f"\nAverage Strength: VIX {int(avg_vix)}, VVIX {int(avg_vvix)}, Spread {int(avg_spread)}, SPX {int(avg_spx)}\n"
    msg += f"Trend Arrows: VIX {trend_arrow(history['vix_week'])}, VVIX {trend_arrow(history['vvix_week'])}, Spread {trend_arrow(history['spread_week'])}, SPX {trend_arrow(history['spx_week'])}\n"
    msg += f"Current Regime: {previous['regime']}"
    send_telegram(msg)

#########################################
# MAIN LOOP
#########################################

def run():
    global previous
    load_history()
    send_telegram("✅ Ultimate VIX Scanner + Weekly Dashboard with Live Option Guidance Started")

    last_weekly_alert = datetime.now() - timedelta(days=1)

    while True:
        try:
            data = fetch_market_data()
            changes = compute_changes(data)

            if fake_spike(data, changes):
                send_telegram("⚠️ FAKE SPIKE DETECTED — Avoid VIX puts")
                time.sleep(CHECK_INTERVAL)
                continue

            score = probability_score(data, changes)
            regime = classify(score)

            # Early warning
            if should_early_warning_alert(changes):
                send_telegram(f"👀 PRE-PHASE-1 ALERT — Spread approaching negative ({changes['spread']:.3f})")

            # Real-time phase alert
            if should_alert(regime, previous["regime"]):
                # compute strengths
                vix_str = signal_strength(-changes["vix_change"])
                vvix_str = signal_strength(-changes["vvix_change"])
                spread_str = signal_strength(-changes["spread"], ideal_positive=True)
                spx_str = signal_strength(changes["spx_change"])

                # smoothed
                vix_smooth = smoothed_strength("vix_strength", vix_str)
                vvix_smooth = smoothed_strength("vvix_strength", vvix_str)
                spread_smooth = smoothed_strength("spread_strength", spread_str)
                spx_smooth = smoothed_strength("spx_strength", spx_str)

                # update trend history
                history["vix_trend"].append(vix_str)
                history["vvix_trend"].append(vvix_str)
                history["spread_trend"].append(spread_str)
                history["spx_trend"].append(spx_str)

                # update weekly
                today = datetime.now().strftime("%m-%d")
                history["vix_week"].append(vix_str)
                history["vvix_week"].append(vvix_str)
                history["spread_week"].append(spread_str)
                history["spx_week"].append(spx_str)
                history["regime_week"].append(regime)
                history["date_week"].append(today)

                # save history to JSON
                save_history()

                guidance_text = option_guidance_live(regime, data["vix"])

                charts = f"""
📈 Recent Trends (last {TREND_LENGTH} readings)
VIX: \n{trend_visual(history['vix_trend'])}
VVIX: \n{trend_visual(history['vvix_trend'])}
Spread: \n{trend_visual(history['spread_trend'])}
SPX: \n{trend_visual(history['spx_trend'])}
"""

                heatmap = f"""
📊 VOL STRENGTH HEATMAP

VIX: {data['vix']:.2f} {heatmap_symbol(vix_str)} ({vix_str}) / {heatmap_symbol(vix_smooth)} ({vix_smooth})
VVIX: {data['vvix']:.2f} {heatmap_symbol(vvix_str)} ({vvix_str}) / {heatmap_symbol(vvix_smooth)} ({vvix_smooth})
VX1-VX2 Spread: {changes['spread']:.3f} {heatmap_symbol(spread_str)} ({spread_str}) / {heatmap_symbol(spread_smooth)} ({spread_smooth})
SPX Change: {changes['spx_change']:.2f}% {heatmap_symbol(spx_str)} ({spx_str}) / {heatmap_symbol(spx_smooth)} ({spx_smooth})

Regime: {regime}
Probability VIX<18 (10d): {score}%

Action Guidance:
{guidance_text}

{charts}
"""
                send_telegram(heatmap)

            # Send weekly dashboard once per day
            now = datetime.now()
            if (now - last_weekly_alert).total_seconds() > 24*3600:
                send_weekly_dashboard()
                last_weekly_alert = now

            previous["regime"] = regime
            previous["vix"] = data["vix"]
            previous["vvix"] = data["vvix"]
            previous["spx"] = data["spx"]
            previous["spread"] = changes["spread"]

        except Exception as e:
            send_telegram(f"Scanner error: {e}")

        time.sleep(CHECK_INTERVAL)

#########################################
# START
#########################################

run()
