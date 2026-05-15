import time
import threading
import logging
import requests
from flask import Flask, jsonify
from flask_cors import CORS

# ─── Configuration ────────────────────────────────────────────────────────────

EVENT_SLUG     = "2026-fifa-world-cup-winner-595"
GAMMA_EVENTS   = "https://gamma-api.polymarket.com/events"
REFRESH_SECS   = 60          # How often to re-fetch from Polymarket (seconds)
PORT           = 5000        # Local server port

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ─── Flask App ────────────────────────────────────────────────────────────────

app = Flask(__name__)
CORS(app)   # Allow cross-origin requests from Shopify

# ─── Shared cache ─────────────────────────────────────────────────────────────

_cache_lock   = threading.Lock()
_cached_data  = None          # Last successfully parsed payload
_last_fetched = 0             # Unix timestamp of last successful fetch
_last_error   = None          # Last error string (shown to client if cache empty)

# ─── Data fetching logic ───────────────────────────────────────────────────────

def _fetch_and_parse() -> dict:
    """
    Hit the Polymarket Gamma API, return cleaned team data.
    Raises on any error so the caller can handle gracefully.
    """
    url    = GAMMA_EVENTS
    params = {"slug": EVENT_SLUG}
    resp   = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()

    events = resp.json()
    if not events:
        raise ValueError(f"No event found for slug '{EVENT_SLUG}'")

    event   = events[0]
    markets = event.get("markets", [])

    teams = []
    for m in markets:
        # Skip inactive / closed / "Team XX" placeholder entries
        if not m.get("active"):
            continue
        if m.get("closed"):
            continue
        title = m.get("groupItemTitle", "").strip()
        if not title or title.startswith("Team ") or title == "Other":
            continue

        # outcomePrices is "[\"0.1525\", \"0.8475\"]" – we want index-0 (Yes price)
        raw_prices = m.get("outcomePrices", '["0","1"]')
        try:
            import json as _json
            prices = _json.loads(raw_prices)
            yes_price = float(prices[0])
        except Exception:
            yes_price = 0.0

        probability_pct = round(yes_price * 100, 2)

        volume_24h  = m.get("volume24hr",  0) or 0
        volume_total = m.get("volume",    0) or 0
        liquidity   = m.get("liquidity",  0) or 0
        status      = "CLOSED" if m.get("closed") else ("OPEN" if m.get("active") else "INACTIVE")

        best_bid    = m.get("bestBid",  0) or 0
        best_ask    = m.get("bestAsk",  0) or 0
        last_price  = m.get("lastTradePrice", 0) or 0

        week_change = m.get("oneWeekPriceChange",  None)
        month_change = m.get("oneMonthPriceChange", None)

        teams.append({
            "team":            title,
            "probability_pct": probability_pct,         # e.g. 15.25
            "last_price":      round(last_price, 4),    # USDC price (0–1)
            "best_bid":        round(best_bid,   4),
            "best_ask":        round(best_ask,   4),
            "week_change_pct": round(week_change  * 100, 2) if week_change  is not None else None,
            "month_change_pct": round(month_change * 100, 2) if month_change is not None else None,
            "volume_24h":      round(volume_24h,   2),
            "volume_total":    round(float(volume_total), 2),
            "liquidity":       round(float(liquidity), 2),
            "status":          status,
            "market_url":      f"https://polymarket.com/event/{EVENT_SLUG}",
            "image":           m.get("image", ""),
        })

    # Sort by probability descending → natural ranking
    teams.sort(key=lambda t: t["probability_pct"], reverse=True)

    # Add rank field
    for i, t in enumerate(teams, start=1):
        t["rank"] = i

    return {
        "event_title":    event.get("title", "2026 FIFA World Cup Winner").strip(),
        "event_status":   "CLOSED" if event.get("closed") else "OPEN",
        "total_volume":   round(float(event.get("volume", 0)), 2),
        "total_liquidity": round(float(event.get("liquidity", 0)), 2),
        "volume_24h":     round(float(event.get("volume24hr", 0)), 2),
        "last_updated":   time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source":         "Polymarket",
        "teams":          teams,
    }


def _refresh_cache():
    """Fetch data and update the cache. Called from background thread."""
    global _cached_data, _last_fetched, _last_error
    try:
        data = _fetch_and_parse()
        with _cache_lock:
            _cached_data  = data
            _last_fetched = time.time()
            _last_error   = None
        log.info("Cache refreshed – %d active teams loaded.", len(data["teams"]))
    except Exception as exc:
        with _cache_lock:
            _last_error = str(exc)
        log.error("Failed to refresh cache: %s", exc)


def _background_loop():
    """Runs forever in a daemon thread, refreshing the cache periodically."""
    while True:
        _refresh_cache()
        time.sleep(REFRESH_SECS)

# ─── Flask Routes ──────────────────────────────────────────────────────────────

@app.route("/api/worldcup", methods=["GET"])
def worldcup():
    with _cache_lock:
        data  = _cached_data
        error = _last_error
        age   = int(time.time() - _last_fetched) if _last_fetched else None

    if data is None:
        status_code = 503
        payload = {
            "error": "Data not yet available. Please try again in a moment.",
            "detail": error,
        }
    else:
        payload = dict(data)
        payload["cache_age_seconds"] = age
        status_code = 200

    return jsonify(payload), status_code


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket World Cup — Live Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#0d1117;color:#e6edf3;min-height:100vh}
:root{--green:#00e676;--red:#ff5252;--accent:#58a6ff;--gold:#ffd700;--silver:#c0c0c0;--bronze:#cd7f32;--card:#161b22;--border:#30363d;--muted:#8b949e}

/* ── TOP BAR ── */
.topbar{background:linear-gradient(135deg,#161b22,#1f2937);border-bottom:1px solid var(--border);padding:16px 32px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.logo{font-size:1.25rem;font-weight:800;display:flex;align-items:center;gap:10px}
.logo-icon{font-size:1.6rem}
.badge-live{font-size:.65rem;font-weight:700;background:rgba(0,230,118,.15);color:var(--green);border:1px solid var(--green);border-radius:20px;padding:3px 10px;letter-spacing:.5px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.server-info{font-size:.75rem;color:var(--muted);text-align:right;line-height:1.8}
.server-info span{color:var(--accent)}

/* ── LAYOUT ── */
.container{max-width:1100px;margin:0 auto;padding:28px 20px}

/* ── STATUS CARDS ── */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:28px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 20px;transition:border-color .2s}
.stat-card:hover{border-color:var(--accent)}
.stat-label{font-size:.68rem;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:6px}
.stat-value{font-size:1.35rem;font-weight:700}
.stat-sub{font-size:.72rem;color:var(--muted);margin-top:4px}

/* ── SERVER STATUS ── */
.status-row{display:flex;align-items:center;gap:8px;margin-bottom:28px;padding:14px 20px;background:var(--card);border:1px solid var(--border);border-radius:12px;flex-wrap:wrap}
.dot{width:10px;height:10px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);flex-shrink:0}
.dot.err{background:var(--red);box-shadow:0 0 8px var(--red)}
.status-text{font-size:.85rem;font-weight:600}
.status-detail{font-size:.75rem;color:var(--muted);margin-left:auto}

/* ── TABLE SECTION ── */
.section-title{font-size:1rem;font-weight:700;margin-bottom:14px;display:flex;align-items:center;gap:10px}
.section-title small{font-size:.72rem;font-weight:400;color:var(--muted)}
.table-wrap{overflow-x:auto;border-radius:12px;border:1px solid var(--border)}
table{width:100%;border-collapse:collapse;font-size:.83rem}
thead th{padding:11px 14px;text-align:left;font-size:.67rem;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);background:#0d1117;white-space:nowrap;border-bottom:1px solid var(--border)}
tbody td{padding:11px 14px;border-bottom:1px solid rgba(48,54,61,.5);vertical-align:middle;white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
tbody tr{transition:background .15s}
tbody tr:hover{background:rgba(88,166,255,.05)}

/* ── RANK ── */
.rank{width:30px;height:30px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-weight:700;font-size:.82rem}
.r1{background:rgba(255,215,0,.18);color:var(--gold);border:1px solid var(--gold)}
.r2{background:rgba(192,192,192,.18);color:var(--silver);border:1px solid var(--silver)}
.r3{background:rgba(205,127,50,.18);color:var(--bronze);border:1px solid var(--bronze)}
.rx{background:rgba(139,148,158,.1);color:var(--muted);border:1px solid var(--border)}

/* ── PROB BAR ── */
.prob-num{font-weight:700;color:var(--accent)}
.bar-bg{height:5px;background:rgba(255,255,255,.07);border-radius:3px;margin-top:4px;width:100px}
.bar-fill{height:5px;border-radius:3px;background:linear-gradient(90deg,#58a6ff,#00e676);transition:width .6s}

/* ── CHANGE ── */
.chg{font-size:.73rem;font-weight:600;padding:2px 8px;border-radius:20px}
.up{background:rgba(0,230,118,.15);color:var(--green)}
.dn{background:rgba(255,82,82,.15);color:var(--red)}
.fl{background:rgba(139,148,158,.1);color:var(--muted)}

/* ── PILL ── */
.pill{font-size:.65rem;font-weight:700;padding:2px 9px;border-radius:20px;letter-spacing:.3px}
.open{background:rgba(0,230,118,.12);color:var(--green);border:1px solid rgba(0,230,118,.3)}
.closed{background:rgba(255,82,82,.12);color:var(--red);border:1px solid rgba(255,82,82,.3)}

/* ── TEAM CELL ── */
.team-cell{display:flex;align-items:center;gap:9px;font-weight:600}
.flag{width:26px;height:18px;border-radius:3px;object-fit:cover}

/* ── API BOX ── */
.api-box{margin-top:28px;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px 24px}
.api-box h3{font-size:.9rem;font-weight:700;margin-bottom:12px;color:var(--accent)}
.api-box code{display:block;background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:12px 16px;font-size:.82rem;color:var(--green);word-break:break-all}
.api-note{font-size:.72rem;color:var(--muted);margin-top:10px;line-height:1.7}

/* ── FOOTER ── */
.footer{text-align:center;padding:24px;font-size:.72rem;color:var(--muted)}
.footer a{color:var(--accent);text-decoration:none}

/* ── LOADER ── */
#loading{text-align:center;padding:60px;color:var(--muted)}
</style>
</head>
<body>

<div class="topbar">
  <div class="logo">
    <span class="logo-icon">⚽</span>
    Polymarket World Cup Dashboard
    <span class="badge-live">LIVE</span>
  </div>
  <div class="server-info">
    Server: <span>http://localhost:5000</span><br>
    API: <span>/api/worldcup</span>&nbsp;·&nbsp;Auto-refresh: <span>60s</span>
  </div>
</div>

<div class="container">

  <div class="status-row" id="status-row">
    <div class="dot" id="dot"></div>
    <div class="status-text" id="status-text">Connecting to Polymarket…</div>
    <div class="status-detail" id="status-detail"></div>
  </div>

  <div class="stat-grid" id="stat-grid">
    <div class="stat-card"><div class="stat-label">Total Volume</div><div class="stat-value" id="s-vol">—</div><div class="stat-sub">All-time on Polymarket</div></div>
    <div class="stat-card"><div class="stat-label">Total Liquidity</div><div class="stat-value" id="s-liq">—</div><div class="stat-sub">Available liquidity</div></div>
    <div class="stat-card"><div class="stat-label">24h Volume</div><div class="stat-value" id="s-24h">—</div><div class="stat-sub">Last 24 hours</div></div>
    <div class="stat-card"><div class="stat-label">Teams Tracked</div><div class="stat-value" id="s-teams">—</div><div class="stat-sub">Active markets</div></div>
    <div class="stat-card"><div class="stat-label">Market Status</div><div class="stat-value" id="s-status">—</div><div class="stat-sub">Polymarket event</div></div>
    <div class="stat-card"><div class="stat-label">Last Refresh</div><div class="stat-value" id="s-refresh" style="font-size:1rem">—</div><div class="stat-sub">Cache age</div></div>
  </div>

  <div class="section-title">
    🏆 Team Rankings &amp; Win Probabilities
    <small id="subtitle">Loading…</small>
  </div>

  <div id="loading">Loading live data from Polymarket…</div>

  <div class="table-wrap" id="table-wrap" style="display:none">
    <table>
      <thead>
        <tr>
          <th>#</th><th>Team</th><th>Win Probability</th>
          <th>Last Price</th><th>Bid / Ask</th><th>7d Chg</th>
          <th>24h Volume</th><th>Liquidity</th><th>Status</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </div>

  <div class="api-box">
    <h3>📡 Shopify Integration Endpoint</h3>
    <code>GET http://localhost:5000/api/worldcup</code>
    <div class="api-note">
      ✅ <strong>This server is DATA-ONLY</strong> — it fetches and displays Polymarket market probabilities.<br>
      ❌ <strong>No trading</strong> — no wallet, no orders, no MetaMask needed.<br>
      🛒 Paste <code>shopify_widget.html</code> into your Shopify theme and set <code>API_URL</code> to your live server URL.
    </div>
  </div>

</div>

<div class="footer">
  Data from <a href="https://polymarket.com/event/2026-fifa-world-cup-winner-595" target="_blank">Polymarket</a> &nbsp;·&nbsp;
  Read-only display widget &nbsp;·&nbsp;
  Not financial advice
</div>

<script>
var API = "/api/worldcup";
var maxProb = 1;

function usd(n){if(!n)return"$0";n=Number(n);if(n>=1e6)return"$"+(n/1e6).toFixed(1)+"M";if(n>=1e3)return"$"+(n/1e3).toFixed(1)+"K";return"$"+n.toFixed(2)}
function pct(n,d){if(n==null)return"—";return Number(n).toFixed(d||2)+"%"}
function chg(v){
  if(v==null)return'<span class="chg fl">—</span>';
  var c=v>0?"up":v<0?"dn":"fl", s=v>0?"▲ +":v<0?"▼ ":"";
  return'<span class="chg '+c+'">'+s+pct(v)+'</span>';
}
function rank(r){
  var c=r===1?"r1":r===2?"r2":r===3?"r3":"rx";
  return'<span class="rank '+c+'">'+r+'</span>';
}

function render(d){
  var teams=d.teams||[];
  maxProb=teams.length?teams[0].probability_pct:1;

  // Status row
  document.getElementById("dot").className="dot";
  document.getElementById("status-text").textContent="Connected to Polymarket — Data live";
  document.getElementById("status-detail").textContent="Cache age: "+(d.cache_age_seconds||0)+"s | Last updated: "+new Date().toLocaleTimeString();

  // Stats
  document.getElementById("s-vol").textContent=usd(d.total_volume);
  document.getElementById("s-liq").textContent=usd(d.total_liquidity);
  document.getElementById("s-24h").textContent=usd(d.volume_24h);
  document.getElementById("s-teams").textContent=teams.length;
  var sp=d.event_status==="OPEN";
  document.getElementById("s-status").innerHTML='<span class="pill '+(sp?"open":"closed")+'">'+(d.event_status||"—")+'</span>';
  document.getElementById("s-refresh").textContent=(d.cache_age_seconds||0)+"s ago";
  document.getElementById("subtitle").textContent=teams.length+" active teams · sorted by win probability";

  // Table rows
  var rows=teams.map(function(t){
    var barW=maxProb>0?Math.round(t.probability_pct/maxProb*100):0;
    var isOpen=t.status==="OPEN";
    var img=t.image?'<img class="flag" src="'+t.image+'" alt="'+t.team+'" loading="lazy">':'';
    return'<tr>'+
      '<td>'+rank(t.rank)+'</td>'+
      '<td><div class="team-cell">'+img+'<span>'+t.team+'</span></div></td>'+
      '<td><span class="prob-num">'+pct(t.probability_pct)+'</span>'+
        '<div class="bar-bg"><div class="bar-fill" style="width:'+barW+'%"></div></div></td>'+
      '<td>$'+Number(t.last_price).toFixed(4)+'</td>'+
      '<td>$'+Number(t.best_bid).toFixed(3)+' / $'+Number(t.best_ask).toFixed(3)+'</td>'+
      '<td>'+chg(t.week_change_pct)+'</td>'+
      '<td>'+usd(t.volume_24h)+'</td>'+
      '<td>'+usd(t.liquidity)+'</td>'+
      '<td><span class="pill '+(isOpen?"open":"closed")+'">'+t.status+'</span></td>'+
    '</tr>';
  }).join("");

  document.getElementById("tbody").innerHTML=rows;
  document.getElementById("loading").style.display="none";
  document.getElementById("table-wrap").style.display="block";
}

function fetchData(){
  fetch(API).then(function(r){return r.json();}).then(function(d){
    if(d.error){throw new Error(d.error);}
    render(d);
  }).catch(function(e){
    document.getElementById("dot").className="dot err";
    document.getElementById("status-text").textContent="Error: "+e.message;
  });
}

fetchData();
setInterval(fetchData,60000);
</script>
</body>
</html>"""

@app.route("/", methods=["GET"])
def index():
    return DASHBOARD_HTML

# ─── Startup (runs for BOTH gunicorn and direct python) ───────────────────────
# NOTE: if __name__ == "__main__" is NOT called by gunicorn.
# We must initialize the cache and background thread at module level.

log.info("Starting initial data fetch …")
_refresh_cache()                          # Populate cache immediately on import

_bg_thread = threading.Thread(target=_background_loop, daemon=True)
_bg_thread.start()
log.info("Background refresh thread started (interval: %ds).", REFRESH_SECS)

# ─── Entry point (direct python run only) ─────────────────────────────────────

if __name__ == "__main__":
    log.info("Server running on http://0.0.0.0:%d", PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
