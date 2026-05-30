import os
import re
import sys
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

MX_TZ = ZoneInfo("America/Mexico_City")
import requests

HISTORY_FILE = "history.json"

API_URL = "https://boletopolis.com/api/v4/rpc"
URL_PATTERN = re.compile(r"/evento/(\d+)/funcion/(\d+)/")


def parse_url(url):
    match = URL_PATTERN.search(url)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def fetch_seat_data(event_id, funcion_id):
    payload = {
        "jsonrpc": "2.0",
        "method": "evento.tipos_boletos_comprar",
        "params": {"id": event_id, "id_funcion": funcion_id},
        "id": 1,
    }
    resp = requests.post(API_URL, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "Unknown API error"))
    return data["result"]


def format_date(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%A %d %B %Y, %I:%M %p")
    except (ValueError, TypeError):
        return date_str or "Unknown date"


def short_date(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%a %b %d, %I:%M %p")
    except (ValueError, TypeError):
        return date_str or "?"


def count_seats(result):
    total_physical = len(result.get("modelo", {}).get("asientos", []))
    evento = result.get("evento", {})
    event_name = evento.get("nombre", "Unknown Event")
    raw_date = evento.get("inicio", "")
    date = format_date(raw_date)
    date_short = short_date(raw_date)

    tipos = result.get("tipos_boletos", [])
    type_reports = []
    total_sellable = 0
    total_sold = 0
    total_waiting = 0

    for tipo in tipos:
        name = tipo.get("nombre", "Unknown")
        capacity = int(tipo.get("capacidad", 0))
        sold = 0
        waiting = 0

        for seat in tipo.get("asientos", []):
            if str(seat.get("ocupado")) == "1":
                sold += 1
            elif str(seat.get("esperando_pago")) == "1":
                waiting += 1

        type_reports.append({
            "name": name,
            "capacity": capacity,
            "sold": sold,
            "waiting": waiting,
            "available": capacity - sold - waiting,
        })
        total_sellable += capacity
        total_sold += sold
        total_waiting += waiting

    total_available = total_sellable - total_sold - total_waiting
    not_available = total_physical - total_sellable

    return {
        "event_name": event_name,
        "date": date,
        "date_short": date_short,
        "raw_date": raw_date,
        "total_physical": total_physical,
        "total_sellable": total_sellable,
        "total_sold": total_sold,
        "total_waiting": total_waiting,
        "total_available": total_available,
        "not_available": not_available,
        "types": type_reports,
    }


def print_report(data):
    pct = data["total_sold"] / data["total_sellable"] * 100 if data["total_sellable"] > 0 else 0
    print(f"\n  {data['event_name']} - {data['date']}")
    print(f"    Sold: {data['total_sold']}/{data['total_sellable']}  ({pct:.1f}%)    Available: {data['total_available']}")
    for t in data["types"]:
        t_pct = t["sold"] / t["capacity"] * 100 if t["capacity"] > 0 else 0
        line = f"      {t['name']:<20s}  {t['sold']}/{t['capacity']} sold ({t_pct:.1f}%)    Available: {t['available']}"
        if t["waiting"] > 0:
            line += f"    Waiting: {t['waiting']}"
        print(line)


def load_history():
    """Load history from JSON file. Returns list of snapshots."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def save_history(history, all_data, grand_sold):
    """Append current snapshot and prune entries older than 48 hours."""
    now = datetime.now(timezone.utc)
    snapshot = {
        "timestamp": now.isoformat(),
        "grand_sold": grand_sold,
        "per_function": {d["raw_date"]: d["total_sold"] for d in all_data},
    }
    history.append(snapshot)

    # Keep only last 48 hours to prevent file from growing forever
    cutoff = (now - timedelta(hours=48)).isoformat()
    history = [h for h in history if h["timestamp"] >= cutoff]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f)
    return history


def calc_sold_24h(history, all_data, grand_sold):
    """Calculate tickets sold in the last 24 hours using history."""
    if not history:
        return None, {}

    now = datetime.now(timezone.utc)
    target = now - timedelta(hours=24)

    # Find the snapshot closest to 24 hours ago
    best = None
    for h in history:
        ts = datetime.fromisoformat(h["timestamp"])
        if ts <= target:
            best = h

    if not best:
        # All history is within 24h, use the oldest entry
        best = history[0]

    grand_diff = grand_sold - best.get("grand_sold", grand_sold)
    per_function = {}
    old_funcs = best.get("per_function", {})
    for d in all_data:
        key = d["raw_date"]
        if key in old_funcs:
            per_function[key] = d["total_sold"] - old_funcs[key]
        else:
            per_function[key] = None  # No historical data for this function

    return max(0, grand_diff), per_function


def generate_html(all_data, grand_sold, grand_sellable, updated_at, sold_24h=None, sold_24h_per_func=None):
    grand_pct = grand_sold / grand_sellable * 100 if grand_sellable > 0 else 0
    grand_available = grand_sellable - grand_sold

    if sold_24h_per_func is None:
        sold_24h_per_func = {}

    now = datetime.now(MX_TZ).replace(tzinfo=None)
    future_data = []
    past_data = []
    for d in all_data:
        try:
            event_dt = datetime.strptime(d["raw_date"], "%Y-%m-%d %H:%M:%S")
            is_past = event_dt < now
        except (ValueError, TypeError):
            is_past = False
        (past_data if is_past else future_data).append(d)

    past_total_sold = sum(d["total_sold"] for d in past_data)
    past_total_available = sum(d["total_available"] for d in past_data)

    past_header_html = ""
    if past_data:
        past_header_html = f"""
        <div class="past-section-header">
            <span class="past-section-title">Past Events</span>
            <span class="past-section-totals">{past_total_sold} sold &nbsp;·&nbsp; {past_total_available} available</span>
        </div>"""

    rows_html = ""
    past_injected = False
    for d in future_data + past_data:
        pct = d["total_sold"] / d["total_sellable"] * 100 if d["total_sellable"] > 0 else 0
        is_past = d in past_data

        if is_past:
            if not past_injected:
                rows_html += past_header_html
                past_injected = True
            rows_html += f"""
        <div class="card card-past">
            <div class="card-header">
                <div class="card-date">{d["date_short"]}</div>
                <div class="card-pct">{pct:.0f}%</div>
            </div>
            <div class="card-stats-past">
                <span>{d["total_sold"]} sold</span>
                <span>{d["total_available"]} available</span>
                <span>{d["total_sellable"]} total</span>
            </div>
        </div>"""
            continue

        bar_color = "#c0392b" if pct >= 80 else "#e67e22" if pct >= 60 else "#f1c40f" if pct >= 40 else "#2e86de" if pct >= 20 else "#2ecc71"

        func_24h = sold_24h_per_func.get(d["raw_date"])
        badge_24h = ""
        if func_24h is not None and func_24h > 0:
            badge_24h = f'<span class="badge-24h">+{func_24h} today</span>'

        types_detail = ""
        for t in d["types"]:
            t_pct = t["sold"] / t["capacity"] * 100 if t["capacity"] > 0 else 0
            waiting_badge = f' <span class="badge-waiting">{t["waiting"]} waiting</span>' if t["waiting"] > 0 else ""
            types_detail += f"""
                <div class="type-row">
                    <span class="type-name">{t["name"]}</span>
                    <span class="type-stats">{t["sold"]}/{t["capacity"]} sold ({t_pct:.0f}%){waiting_badge}</span>
                    <span class="type-avail">{t["available"]} left</span>
                </div>"""

        rows_html += f"""
        <div class="card">
            <div class="card-header">
                <div class="card-date">{d["date_short"]} {badge_24h}</div>
                <div class="card-pct">{pct:.0f}%</div>
            </div>
            <div class="progress-bar">
                <div class="progress-fill" style="width: {pct}%; background: {bar_color};"></div>
            </div>
            <div class="card-stats">
                <div class="stat">
                    <div class="stat-num">{d["total_sold"]}</div>
                    <div class="stat-label">Sold</div>
                </div>
                <div class="stat">
                    <div class="stat-num">{d["total_available"]}</div>
                    <div class="stat-label">Available</div>
                </div>
                <div class="stat">
                    <div class="stat-num">{d["total_sellable"]}</div>
                    <div class="stat-label">Total</div>
                </div>
            </div>
            <div class="types-detail">{types_detail}
            </div>
        </div>"""

    event_name = all_data[0]["event_name"] if all_data else "Theatre Report"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{event_name} - Seat Report</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Uncial+Antiqua&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: linear-gradient(180deg, #c8d8a0 0%, #a8c070 100%);
            color: #1e2d0a;
            min-height: 100vh;
            padding: 20px;
        }}
        .container {{ max-width: 800px; margin: 0 auto; }}
        .header {{
            text-align: center;
            padding: 35px 0 10px;
        }}
        .header h1 {{
            font-family: 'Uncial Antiqua', Georgia, serif;
            font-size: 2.8em;
            font-weight: 400;
            color: #2d5a0e;
            margin-bottom: 4px;
            letter-spacing: 1px;
            text-shadow: 2px 2px 0px #a8c070, 0 0 20px rgba(45,90,14,0.3);
        }}
        .header .subtitle {{
            color: #5a7a30;
            font-size: 0.95em;
            font-weight: 500;
            letter-spacing: 2px;
            text-transform: uppercase;
        }}
        .summary {{
            background: linear-gradient(135deg, #2d5a0e, #4a7c1e);
            border-radius: 18px;
            padding: 28px;
            margin: 24px 0 30px;
            display: flex;
            justify-content: space-around;
            align-items: center;
            box-shadow: 0 8px 30px rgba(45, 90, 14, 0.4);
            border: 2px solid #8b6914;
        }}
        .summary-item {{ text-align: center; }}
        .summary-item .big {{
            font-size: 2.5em;
            font-weight: 700;
            color: #fff;
        }}
        .summary-item .big.pct {{ color: #f0d060; }}
        .summary-item .label {{
            font-size: 0.8em;
            color: rgba(255,255,255,0.75);
            margin-top: 4px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .cards {{ display: flex; flex-direction: column; gap: 14px; }}
        .card {{
            background: #f0f5e0;
            border-radius: 14px;
            padding: 20px 22px;
            border: 1px solid #8fb050;
            transition: transform 0.15s, box-shadow 0.15s;
            box-shadow: 0 2px 8px rgba(45,90,14,0.1);
        }}
        .card:hover {{ transform: translateY(-2px); box-shadow: 0 6px 20px rgba(45,90,14,0.2); }}
        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }}
        .card-date {{ font-size: 1.1em; font-weight: 600; color: #2d5a0e; }}
        .card-pct {{ font-size: 1.4em; font-weight: 700; color: #2d5a0e; }}
        .progress-bar {{
            height: 7px;
            background: #c8d8a0;
            border-radius: 4px;
            overflow: hidden;
            margin-bottom: 14px;
        }}
        .progress-fill {{
            height: 100%;
            border-radius: 4px;
            transition: width 0.5s;
        }}
        .card-stats {{
            display: flex;
            justify-content: space-around;
            margin-bottom: 10px;
        }}
        .stat {{ text-align: center; }}
        .stat-num {{ font-size: 1.3em; font-weight: 700; color: #2d5a0e; }}
        .stat-label {{ font-size: 0.72em; color: #5a7a30; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 500; }}
        .types-detail {{
            border-top: 1px solid #b8d080;
            padding-top: 10px;
            margin-top: 4px;
        }}
        .type-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 4px 0;
            font-size: 0.85em;
        }}
        .type-name {{ color: #4a6a20; min-width: 120px; font-weight: 500; }}
        .type-stats {{ color: #1e2d0a; flex: 1; text-align: center; }}
        .type-avail {{ color: #5a7a30; min-width: 70px; text-align: right; }}
        .past-section-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 18px 4px 6px;
            border-top: 1px solid #8fb050;
            margin-top: 8px;
        }}
        .past-section-title {{
            font-size: 0.75em;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            color: #7a9a50;
        }}
        .past-section-totals {{
            font-size: 0.82em;
            color: #7a9a50;
            font-weight: 500;
        }}
        .card-past {{
            background: #e0e8c8;
            border-color: #b0c880;
            box-shadow: none;
        }}
        .card-past:hover {{ transform: none; box-shadow: none; }}
        .card-past .card-date {{ color: #7a9a50; font-weight: 500; }}
        .card-past .card-pct {{ color: #8aaa60; font-size: 1.1em; }}
        .card-stats-past {{
            display: flex;
            gap: 20px;
            font-size: 0.85em;
            color: #7a9a50;
            margin-top: 2px;
        }}
        .badge-waiting {{
            background: #8b6914;
            color: #fff8e0;
            padding: 1px 6px;
            border-radius: 8px;
            font-size: 0.8em;
            font-weight: 600;
        }}
        .badge-24h {{
            background: #4a7c1e;
            color: #e0f0b0;
            padding: 2px 8px;
            border-radius: 8px;
            font-size: 0.7em;
            font-weight: 600;
            margin-left: 8px;
            vertical-align: middle;
        }}
        .footer {{
            text-align: center;
            padding: 30px 0 10px;
            color: #5a7a30;
            font-size: 0.8em;
        }}
        @media (max-width: 500px) {{
            body {{ padding: 12px; }}
            .header h1 {{ font-size: 2em; }}
            .summary {{ flex-wrap: wrap; gap: 15px; padding: 20px; }}
            .summary-item .big {{ font-size: 1.8em; }}
            .card {{ padding: 14px 16px; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{event_name}</h1>
            <div class="subtitle">Seat Availability Report</div>
        </div>

        <div class="summary">
            <div class="summary-item">
                <div class="big pct">{grand_pct:.0f}%</div>
                <div class="label">Overall Sold</div>
            </div>
            <div class="summary-item">
                <div class="big">{grand_sold}</div>
                <div class="label">Tickets Sold</div>
            </div>
            <div class="summary-item">
                <div class="big">{grand_available}</div>
                <div class="label">Still Available</div>
            </div>
            <div class="summary-item">
                <div class="big">{len(all_data)}</div>
                <div class="label">Functions</div>
            </div>
            <div class="summary-item">
                <div class="big">{f"+{sold_24h}" if sold_24h is not None else "--"}</div>
                <div class="label">Last 24h</div>
            </div>
        </div>


        <div class="cards">
            {rows_html}
        </div>

        <div class="footer">
            Last updated: {updated_at}<br>
            Data sourced from Boletopolis
        </div>
    </div>
</body>
</html>"""
    return html


def main():
    links_file = "links.txt"
    html_output = None

    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--html":
            html_output = sys.argv[i + 1] if i + 1 < len(sys.argv) else "index.html"
            i += 2
        else:
            links_file = sys.argv[i]
            i += 1

    try:
        with open(links_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"Error: '{links_file}' not found. Create it with one URL per line.")
        sys.exit(1)

    urls = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    if not urls:
        print(f"No URLs found in '{links_file}'. Add one URL per line.")
        sys.exit(1)

    print(f"Processing {len(urls)} URL(s)...\n")
    print("=" * 60)

    grand_sold = 0
    grand_sellable = 0
    all_data = []

    for url in urls:
        event_id, funcion_id = parse_url(url)
        if not event_id:
            print(f"\n  [SKIP] Invalid URL: {url}")
            continue

        try:
            result = fetch_seat_data(event_id, funcion_id)
            data = count_seats(result)
            print_report(data)
            all_data.append(data)
            grand_sold += data["total_sold"]
            grand_sellable += data["total_sellable"]
        except RuntimeError as e:
            print(f"\n  [ERROR] {url}\n    API error: {e}")
        except requests.RequestException as e:
            print(f"\n  [ERROR] {url}\n    Connection error: {e}")

    print(f"\n{'=' * 60}")
    grand_available = grand_sellable - grand_sold
    grand_pct = grand_sold / grand_sellable * 100 if grand_sellable > 0 else 0
    print(f"  TOTAL across {len(all_data)} functions:")
    print(f"    Sold:      {grand_sold}/{grand_sellable}  ({grand_pct:.1f}%)")
    print(f"    Missing:   {grand_available}")
    print(f"{'=' * 60}")

    if html_output and all_data:
        # Load history, calculate 24h change, save new snapshot
        history = load_history()
        sold_24h, sold_24h_per_func = calc_sold_24h(history, all_data, grand_sold)
        history = save_history(history, all_data, grand_sold)

        if sold_24h is not None:
            print(f"\n  Sold in last 24h: +{sold_24h}")

        updated_at = datetime.now(MX_TZ).strftime("%Y-%m-%d %H:%M Mexico City Time")
        html = generate_html(all_data, grand_sold, grand_sellable, updated_at, sold_24h, sold_24h_per_func)
        with open(html_output, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nHTML report saved to: {html_output}")


if __name__ == "__main__":
    main()
