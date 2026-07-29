import pandas as pd
import numpy as np
from pybit.unified_trading import HTTP
import os
import time
import sys
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============================================================
# LOG SERVER — akses via https://xxx.up.railway.app/logs
# ============================================================
LOG_FILE = "bot.log"
ENTRY_FILE = "entries.log"   # khusus catatan entry — TIDAK tergulung oleh log monitoring

def log_entry(text):
    """Catat entry ke entries.log (permanen, tak tergulung) DAN ke /logs."""
    import datetime
    ts = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)).strftime('[%Y-%m-%d %H:%M:%S] ')
    try:
        with open(ENTRY_FILE, 'a', encoding='utf-8') as f:
            f.write(ts + text.replace('\n', '\n' + ' ' * len(ts)) + '\n')
    except Exception:
        pass
    print(text)   # juga muncul di /logs

class _Tee:
    """Redirect print() ke stdout DAN file sekaligus, dengan timestamp WIB per baris."""
    def __init__(self):
        self._out     = sys.__stdout__
        self._file    = open(LOG_FILE, 'a', buffering=1, encoding='utf-8')
        self._newline = True
    def write(self, msg):
        import datetime
        out = ''
        for ch in msg:
            if self._newline and ch != '\n':
                out += (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)).strftime('[%H:%M:%S] ')
                self._newline = False
            out += ch
            if ch == '\n':
                self._newline = True
        self._out.write(out)
        self._file.write(out)
    def flush(self):
        self._out.flush()
        self._file.flush()

sys.stdout = _Tee()

LAST_OHLC = {}   # (symbol, interval) -> df OHLC terakhir (diunduh via /ohlc utk diagnostik)


def _parse_log_blocks(text):
    """Pecah isi entries.log jadi blok-blok (1 blok = 1 pemanggilan log_entry, bisa multi-baris
    dgn baris lanjutan yg diindentasi). Tiap blok ditandai koin apa yg disebut di dalamnya
    (simbol pertama yg match pola HURUF+ANGKA+USDT), utk dipakai filter per-koin di /view."""
    import re
    ts_re   = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] ?')
    coin_re = re.compile(r'\b([A-Z0-9]{2,15}USDT)\b')
    blocks, cur = [], None
    for line in text.split('\n'):
        m = ts_re.match(line)
        if m:
            if cur is not None:
                blocks.append(cur)
            cur = {'ts': m.group(1), 'lines': [line]}
        elif cur is not None:
            cur['lines'].append(line)
    if cur is not None:
        blocks.append(cur)
    out = []
    for b in blocks:
        block_text = '\n'.join(b['lines']).rstrip('\n')
        cm = coin_re.search(block_text)
        out.append({'ts': b['ts'], 'coin': (cm.group(1) if cm else None), 'text': block_text})
    return out

class _LogHandler(BaseHTTPRequestHandler):
    def _send(self, body, ctype='text/plain; charset=utf-8', extra=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Access-Control-Allow-Origin', '*')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        import datetime as _dt
        path = self.path.split('?', 1)[0]
        query = {}
        if '?' in self.path:
            for kv in self.path.split('?', 1)[1].split('&'):
                if '=' in kv:
                    k, v = kv.split('=', 1); query[k] = v

        if path == '/entries':
            try:
                with open(ENTRY_FILE, 'r', encoding='utf-8') as f:
                    data = f.read()
            except Exception:
                data = '(belum ada entry)'
            return self._send(data)

        if path == '/view':
            import json
            try:
                with open(ENTRY_FILE, 'r', encoding='utf-8') as f:
                    raw = f.read()
            except Exception:
                raw = ''
            blocks = _parse_log_blocks(raw)
            # Aktivitas terakhir per koin (blok sudah urut kronologis di file -> yg terakhir ditulis = terbaru)
            coin_last_ts = {}
            for b in blocks:
                if b['coin']:
                    coin_last_ts[b['coin']] = b['ts']
            # Urut: aktivitas TERBARU paling atas ("kalau ada aktivitas terbaru, jadi paling atas")
            coins_sorted = sorted(coin_last_ts.keys(), key=lambda c: coin_last_ts[c], reverse=True)
            html = ("<!doctype html><html><head><meta charset='utf-8'>"
                    "<meta name='viewport' content='width=device-width, initial-scale=1, maximum-scale=1'>"
                    "<title>Bot Log</title>"
                    "<style>"
                    "*{box-sizing:border-box}"
                    "html,body{width:100%;overflow-x:hidden}"
                    "body{font-family:'Courier New',monospace;background:#0d0d0d;color:#ddd;margin:0;padding:0;"
                    "font-size:13px}"
                    ".topbar{display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding:8px 10px;"
                    "background:#181818;border-bottom:1px solid #333;position:sticky;top:0;z-index:2}"
                    ".tabbtn{background:#222;color:#ccc;border:1px solid #444;border-radius:6px;padding:8px 14px;"
                    "cursor:pointer;font-size:13px;flex:0 0 auto}"
                    ".tabbtn.active{background:#2a6;color:#fff;border-color:#2a6}"
                    ".minilinks{display:flex;gap:10px;margin-left:auto;flex-wrap:wrap}"
                    "a.mini{color:#7ad;text-decoration:none;font-size:12px;white-space:nowrap}"
                    ".wrap{display:flex;flex-direction:column;min-height:calc(100vh - 48px)}"
                    "@media(min-width:700px){.wrap{flex-direction:row;height:calc(100vh - 48px)}}"
                    ".sidebar{display:none;border-bottom:1px solid #333;background:#151515;"
                    "max-height:38vh;overflow-y:auto}"
                    "@media(min-width:700px){.sidebar{max-height:none;height:100%;width:180px;"
                    "border-bottom:none;border-right:1px solid #333;flex:0 0 180px}}"
                    ".sidebar.show{display:block}"
                    ".coinbtn{display:block;width:100%;text-align:left;background:none;border:none;color:#ccc;"
                    "padding:10px 14px;cursor:pointer;font-size:13px;border-bottom:1px solid #222}"
                    ".coinbtn:active,.coinbtn:hover{background:#222}"
                    ".coinbtn.active{background:#26a;color:#fff}"
                    ".main{flex:1;overflow-y:auto;overflow-x:hidden;padding:8px 10px;white-space:pre-wrap;"
                    "word-break:break-word;font-size:12px;line-height:1.5;-webkit-overflow-scrolling:touch}"
                    ".blk{padding:5px 0;border-bottom:1px solid #1c1c1c}"
                    "@media(min-width:700px){.main{font-size:13px;padding:10px 16px}}"
                    "</style></head><body>"
                    "<div class='topbar'>"
                    "<button id='tab-semua' class='tabbtn active' onclick=\"setTab('semua')\">Semua</button>"
                    "<button id='tab-percoin' class='tabbtn' onclick=\"setTab('percoin')\">Per Koin</button>"
                    "<div class='minilinks'>"
                    "<a class='mini' href='/entries'>raw</a>"
                    "<a class='mini' href='/logs'>console</a>"
                    "<a class='mini' href='/ohlc'>ohlc</a>"
                    "</div></div>"
                    "<div class='wrap'>"
                    "<div id='sidebar' class='sidebar'></div>"
                    "<div id='main' class='main'></div>"
                    "</div>"
                    "<script>"
                    f"const BLOCKS = {json.dumps(blocks)};"
                    f"const COINS = {json.dumps(coins_sorted)};"
                    "let mode='semua', selCoin=null;"
                    "function render(){"
                    "  const main=document.getElementById('main');"
                    "  const sidebar=document.getElementById('sidebar');"
                    "  document.getElementById('tab-semua').className='tabbtn'+(mode==='semua'?' active':'');"
                    "  document.getElementById('tab-percoin').className='tabbtn'+(mode==='percoin'?' active':'');"
                    "  if(mode==='semua'){"
                    "    sidebar.className='sidebar';"
                    "    main.innerHTML=BLOCKS.map(b=>'<div class=\"blk\">'+esc(b.text)+'</div>').join('');"
                    "  } else {"
                    "    sidebar.className='sidebar show';"
                    "    sidebar.innerHTML=COINS.map(c=>'<button class=\"coinbtn'+(c===selCoin?' active':'')+'\" "
                    "onclick=\"selectCoin(\\''+c+'\\')\">'+c+'</button>').join('');"
                    "    if(!selCoin){main.innerHTML='<i>Pilih koin di atas/kiri.</i>';}"
                    "    else{"
                    "      const filtered=BLOCKS.filter(b=>b.coin===selCoin);"
                    "      main.innerHTML=filtered.length?filtered.map(b=>'<div class=\"blk\">'+esc(b.text)+'</div>').join('')"
                    "        :'<i>Belum ada log untuk '+selCoin+'.</i>';"
                    "    }"
                    "  }"
                    "  main.scrollTop=main.scrollHeight;"
                    "}"
                    "function esc(s){const d=document.createElement('div');d.innerText=s;return d.innerHTML;}"
                    "function setTab(m){mode=m;render();}"
                    "function selectCoin(c){selCoin=c;render();}"
                    "render();"
                    "</script></body></html>")
            return self._send(html, 'text/html; charset=utf-8')

        if path == '/logs':
            try:
                with open(LOG_FILE, 'r', encoding='utf-8') as f:
                    data = ''.join(f.readlines()[-200:])
            except Exception:
                data = ''
            return self._send(data)

        # ---- Unduh OHLC (diagnostik): /ohlc (halaman tombol) atau /ohlc?symbol=X&tf=60 (CSV) ----
        if path == '/ohlc':
            sym = query.get('symbol'); tf = query.get('tf', '60')
            if sym:   # unduh CSV
                df = LAST_OHLC.get((sym, str(tf)))
                if df is None:
                    return self._send(f"(data {sym} tf{tf} belum ada — tunggu bot scan dulu)")
                rows = ["ts_ms,waktu_WIB,open,high,low,close,volume"]
                for _, r in df.iterrows():
                    t = _dt.datetime.utcfromtimestamp(int(r['ts']) / 1000) + _dt.timedelta(hours=7)
                    rows.append(f"{int(r['ts'])},{t:%Y-%m-%d %H:%M:%S},"
                                f"{r['open']:.10g},{r['high']:.10g},{r['low']:.10g},{r['close']:.10g},{r.get('vol',0):.10g}")
                csv = "\n".join(rows)
                fname = f"{sym}_tf{tf}_{_dt.datetime.utcnow():%Y%m%d_%H%M}.csv"
                return self._send(csv, 'text/csv; charset=utf-8',
                                  {'Content-Disposition': f'attachment; filename="{fname}"'})
            # halaman tombol
            keys = sorted(LAST_OHLC.keys())
            if not keys:
                return self._send("<h3>Belum ada data. Tunggu bot scan beberapa detik lalu refresh.</h3>"
                                  "<a href='/ohlc'>refresh</a>", 'text/html; charset=utf-8')
            syms = sorted({k[0] for k in keys})
            html = ["<html><head><meta charset='utf-8'><title>Unduh OHLC</title>",
                    "<style>body{font-family:sans-serif;background:#111;color:#eee;padding:16px}"
                    "a.btn{display:inline-block;margin:3px;padding:6px 10px;background:#2a6;color:#fff;"
                    "text-decoration:none;border-radius:5px}a.btn.m5{background:#26a}h4{margin:14px 0 4px}</style></head><body>",
                    "<h2>Unduh OHLC (data yg dilihat bot)</h2>",
                    "<p>Klik untuk unduh CSV (ts epoch + waktu WIB + OHLC). Kirim file-nya ke Claude untuk cek break/choch.</p>",
                    "<p><a href='/logs'>/logs</a> · <a href='/entries'>/entries</a> · <a href='/ohlc'>refresh</a></p>"]
            for s in syms:
                html.append(f"<h4>{s}</h4>")
                if (s, '60') in LAST_OHLC:
                    html.append(f"<a class='btn' href='/ohlc?symbol={s}&tf=60'>⬇ H1 (60m)</a>")
                if (s, '5') in LAST_OHLC:
                    html.append(f"<a class='btn m5' href='/ohlc?symbol={s}&tf=5'>⬇ M5</a>")
            html.append("</body></html>")
            return self._send("\n".join(html), 'text/html; charset=utf-8')

        if path == '/':
            return self._send("<html><body style='font-family:sans-serif;background:#111;color:#eee;padding:16px'>"
                              "<h2>SMC bot</h2><p><a href='/logs' style='color:#6cf'>/logs</a> · "
                              "<a href='/entries' style='color:#6cf'>/entries</a> · "
                              "<a href='/ohlc' style='color:#6cf'><b>/ohlc — unduh data OHLC</b></a></p></body></html>",
                              'text/html; charset=utf-8')

        self.send_response(404); self.end_headers()

    def log_message(self, *a):
        pass

PORT = int(os.environ.get('PORT', 8080))
threading.Thread(
    target=lambda: HTTPServer(('0.0.0.0', PORT), _LogHandler).serve_forever(),
    daemon=True
).start()
print(f"📡 Log server jalan di port {PORT} → /logs")

# ============================================================
# CONFIG
# ============================================================
API_KEY    = os.environ.get('API_KEY', '')
API_SECRET = os.environ.get('API_SECRET', '')
CATEGORY   = "linear"
TESTNET    = os.environ.get('TESTNET', 'false').lower() == 'true'

if not API_KEY or not API_SECRET:
    raise ValueError("❌ API_KEY dan API_SECRET belum diset!")

session = HTTP(testnet=TESTNET, api_key=API_KEY, api_secret=API_SECRET)

# ── Strategy params (sinkron dengan backtest.py) ─────────────
SL_MULT          = 6.2    # SL = SL_MULT × gap_size dari entry (fallback)
TRAIL_STOP       = 1.0    # trailing distance = TRAIL_STOP × dist (sinkron backtest Trail=0.5R)
TRAIL_ACT_R      = 9.0    # trail aktif setelah +TRAIL_ACT_R (Bybit min > trailingStop)
TRAIL_TIMEOUT_DAYS = 3    # close posisi jika peak tidak bergerak selama N hari (sinkron backtest)
USE_TP           = False  # False = trailing stop AKTIF (TP fix dimatikan)
RR_TP            = 9.0    # TP di 1:RR_TP (4.0 = 1:4)
RISK_PCT         = 0.01   # risk per trade = 1% dari total equity
LEVERAGE         = 25     # leverage (dibatasi max_leverage coin). Naikkan utk hemat margin (slot lebih banyak)
MIN_ORDER_USD    = 5.0    # minimum order value Bybit
ORDER_BUMP_FLOOR = 4.0    # order >= ini & < $5 -> naikkan qty ke $5 (over-risk <=1.25x); di bawah ini skip
SBR_MODE         = True   # True = SBR entry di C1.close + SL di C1.low, False = OCL entry lama
ENTRY_MODE       = 'fvg_limit'  # limit di zona FVG (satu-satunya jalur)
TOUCH_VOL_MIN    = 0.8    # touch candle volume min (× avg 20 M5 candle) — hanya dipakai fvg_sbr
MAX_GAP_PCT      = 0.0    # 0 = TANPA BATAS gap (entry=C1.close, SL=C1.low — lebar gap tak ngaruh)
MAX_CONCURRENT   = 5     # PLAFON KEAMANAN posisi bersamaan (backstop). Pembatas utama = MARGIN.
                          # ⚠️ tiap posisi risiko ~1% → 12 posisi = ~12% jika semua kena SL serentak
                          #    (alt sering jatuh berkorelasi!). Turunkan kalau mau lebih aman.
APPROACH_R       = 2.0    # place limit saat harga dalam 1R dari entry (ujung wick C2)
REQUIRE_BOS      = True   # SMC inti: WAJIB BOS H1 dulu
SL_FRAC          = 1.0    # SL penuh di invalidation C1 low/high (standar SMC)
SL_CAP_RANGE     = 0.01   # jarak entry->SL = 10% range BOS (lihat SL_FIXED_RANGE)
SL_FIXED_RANGE   = True   # True = SL SELALU 10% range BOS (abaikan C1); False = SL ikut C1, di-cap 10% range
MIN_DIST_FLOOR   = True   # True = dist kecil pakai SL minimum 0.2% (bukan di-skip)
INDUCEMENT_ENTRY = True   # True = aktif entry inducement (market, kebalik arah BOS besar) berdampingan dgn limit FVG
INDUCEMENT_ZONE_LO = 0.268 # bos kecil dicari mulai 26.8% range BOS besar (dari puncak/lembah)
INDUCEMENT_ZONE_HI = 1.0 # ...sampai 78.6% range. (pita IDM 26.8-78.6%)
INDUCEMENT_TF    = "60"   # timeframe cari inducement: "5"=M5, "60"=H1
INDUCEMENT_SWING = 1      # ukuran swing bos kecil MINIMUM: 1-1 (mencakup 2-2..4-4 & asimetris otomatis)
INDUCEMENT_SWING_MAX = 5   # IDM di-SKIP bila kekuatan swing >= ini di KEDUA sisi (= SWING_BARS; skala BOS besar 5-5+)
REQUIRE_IDM_FOR_FVG = True # True = entry FVG limit HANYA bila BOS besar punya IDM mini-BOS di dalamnya (lebih ketat)

# === ENTRY IDM via LIMIT (Fib retrace candle M5 pemicu) ===
# True = entry IDM pakai LIMIT di Fib IDM_LIMIT_FIB dari range candle M5 yg close menembus trigger
#        (Long: 0%=low,100%=high; Short: 0%=high,100%=low). False = market di harga sweep (lama).
IDM_LIMIT_ENTRY    = True
IDM_LIMIT_FIB      = 0.50   # 50% range candle H1 yg membentuk trigger IDM

# --- Filter momentum "candle makan candle" sebelum entry limit IDM ---
# Tujuan: pastikan ada bukti kekuatan buyer/seller asli (bukan cuma sapuan tipis) di
# leg impulsif (choch->puncak) sebelum mempercayai liquidity di balik IDM tsb.
INDUCEMENT_MOMENTUM_FILTER = False
INDUCEMENT_MOMENTUM_MAX_CANDLES = 5   # window maksimum: N candle H1 terbaru (termasuk candle berjalan)
INDUCEMENT_MOMENTUM_MIN_CANDLES = 3   # kalau candle sejak puncak < ini -> jangan entry (data kurang)
IDM_CANCEL_MOVE_PCT = 0.10  # (lama, hanya aktif kalau IDM_M5_ENGULF=False) batalkan limit IDM jika harga bergerak > N×range BOS dari trigger
IDM_M5_ENGULF       = True  # True = setelah trigger tersapu, monitor M5 engulfing dulu sebelum market entry
IDM_CANCEL_RANGE_PCT= 0.20  # hangus permanen jika harga >N×range BOS dari trigger ke arah mana pun (IDM_M5_ENGULF=True)
REQUIRE_FRESH_C1 = True    # True = tolak FVG bila C1.close sudah disentuh candle SETELAH C3 (zona tak fresh)

# --- Filter konfluensi funding rate (window pre-settlement) ---
# Bybit settle funding 3x sehari: 00:00, 08:00, 16:00 UTC (07:00, 15:00, 23:00 WIB).
# FUNDING_WINDOW_MIN menit sebelum settlement: blokir pasang limit baru YG GAK SEARAH funding,
# DAN batalkan limit yg sudah terpasang (FVG pending + IDM idm_pending) yg gak searah.
# Setelah jam settlement lewat: otomatis normal kembali (tidak perlu restart).
# Posisi aktif (sudah terisi) TIDAK disentuh.
FUNDING_FILTER      = False   # True = aktifkan logika window pre-settlement
FUNDING_MIN_EDGE    = 0.0     # ambang batas rate (fraction). 0.0 = cukup searah saja.
FUNDING_WINDOW_MIN  = 60      # menit sebelum settlement yg jadi window aktif
FUNDING_CACHE_TTL   = 300     # detik — cache get_tickers biar tak spam API

# === HEDGE MODE ===
# True = IDM (market, kebalik arah) + limit FVG (searah BOS) boleh JALAN BARENGAN per koin.
# WAJIB: akun Bybit di Hedge Mode (switch_position_mode mode=3) DULU. positionIdx: Buy=1, Sell=2.
# PERINGATAN: ini ubah routing order live; UJI DI TESTNET (TESTNET=true) sebelum live.
ALLOW_HEDGE = True
def _pidx(side):
    """positionIdx Bybit: hedge -> Buy=1/Sell=2; one-way -> 0."""
    return (1 if side == "Buy" else 2) if ALLOW_HEDGE else 0
def _akey(coin, e_stype):
    """Key active_positions: hedge -> per-arah ('COIN|Long'); one-way -> 'COIN'."""
    return f"{coin}|{e_stype}" if ALLOW_HEDGE else coin

# (jalur eksperimen wait_rev DIBUANG — SMC inti only)

SYMBOLS = [
    # 36 coin — sinkron dengan backtest (wait_rev, −INJ)
    'XPLUSDT', 'MNTUSDT', 'PLUMEUSDT', 'HYPEUSDT', 'BNBUSDT', 'BELUSDT', 'BERAUSDT', 'DASHUSDT', 'DOGEUSDT', 'USUALUSDT', 'TAOUSDT', 'ESPORTSUSDT', 'LABUSDT', 'HUSDT', 'AVAXUSDT', 'REUSDT', '1000BONKUSDT', 'ORCAUSDT', 'AAVEUSDT', 'GMXUSDT', 'LTCUSDT', 'ICPUSDT', 'VIRTUALUSDT', 'CFXUSDT', 'UNIUSDT', 'ONDOUSDT', 'SUIUSDT', 'ALGOUSDT', 'HBARUSDT', 'EIGENUSDT', 'XRPUSDT', 'SOLUSDT', 'CRVUSDT', 'RENDERUSDT', 'XVGUSDT', 'SANDUSDT', 'AXSUSDT', 'IMXUSDT', 'FARTCOINUSDT', 'OPUSDT', '1000PEPEUSDT', 'TIAUSDT', 'GALAUSDT', 'APEUSDT', 'FLOWUSDT',
]

ATR_THRESHOLD = {
    # ATR P25 dari backtest fvg_limit Jan2025–Apr2026
    '1000BONKUSDT'  : 0.0031,   # P25=0.308%
    'BERAUSDT'      : 0.0031,   # P25=0.305%
    'SHIB1000USDT'  : 0.0019,   # P25=0.188%
    'JUPUSDT'       : 0.0028,   # P25=0.278%
    'ORCAUSDT'      : 0.0021,   # P25=0.214%
    'XRPUSDT'       : 0.0018,   # P25=0.185%
    'TAOUSDT'       : 0.0031,   # P25=0.313%
    'AAVEUSDT'      : 0.0026,   # P25=0.259%
    'GMXUSDT'       : 0.0020,   # P25=0.203%
    'LTCUSDT'       : 0.0018,   # P25=0.178%
    'ICPUSDT'       : 0.0023,   # P25=0.231%
    'VIRTUALUSDT'   : 0.0036,   # P25=0.363%
}

# ── Dist range filter: skip setup kalau dist% di luar sweet spot ────────────
# dist% = (c1_close - c1_low/high) / c1_close × 100
# Range dari bucket analysis backtest Jan2025-Apr2026 (dist dinamis).
DIST_RANGE_FILTER = {
    '1000BONKUSDT' : (0.4, 0.8),   # 0.4-0.6: WR=48% N=159, 0.6-0.8: WR=47% N=53
    'AAVEUSDT'     : (0.6, 1.5),   # 0.8-1: WR=46% N=151, 0.6-0.8: WR=46% N=124
    'BERAUSDT'     : (0.6, 1.5),   # 0.6-0.8: WR=50% N=117, 0.8-1: WR=50% N=198
    'GMXUSDT'      : (1.0, 2.0),   # 1-1.5: WR=47% N=270
    'ICPUSDT'      : (0.6, 1.5),   # 0.8-1: WR=50% N=111, 1-1.5: WR=46% N=226
    'JUPUSDT'      : (1.0, 2.0),   # 1-1.5: WR=47% N=127, 1.5-2: WR=49% N=111
    'LTCUSDT'      : (0.6, 1.5),   # 0.8-1: WR=49% N=123, 1.5-2: WR=46% N=71
    'ORCAUSDT'     : (0.6, 1.5),   # 0.8-1: WR=51% N=196 ★
    'SHIB1000USDT' : (1.0, 2.5),   # 1-1.5: WR=47% N=135, 1.5-2: WR=49% N=121
    'SOLUSDT'      : (1.0, 1.5),   # 1-1.5: WR=50% N=117
    'TAOUSDT'      : (0.6, 1.0),   # 0.8-1: WR=65% N=63 ★, 0.4-0.6: WR=49% N=211
    'VIRTUALUSDT'  : (0.6, 1.5),   # 0.8-1: WR=48% N=82
    'XRPUSDT'      : (0.4, 0.8),   # 0.4-0.6: WR=44% N=114, 0.6-0.8: WR=46% N=113
}

# ── Direction filter per coin ────────────────────────────────────────────────
# Dari analisis win/loss backtest: hanya ambil arah yang WR tinggi.
DIR_FILTER: dict = {
    'JUPUSDT'      : 'Short',
    'AAVEUSDT'     : 'Short',
    '1000BONKUSDT' : 'Short',
    'BERAUSDT'     : None,
    'GMXUSDT'      : None,
    'ICPUSDT'      : None,
    'ORCAUSDT'     : None,
    'SHIB1000USDT' : None,
    'SOLUSDT'      : None,
    'TAOUSDT'      : None,
    'VIRTUALUSDT'  : None,
    'XRPUSDT'      : None,
    'LTCUSDT'      : None,
}

# ── Session filter per coin ──────────────────────────────────────────────────
SESSION_FILTER: dict = {
    '1000BONKUSDT' : None,
    'AAVEUSDT'     : None,
    'BERAUSDT'     : None,
    'GMXUSDT'      : None,
    'ICPUSDT'      : None,
    'JUPUSDT'      : None,
    'LTCUSDT'      : None,
    'ORCAUSDT'     : None,
    'SHIB1000USDT' : None,
    'SOLUSDT'      : None,
    'TAOUSDT'      : None,
    'VIRTUALUSDT'  : None,
    'XRPUSDT'      : None,
}


bot_start_ts     = 0     # di-set saat run_bot() mulai — untuk filter sweep historis IDM
pending          = {}
struct_pending   = {}   # coin -> {stype: setup} — pipeline baru: BOS H1 -> IDM touch -> BOS M5 lawan arah -> CHoCH balik -> entry 50%/SL 100% range CHoCH
idm_pending      = {}   # _akey(coin,e_stype) -> limit IDM yg menunggu fill (Fib retrace candle M5)
active_positions = {}
inducement_done  = {}   # coin -> signature struktur BOS besar yg sudah di-entry inducement (anti entry-ulang)
experimental_pending = {}   # coin -> {'m5_focus_hi','m5_focus_lo','m5_focus_ts', ...} — state mode eksperimental (berbasis TIMESTAMP, bukan index; independen dari pending/idm_pending)
h1_bias_state    = {}   # coin -> {'last_ts': <ts candle H1 closed terakhir yg sudah diproses>, 'bias': 'Long'/'Short'/None} — patokan arah entry M5 dari cross EMA3/EMA20 H1

def _bos_match(swing_val, choch_level, swing_val2, choch_level2):
    """True kalau dua BOS besar SAMA PERSIS (dipakai cross-check IDM vs FVG di bawah)."""
    if swing_val is None or choch_level is None or swing_val2 is None or choch_level2 is None:
        return False
    return abs(swing_val - swing_val2) < 1e-12 and abs(choch_level - choch_level2) < 1e-12

def _fvg_trigger_touched_for_bos(coin, swing_val, choch_level):
    """True kalau ADA setup FVG (pending[coin][d], arah manapun) untuk BOS besar SAMA PERSIS
    (swing_val & choch_level identik) yang trigger-nya (c1c / orig_ocl) SUDAH tersentuh M5
    (setup['m5_c1c_touched'] True). Dipakai IDM untuk cek apakah FVG "sudah ambil" arah itu."""
    dirs = pending.get(coin)
    if not dirs:
        return False
    for d, s in dirs.items():
        if s.get('m5_c1c_touched') and _bos_match(s.get('swing_val'), s.get('choch_level'), swing_val, choch_level):
            return True
    return False

def _idm_trigger_touched_for_bos(coin, swing_val, choch_level):
    """True kalau ADA entri idm_pending untuk BOS besar SAMA PERSIS yang trigger-nya sudah
    tersentuh (idm_pending selalu dibuat SETELAH trigger tersentuh -> keberadaan entri = tersentuh,
    kecuali sudah hangus/dibuang). Dipakai FVG untuk cek apakah IDM "sudah ambil" arah itu."""
    for key, p in idm_pending.items():
        if p.get('coin') != coin:
            continue
        if p.get('m5_hangus'):
            continue   # sudah hangus -> dianggap tidak lagi "mengambil" arah itu
        if _bos_match(p.get('swing_val'), p.get('choch_level'), swing_val, choch_level):
            return True
    return False

# ── Persistensi inducement_done ke file JSON (bertahan lewat redeploy/restart) ──
# Path bisa dioverride via env var STATE_FILE_PATH (arahkan ke Railway Volume kalau ada,
# misal "/data/bot_state.json" — kalau tidak diset & filesystem-nya ephemeral, state akan
# tetap hilang saat redeploy, sama seperti sebelumnya).
STATE_FILE = os.environ.get("STATE_FILE_PATH", "bot_state.json")

def _sig_to_list(sig):
    """Tuple signature (bisa berisi None) -> list, supaya JSON-serializable."""
    if sig is None:
        return None
    return list(sig)

def _sig_from_list(lst):
    """List dari JSON -> tuple signature (balik ke bentuk asli)."""
    if lst is None:
        return None
    return tuple(lst)

def save_state():
    """Tulis ulang inducement_done ke STATE_FILE (dipanggil tiap kali inducement_done di-update)."""
    try:
        encoded = {f"{coin}|{stype}": _sig_to_list(sig) for (coin, stype), sig in inducement_done.items()}
        tmp_path = STATE_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump({"inducement_done": encoded}, f)
        os.replace(tmp_path, STATE_FILE)   # atomic write, hindari file korup kalau kepotong
    except Exception as e:
        print(f"⚠️ save_state gagal: {e}")

def load_state():
    """Load inducement_done dari STATE_FILE saat bot start. Aman kalau file belum ada."""
    global inducement_done
    if not os.path.exists(STATE_FILE):
        print(f"ℹ️ {STATE_FILE} belum ada — mulai inducement_done kosong (normal di run pertama).")
        return
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        encoded = data.get("inducement_done", {})
        loaded = {}
        for key, sig_list in encoded.items():
            coin, stype = key.split("|", 1)
            loaded[(coin, stype)] = _sig_from_list(sig_list)
        inducement_done = loaded
        print(f"✅ State dimuat dari {STATE_FILE}: {len(inducement_done)} entri inducement_done.")
    except Exception as e:
        print(f"⚠️ load_state gagal ({e}) — mulai inducement_done kosong.")

instrument_cache = {}
funding_cache    = {}   # symbol -> {'rate': float, 'ts': float} — cache funding rate (TTL=FUNDING_CACHE_TTL)
done_setups      = {}   # coin -> {swing_val, stype, used_ocl} — cegah re-entry di BOS yang sama


# ============================================================
# FUNGSI DATA
# ============================================================

def get_data(symbol, interval, limit=200):
    try:
        res = session.get_kline(
            category=CATEGORY, symbol=symbol,
            interval=interval, limit=limit
        )
        if res['retCode'] == 0:
            df = pd.DataFrame(
                res['result']['list'],
                columns=['ts','open','high','low','close','vol','turnover']
            )
            df[['open','high','low','close','vol','turnover','ts']] = \
                df[['open','high','low','close','vol','turnover','ts']].apply(pd.to_numeric)
            df = df.iloc[::-1].reset_index(drop=True)
            LAST_OHLC[(symbol, str(interval))] = df   # cache utk unduhan diagnostik
            return df
        print(f"⚠️ get_data {symbol} {interval}: {res.get('retMsg','')}")
        return None
    except Exception as e:
        print(f"⚠️ get_data {symbol} {interval}: {e}")
        return None


# ============================================================
# INSTRUMENT INFO
# ============================================================

def get_instrument_info(symbol):
    if symbol in instrument_cache:
        return instrument_cache[symbol]
    try:
        res = session.get_instruments_info(category=CATEGORY, symbol=symbol)
        if res['retCode'] == 0:
            info = res['result']['list'][0]
            lot  = info['lotSizeFilter']
            data = {
                'min_qty'     : float(lot['minOrderQty']),
                'qty_step'    : float(lot['qtyStep']),
                'tick_size'   : float(info['priceFilter']['tickSize']),
                'max_leverage': float(info.get('leverageFilter', {}).get('maxLeverage', 10)),
            }
            instrument_cache[symbol] = data
            return data
    except Exception as e:
        print(f"⚠️ instrument_info {symbol}: {e}")
    return {'min_qty': 0.01, 'qty_step': 0.01, 'tick_size': 0.0001}


# ============================================================
# FUNDING RATE (filter konfluensi)
# ============================================================

def get_funding_rate(symbol):
    """Funding rate terkini (fraction, mis. 0.0001 = 0.01%) dari Bybit ticker.
    Di-cache TTL=FUNDING_CACHE_TTL detik biar tak spam API tiap loop coin.
    Kalau API gagal: balikin cache lama (kalau ada) drpd None, biar tahan sesaat gangguan jaringan."""
    now = time.time()
    cached = funding_cache.get(symbol)
    if cached and (now - cached['ts']) < FUNDING_CACHE_TTL:
        return cached['rate']
    try:
        res = session.get_tickers(category=CATEGORY, symbol=symbol)
        if res['retCode'] == 0 and res['result']['list']:
            rate = float(res['result']['list'][0]['fundingRate'])
            funding_cache[symbol] = {'rate': rate, 'ts': now}
            return rate
        print(f"⚠️ funding_rate {symbol}: {res.get('retMsg','')}")
    except Exception as e:
        print(f"⚠️ funding_rate {symbol}: {e}")
    return cached['rate'] if cached else None


def funding_favors(stype, symbol):
    """True kalau funding rate SAAT INI menguntungkan posisi `stype` ('Long'/'Short') saat settlement.
    Bybit: rate POSITIF -> Long bayar Short. rate NEGATIF -> Short bayar Long.
    Kalau rate gagal diambil -> True (jangan blokir entry krn alasan teknis API, bukan krn sinyal funding)."""
    rate = get_funding_rate(symbol)
    if rate is None:
        return True
    if stype == "Long":
        return rate <= -FUNDING_MIN_EDGE
    else:
        return rate >= FUNDING_MIN_EDGE


def in_funding_window():
    """True kalau sekarang dalam FUNDING_WINDOW_MIN menit sebelum salah satu jam settlement Bybit.
    Settlement UTC: 00:00, 08:00, 16:00.  Fungsi ini pakai UTC supaya konsisten tanpa peduli TZ server."""
    import datetime as _dt
    now_utc = _dt.datetime.utcnow()
    mins_utc = now_utc.hour * 60 + now_utc.minute
    for settle_h in (0, 8, 16):
        settle_mins = settle_h * 60
        # selisih menit menuju settlement berikutnya (wrap-around 24 jam)
        diff = (settle_mins - mins_utc) % (24 * 60)
        if 0 < diff <= FUNDING_WINDOW_MIN:   # 0 dikecualikan: pas detik-detik settlement = sudah lewat
            return True
    return False


def cancel_unfavorable_limits(coin):
    """Selama dalam funding window: batalkan limit FVG (pending) & IDM (idm_pending) coin ini
    yang arahnya GAK SEARAH funding rate sekarang. Posisi aktif tidak disentuh."""
    import copy
    # ── FVG limits (pending) ──
    if coin in pending:
        dirs_to_remove = []
        for d, st in list(pending[coin].items()):
            if st.get('phase') != 'WAIT_FILL':
                continue                         # WAIT_APPROACH belum punya limit order -> skip
            stype_limit = st.get('type')
            if stype_limit and not funding_favors(stype_limit, coin):
                oid = st.get('order_id')
                if oid:
                    cancel_order(coin, oid)
                dirs_to_remove.append(d)
                rate = get_funding_rate(coin)
                print(f"   💸 {coin} FVG {stype_limit}: limit dibatalkan (funding window, rate={rate})")
        for d in dirs_to_remove:
            del pending[coin][d]
        if not pending[coin]:
            del pending[coin]
    # ── IDM limits (idm_pending) ──
    for key in list(idm_pending.keys()):
        # key = _akey(coin, e_stype) = f"{coin}_{e_stype}"
        if not key.startswith(coin + "_"):
            continue
        e_stype = key[len(coin)+1:]
        if not funding_favors(e_stype, coin):
            st = idm_pending[key]
            oid = st.get('order_id')
            if oid:
                cancel_order(coin, oid)
            del idm_pending[key]
            rate = get_funding_rate(coin)
            print(f"   💸 {coin} IDM {e_stype}: limit dibatalkan (funding window, rate={rate})")


def round_qty(qty, step):
    step_str  = f'{step:.10f}'.rstrip('0')
    precision = len(step_str.split('.')[-1]) if '.' in step_str else 0
    return round(int(qty / step) * step, precision)


def round_price(price, tick):
    tick_str  = f'{tick:.10f}'.rstrip('0')
    precision = len(tick_str.split('.')[-1]) if '.' in tick_str else 0
    return round(round(price / tick) * tick, precision)


# ============================================================
# INDICATORS
# ============================================================

def calc_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def calc_atr(df, period=14):
    h, l, pc = df['high'], df['low'], df['close'].shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ============================================================
# SWING DETECTION
# ============================================================

# Swing high/low: butuh SWING_BARS candle lebih rendah/tinggi di KIRI & KANAN.
# Hanya swing yang SUDAH terkonfirmasi penuh (5-kanan terbentuk) yang dikembalikan.
# Candle yang MENEMBUS swing (breaker) dievaluasi terpisah (lihat closed_h1 = df.iloc[-2])
# dan TIDAK perlu konfirmasi kanan-5 — cukup close menembus swing yang sudah valid.
SWING_BARS = 5
# Fraktal HALUS untuk telusur leg (rebreak/extension) di dalam impuls. Lebih halus dari SWING_BARS
# supaya swing-2 minor (mis. retrace dangkal lalu rebreak) tetap terbaca, tapi tak sebising bar mentah.
SUBLEG_BARS = 3

# Filter zona entry: C1.close (entry) harus berada di retrace ENTRY_ZONE_LO..ENTRY_ZONE_HI
# dari range BOS, di mana 0% = ekstrem impulse (swing terbaru), 100% = CHOCH (invalidasi).
# Mis. 0.50..1.00 = hanya zona "diskon" (separuh lebih dalam menuju CHOCH).
ENTRY_ZONE_LO = 0.0   # golden ratio / OTE — C1.close minimal retrace 61.8%
ENTRY_ZONE_HI = 1.00
# Trigger FVG entry = ujung C3 (low[C3] untuk Long, high[C3] untuk Short = batas gap).
# Zona golden ratio dihitung dari C3 ujung, bukan C1 close.
FVG_CANCEL_RANGE_PCT = 0.20   # 20% BOS range dari C3 ujung ke arah BOS → setup hangus

# --- Filter engulfing M5 sebelum entry FVG (C1 close sebagai trigger) ---
# Saat C1 close H1 tersentuh, bot monitor M5 dan tunggu konfirmasi engulfing sebelum market order.
# "Candle fokus" = candle M5 pertama yang menyentuh C1 close, lalu bergeser jika ada wick/close
# yang keluar dari range candle fokus. Entry terjadi saat close candle M5 melewati high candle fokus
# (Long) atau low candle fokus (Short). SL = low_engulfing - SL_ENGULF_PCT*bos_rng (Long).
M5_ENGULF_FILTER  = True    # False = skip filter ini, entry langsung market saat C1 close tersentuh
SL_ENGULF_PCT     = 0.05    # SL = entry ± N% range BOS (fixed, proporsional ke besar-kecil BOS)

# --- MODE EKSPERIMENTAL: monitoring M5 semua coin dari awal, tanpa nunggu trigger H1 (BOS/IDM/FVG) ---
# Independen sepenuhnya dari jalur IDM/FVG di atas — tidak menyentuh state/limit slot mereka.
# Begitu bot start, SEMUA coin di SYMBOLS langsung mulai cari candle "fokus" M5 (arah manapun,
# Long & Short dipantau bersamaan) dan mencari engulfing dari situ terus-menerus (fokus geser
# tiap kali ada candle yang melewati hi/lo fokus, sama seperti aturan dasar _scan_engulf biasa).
# TIDAK ADA gate EMA H1, TIDAK ADA cross-check IDM/FVG, TIDAK ADA BOS besar sama sekali — market
# order langsung begitu syarat di bawah terpenuhi.
EXPERIMENTAL_MODE     = False   # master switch mode eksperimental lama — dimatikan, kode dibiarkan (bisa dinyalakan lagi)
EXPERIMENTAL_EMA_PREV = True    # syarat: candle SEBELUM engulfing (i-1) wick harus sentuh EMA20 M5
EXPERIMENTAL_EMA_CROSS_BARS = 5      # window candle (SEBELUM & SESUDAH engulfing) utk cari cross EMA3/EMA20
EXPERIMENTAL_SL_PCT   = 0.10    # SL = entry AKTUAL ± N% dari harga entry (dihitung ulang saat cross terjadi, bukan dari ujung candle)
EXPERIMENTAL_IDLE_LOG_EVERY = 12  # log status "masih diam" tiap N siklus tanpa event (12 siklus ≈ 1 jam)

# --- FILTER BIAS H1 (EMA3/EMA20) untuk arah entry M5 mode eksperimental ---
# H1 dipakai sbg "patokan arah": EMA3 cross EMA20 dari BAWAH ke ATAS di H1 -> bias = Long (hanya
# cari entry Long di M5). Beberapa candle H1 kemudian, kalau EMA3 cross balik ke BAWAH EMA20 ->
# bias berganti jadi Short (hanya cari entry Short di M5). Bias tetap berlaku sampai ada cross
# H1 berlawanan berikutnya (tidak reset tiap candle). Tujuannya mengurangi over-trading krn noise
# M5 dengan memaksa entry searah tren H1 saja.
EXPERIMENTAL_H1_BIAS_FILTER = True   # master switch filter ini — False = M5 bebas cari 2 arah spt sebelumnya

# --- MODE STRUKTURAL (aktif) — BOS H1 -> IDM (WAJIB) -> BOS M5 lawan arah -> CHoCH balik -> entry ---
# BOS H1 + IDM + FVG tetap DIDETEKSI seperti biasa (FVG cuma dihitung utk info/log, TIDAK dipakai
# sbg syarat/entry). Entry LAMA (gate EMA20 H1, limit di C1-close/trig FVG, entry langsung dari IDM)
# DIHILANGKAN total — diganti alur baru:
#   1) BOS H1 + FVG (WAJIB ada) + IDM (WAJIB, leg yg dipilih harus tepat di bawahnya ada FVG asli —
#      bukan cuma leg IDM lain; BOS tanpa FVG / tanpa leg IDM yg FVG-backed dianggap LEMAH & di-skip)
#      -> setup WAIT_IDM_TOUCH.
#   2) Harga M5 retrace menyentuh level IDM H1 -> masuk WAIT_M5_CHOCH (mulai monitoring M5). Kalau
#      SETELAH itu harga M5 ternyata menyentuh PUNCAK H1 lagi (bukan reversal, trend cuma lanjut) ->
#      setup dibuang, tunggu BOS H1 baru (siklus berikutnya otomatis dpt versi ter-update).
#   3) Bangun BOS-m5 SEBENARNYA di arah LAWAN BOS H1 — PERSIS metode H1 (pick_bos_swing +
#      impulse_anchors + apply_latest_leg): break, choch_level (level protektif GENUINE), puncak.
#   4) Di DALAM impuls BOS-m5 itu (dari titik break sampai puncak), cari IDM-m5 (rantai record-break,
#      PERSIS metode find_inducement/IDM H1) — WAJIB tersentuh dulu sebelum apapun.
#   5) Setelah IDM-m5 tersentuh, dua kemungkinan:
#      a) Puncak BOS-m5 tersentuh LAGI -> trend {opp} lanjut (manipulasi belum selesai) -> tunggu
#         BOS-m5 yang lebih baru terbentuk (otomatis, dihitung ulang tiap siklus).
#      b) CHoCH: candle CLOSE (bukan wick/sweep) menembus choch_level_m5 SEARAH BOS H1 -> pembalikan
#         genuine -> lanjut ke #6.
#   6) Cari RBS (Resistance Become Support, BOS H1 Long) / SBR (Support Become Resistance, Short)
#      KECIL — dicari dari titik BOS-m5 terbentuk SAMPAI titik CHoCH break saja (di atas titik break
#      dianggap umpan): candle searah CHoCH close lalu candle lawan open dekat situ & harga menjauh
#      (ada 'space' dulu, wajib), baru sah jadi level kecil; begitu di-break balik (close) -> RBS/SBR
#      terkonfirmasi. Kalau TIDAK ADA RBS/SBR di window itu -> JANGAN entry, reset & tunggu BOS-m5
#      {opp} yang benar-benar baru, tunggu CHoCH lagi.
#   7) Entry = limit di TENGAH zona RBS/SBR (titik close & open candle pembentuknya), SL = 100% range
#      (ekstrem BOS-m5 fokus).
STRUCT_MODE = True   # master switch — matikan (False) utk balik ke jalur EMA/FVG/IDM entry lama
REBREAK_INVALID = True  # True = BOS batal bila harga retrace >= RETRACE_LOCK lalu close lewati swing-2 (struktur baru)
ZONE_FROM_RETRACE = True # True = batas bawah zona entry = max(61.8%, retrace terdalam); area yg sudah dilewati retrace tak dipakai
RETRACE_LOCK    = 0.50  # ambang retrace yang "mengunci" swing-2 sebagai puncak (50% range BOS)

def find_last_swing_bos(df, n=SWING_BARS):
    highs, lows = [], []
    hi = df['high'].values; lo = df['low'].values; ts = df['ts'].values
    for i in range(n, len(df) - n):
        h = hi[i]; l = lo[i]
        if all(hi[i-k] < h for k in range(1, n+1)) and all(hi[i+k] < h for k in range(1, n+1)):
            highs.append({'val': h, 'idx': i, 'ts': ts[i]})
        if all(lo[i-k] > l for k in range(1, n+1)) and all(lo[i+k] > l for k in range(1, n+1)):
            lows.append({'val': l, 'idx': i, 'ts': ts[i]})
    return highs, lows


def impulse_anchors(stype, swing_val, brk_idx, sh_h1, sl_h1, df=None):
    """CHOCH = protective low/high = EKSTREM (low terendah / high tertinggi) ANTARA
    swing-1 (yang di-break) dan puncak/lembah swing-2 — yaitu launch impulse, bukan
    swing lama di belakang swing-1. Return (bos_idx, choch_level, peak_val).
    peak_val = swing 5-5 terkonfirmasi yang jadi puncak/lembah (None bila belum terbentuk)."""
    if swing_val is None or brk_idx is None or not sh_h1 or not sl_h1:
        return None, None, None
    if stype == "Long":
        peaks = [x for x in sh_h1 if x['idx'] > brk_idx and x['val'] > swing_val]
        peak_val = max(peaks, key=lambda x: x['val'])['val'] if peaks else None
        # puncak (batas atas pencarian choch) = high tertinggi mentah setelah break
        if df is not None and len(df) > brk_idx + 1:
            peak_idx = int(df['high'].iloc[brk_idx:].idxmax())
        else:
            peak_idx = (max(peaks, key=lambda x: x['val'])['idx'] if peaks else sh_h1[-1]['idx'])
        # CHOCH = swing low 5-5 TERDALAM antara break & puncak (HARUS swing 5-5; kalau tak ada -> skip)
        cands = [x for x in sl_h1 if brk_idx <= x['idx'] < peak_idx]
        if not cands:
            return None, None, peak_val
        ch = min(cands, key=lambda x: x['val'])
        return ch['idx'], ch['val'], peak_val
    else:
        troughs = [x for x in sl_h1 if x['idx'] > brk_idx and x['val'] < swing_val]
        peak_val = min(troughs, key=lambda x: x['val'])['val'] if troughs else None
        if df is not None and len(df) > brk_idx + 1:
            trough_idx = int(df['low'].iloc[brk_idx:].idxmin())
        else:
            trough_idx = (min(troughs, key=lambda x: x['val'])['idx'] if troughs else sl_h1[-1]['idx'])
        cands = [x for x in sh_h1 if brk_idx <= x['idx'] < trough_idx]
        if not cands:
            return None, None, peak_val
        ch = max(cands, key=lambda x: x['val'])
        return ch['idx'], ch['val'], peak_val


def rebreak_invalid(df, start_idx, swing2, choch_level, stype, lock_retr=0.50):
    """True bila SETELAH harga retrace >= lock_retr (dari swing2 ke arah choch),
    ada candle yang CLOSE melewati swing2 (= rebreak, struktur baru).
    swing2 = puncak/lembah swing 5-5 (TETAP). Dihitung historis -> konsisten lintas-redeploy."""
    n = len(df)
    if swing2 is None or start_idx is None or start_idx >= n - 1 or choch_level is None:
        return False
    hi = df['high'].values; lo = df['low'].values; cl = df['close'].values
    if stype == "Long":
        rng = swing2 - choch_level
        if rng <= 0:
            return False
        half = swing2 - lock_retr * rng
        retraced = False
        for k in range(int(start_idx) + 1, n):
            if lo[k] <= half:
                retraced = True
            if retraced and cl[k] > swing2:
                return True
        return False
    else:
        rng = choch_level - swing2
        if rng <= 0:
            return False
        half = swing2 + lock_retr * rng
        retraced = False
        for k in range(int(start_idx) + 1, n):
            if hi[k] >= half:
                retraced = True
            if retraced and cl[k] < swing2:
                return True
        return False


def h1_ema_gate(df_h1, trigger_ts_ms, direction):
    """Dipanggil SEKALI saat trigger H1 (IDM atau FVG) baru tersentuh — bukan tiap saat.
    Cek posisi body candle H1 (open-close, bukan wick) terhadap EMA20 H1 utk menentukan arah
    entry final:
      - Body SELURUHNYA di sisi 'default' arah (Long->di atas EMA, Short->di bawah EMA)
        -> arah TETAP sama (mode IDM/FVG biasa).
      - Body SELURUHNYA di sisi BERLAWANAN -> arah DIBALIK (mode EMA, mis. IDM Short jadi
        cari entry Long karena banyak buyer mengisi di EMA).
      - EMA ada DI TENGAH body (ambigu, tak bisa ditentukan) -> tunggu candle H1 BERIKUTNYA yang
        wick-nya menyentuh EMA lagi, lalu candle itu yang jadi acuan keputusan (ulang aturan yang
        sama: body di atas/bawah EMA candle retest itu).
    trigger_ts_ms = waktu (ms) saat trigger H1 pertama tersentuh (dipakai cari candle H1 pertama
    yang relevan, mundur 1 jam supaya candle yang sedang berjalan saat trigger tersentuh ikut).
    direction = arah entry DEFAULT ('Long'/'Short') sebelum dipengaruhi EMA.
    Return: (resolved: bool, final_direction: str|None, info: str, decision_ts_close: float|None)."""
    if df_h1 is None or len(df_h1) == 0 or 'ts' not in df_h1.columns:
        return False, None, "data H1 kosong", None
    df = df_h1.copy()
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    cutoff = trigger_ts_ms - 3600_000   # mundur 1 candle H1 (sama alasannya dgn fix created_ts M5)
    seg = df[df['ts'] >= cutoff].reset_index(drop=True)
    n = len(seg)
    closed_end = n - 1   # jangan hitung candle H1 yang masih berjalan
    if closed_end < 1:
        return False, None, "belum ada candle H1 closed sejak trigger tersentuh", None

    def _opp(d):
        return 'Short' if d == 'Long' else 'Long'

    def _wick_touches_ema(idx):
        lo = float(seg['low'].iloc[idx]); hi = float(seg['high'].iloc[idx]); ema_i = float(seg['ema20'].iloc[idx])
        return lo <= ema_i <= hi

    # Cari candle PERTAMA (mulai dari awal segmen) yang wick-nya menyentuh EMA20 —
    # ini jadi syarat wajib SEBELUM body-nya boleh dicek, berlaku sama untuk candle
    # pertama maupun candle retest berikutnya (tidak ada lagi perlakuan berbeda).
    decision_idx = None
    for i in range(0, closed_end):
        if _wick_touches_ema(i):
            decision_idx = i
            break
    if decision_idx is None:
        return False, None, "belum ada candle H1 (wick) yang menyentuh EMA20 sejak trigger tersentuh", None

    while decision_idx < closed_end:
        op  = float(seg['open'].iloc[decision_idx]); cl = float(seg['close'].iloc[decision_idx])
        ema = float(seg['ema20'].iloc[decision_idx])
        body_lo, body_hi = min(op, cl), max(op, cl)
        if ema < body_lo or ema > body_hi:
            above = ema < body_lo
            same_side = (above and direction == 'Long') or (not above and direction == 'Short')
            final_dir = direction if same_side else _opp(direction)
            mode = "IDM/FVG (searah)" if same_side else "EMA (dibalik)"
            decision_ts_close = float(seg['ts'].iloc[decision_idx]) + 3600_000   # waktu CLOSE candle H1 ini
            return True, final_dir, (f"H1 {_ts_wib(seg['ts'].iloc[decision_idx])}: wick sentuh EMA20 & body "
                                      f"{'di atas' if above else 'di bawah'} EMA20({ema:.6g}) -> mode {mode}"), decision_ts_close
        # EMA di tengah body (ambigu) -> cari candle SETELAHNYA yang wick-nya menyentuh EMA lagi
        found = None
        for j in range(decision_idx + 1, closed_end):
            if _wick_touches_ema(j):
                found = j
                break
        if found is None:
            return False, None, "EMA di tengah body candle H1, menunggu candle H1 sentuh EMA lagi", None
        decision_idx = found   # candle retest ini jadi acuan keputusan baru, ulangi cek dari sini
    return False, None, "menunggu candle H1 close berikutnya", None


def struct_touch_invalidated(df_m5, bos_stype, choch_level, peak_val):
    """Cek candle M5 (SEMUA yang ada di window yang di-fetch, termasuk yg masih berjalan):
    apakah wick-nya sudah menyentuh choch_level, ATAU menyentuh/melewati peak_val ke arah tren
    BOS besar tetap berlanjut (berarti puncak belum final, struktur masih berkembang).
    Pakai LOW/HIGH M5 (wick), bukan close H1 — supaya invalidasi terdeteksi persis saat M5
    menyentuhnya, bukan cuma pas candle H1 close.
    bos_stype = arah BOS BESAR ('Long'/'Short'). Return (True, alasan) / (False, None)."""
    if df_m5 is None or len(df_m5) == 0:
        return False, None
    lo_min = float(df_m5['low'].min())
    hi_max = float(df_m5['high'].max())
    if bos_stype == "Long":
        if choch_level is not None and lo_min <= float(choch_level):
            return True, f"CHOCH {float(choch_level):.6g} tersentuh M5 (low={lo_min:.6g})"
        if peak_val is not None and hi_max > float(peak_val):
            return True, f"puncak {float(peak_val):.6g} tersentuh/lewat M5 (high={hi_max:.6g}) — tren masih lanjut"
    else:
        if choch_level is not None and hi_max >= float(choch_level):
            return True, f"CHOCH {float(choch_level):.6g} tersentuh M5 (high={hi_max:.6g})"
        if peak_val is not None and lo_min < float(peak_val):
            return True, f"puncak {float(peak_val):.6g} tersentuh/lewat M5 (low={lo_min:.6g}) — tren masih lanjut"
    return False, None


def choch_is_broken(df, bos_idx, choch_level, stype):
    """CHoCH ditembus = SETELAH puncak, ada candle yang CLOSE menembus choch (Long: < choch / Short: > choch).
    Historis -> tetap mati walau harga sudah balik. bos_idx = indeks choch (launch)."""
    n = len(df)
    if bos_idx is None or bos_idx >= n or choch_level is None:
        return False
    if stype == "Long":
        peak_idx = int(df['high'].iloc[bos_idx:].idxmax())
        return bool((df['close'].iloc[peak_idx:] < choch_level).any())
    else:
        peak_idx = int(df['low'].iloc[bos_idx:].idxmin())
        return bool((df['close'].iloc[peak_idx:] > choch_level).any())


def momentum_eaten(df_h1, peak_idx, stype):
    """Cek 'candle makan candle' di leg impulsif BOS besar (`stype`), dari puncak/lembah
    s/d candle TERBARU (termasuk yg belum close).
    Window = maks INDUCEMENT_MOMENTUM_MAX_CANDLES candle terbaru, tak boleh lewat `peak_idx`.
    Kalau jumlah candle di window < INDUCEMENT_MOMENTUM_MIN_CANDLES -> None (data kurang, jangan entry).
    stype Long -> fokus HIGH dimakan (lower bound jadi referensi baru kalau LOW tertembus duluan).
    stype Short -> fokus LOW dimakan (upper bound jadi referensi baru kalau HIGH tertembus duluan).
    'Dimakan' = sisi relevan disentuh ATAU dilewati (>=  / <=), bukan harus tembus.
    Return True/False/None."""
    n = len(df_h1)
    if peak_idx is None or peak_idx < 0 or peak_idx >= n:
        return None
    latest_idx = n - 1
    start_idx = max(peak_idx, latest_idx - (INDUCEMENT_MOMENTUM_MAX_CANDLES - 1))
    window_size = latest_idx - start_idx + 1
    if window_size < INDUCEMENT_MOMENTUM_MIN_CANDLES:
        return None
    ref_hi = float(df_h1['high'].iloc[start_idx])
    ref_lo = float(df_h1['low'].iloc[start_idx])
    for i in range(start_idx + 1, latest_idx + 1):
        hi = float(df_h1['high'].iloc[i])
        lo = float(df_h1['low'].iloc[i])
        if stype == "Long":
            if hi >= ref_hi:
                return True          # sisi relevan (atas) dimakan -> sukses
            if lo <= ref_lo:
                ref_hi, ref_lo = hi, lo   # sisi bawah tertembus -> range lama tak lagi utuh, reset referensi
        else:
            if lo <= ref_lo:
                return True          # sisi relevan (bawah) dimakan -> sukses
            if hi >= ref_hi:
                ref_hi, ref_lo = hi, lo   # sisi atas tertembus -> reset referensi
    return False


def deepest_retrace_lo(df, bos_idx, choch_level, stype):
    """Batas bawah zona entry dinamis = max(ENTRY_ZONE_LO, retrace TERDALAM setelah puncak).
    Area 0..retrace_terdalam sudah dilewati candle retrace -> tak boleh dipakai entry (sudah terisi)."""
    n = len(df)
    if not ZONE_FROM_RETRACE or bos_idx is None or bos_idx >= n or choch_level is None:
        return ENTRY_ZONE_LO
    if stype == "Long":
        sub = df['high'].iloc[bos_idx:]
        B = float(sub.max()); pk = int(sub.idxmax()); rng = B - choch_level
        if rng <= 0: return ENTRY_ZONE_LO
        low_after = float(df['low'].iloc[pk:].min())
        frac = (B - low_after) / rng
    else:
        sub = df['low'].iloc[bos_idx:]
        B = float(sub.min()); pk = int(sub.idxmin()); rng = choch_level - B
        if rng <= 0: return ENTRY_ZONE_LO
        high_after = float(df['high'].iloc[pk:].max())
        frac = (high_after - B) / rng
    return max(ENTRY_ZONE_LO, min(frac, ENTRY_ZONE_HI))


# ============================================================
# FVG — dengan volume fields untuk fvg
# ============================================================

def _gap_vol_fields(df, c3_idx):
    """Extract volume + OCL + C1 fields untuk FVG (df dalam H1). C1=c3_idx-2."""
    c2_idx   = c3_idx - 1
    c1_idx   = c3_idx - 2
    c2_close = float(df['close'].iloc[c2_idx]) if c2_idx >= 0 else 0.0
    c2_low   = float(df['low'].iloc[c2_idx])   if c2_idx >= 0 else 0.0
    c2_high  = float(df['high'].iloc[c2_idx])  if c2_idx >= 0 else 0.0
    c3_open  = float(df['open'].iloc[c3_idx])  if c3_idx < len(df) else 0.0
    c1_open  = float(df['open'].iloc[c1_idx])  if c1_idx >= 0 else 0.0
    c1_close = float(df['close'].iloc[c1_idx]) if c1_idx >= 0 else 0.0
    c1_low   = float(df['low'].iloc[c1_idx])   if c1_idx >= 0 else 0.0
    c1_high  = float(df['high'].iloc[c1_idx])  if c1_idx >= 0 else 0.0
    base = {'c2_close': c2_close, 'c2_low': c2_low, 'c2_high': c2_high, 'c3_open': c3_open,
            'c1_open': c1_open, 'c1_close': c1_close,
            'c1_low': c1_low,   'c1_high': c1_high, 'c3_idx': c3_idx}
    if 'vol' not in df.columns:
        return {**base, 'c3_vol': 0.0, 'vol_max10h': 0.0}
    c3_vol    = float(df['vol'].iloc[c3_idx])
    avg_start = max(0, c3_idx - 5)
    vol_max   = float(df['vol'].iloc[avg_start:c3_idx].max()) if c3_idx > 0 else 0.0
    return {**base, 'c3_vol': c3_vol, 'vol_max10h': vol_max}


def get_internal_gaps(df, stype, bos_idx, lookback=60, require_fresh=True, peak_idx=None):
    """Scan FVG dari bos_idx (CHOCH) sampai peak_idx (puncak) — leg impulsif saja.
    Kalau peak_idx=None, scan sampai akhir data (perilaku lama untuk caller lain).
    require_fresh=True: cek apakah FVG sudah terisi (candle dalam range menyentuh bottom/top gap).
    require_fresh=False: semua gap mentah tanpa filter, freshness diserahkan ke pemanggil."""
    gaps = []
    # Batas akhir scan: peak_idx kalau tersedia, else akhir data
    scan_end = (peak_idx - 1) if (peak_idx is not None and peak_idx > bos_idx) else (len(df) - 2)
    scan_end = min(scan_end, len(df) - 2)

    # Scan FVG dari bos_idx sampai scan_end (CHOCH → puncak)
    # C1=i-1, C2=i, C3=i+1  →  i mulai dari bos_idx+1
    for i in range(bos_idx + 1, scan_end + 1):
        if i + 1 >= len(df): continue
        gap = None
        if stype == "Long" and df['high'].iloc[i-1] < df['low'].iloc[i+1]:
            gap = {"top": df['low'].iloc[i+1], "bottom": df['high'].iloc[i-1], "zone": "impulse"}
            gap.update(_gap_vol_fields(df, i + 1))
        elif stype == "Short" and df['low'].iloc[i-1] > df['high'].iloc[i+1]:
            gap = {"top": df['low'].iloc[i-1], "bottom": df['high'].iloc[i+1], "zone": "impulse"}
            gap.update(_gap_vol_fields(df, i + 1))
        if gap:
            is_fresh = True
            if require_fresh:
                # Cek apakah ada candle SETELAH C3 (dalam range s/d peak_idx) yang menutup gap
                check_end = (peak_idx + 1) if peak_idx is not None else len(df)
                check_end = min(check_end, len(df))
                for j in range(i + 2, check_end):
                    if stype == "Long"  and df['low'].iloc[j]  <= gap['bottom']: is_fresh = False; break
                    if stype == "Short" and df['high'].iloc[j] >= gap['top']:    is_fresh = False; break
            if is_fresh:
                gaps.append(gap)

    if stype == "Long":
        gaps.sort(key=lambda g: g['top'], reverse=True)
    else:
        gaps.sort(key=lambda g: g['bottom'])
    return gaps


def fvg_fully_broken(candle, fvg, stype):
    if stype == "Long":  return candle['close'] < fvg['bottom']
    else:                return candle['close'] > fvg['top']

def candle_touches_fvg(candle, fvg, stype):
    if stype == "Long":
        return candle['low'] <= fvg['top'] and not fvg_fully_broken(candle, fvg, stype)
    else:
        return candle['high'] >= fvg['bottom'] and not fvg_fully_broken(candle, fvg, stype)


def _get_fvgs(df_h1, stype, bos_idx, choch_level=None, zone_lo=None, require_fresh=None):
    """FVG biasa (TANPA syarat volume): C1/C3 valid, CHOCH filter, zona entry, MAX_GAP, fresh-C1.
    zone_lo = batas bawah zona (default ENTRY_ZONE_LO). Dipakai utk zona dinamis (>= retrace terdalam).
    require_fresh = override REQUIRE_FRESH_C1 global (None = pakai default global)."""
    fresh_flag = REQUIRE_FRESH_C1 if require_fresh is None else require_fresh
    # peak_idx: high/low tertinggi setelah bos_idx — batas atas scan FVG (CHOCH→puncak)
    if stype == "Long":
        peak_idx_fvg = int(df_h1['high'].iloc[bos_idx:].idxmax()) if len(df_h1) > bos_idx else None
    else:
        peak_idx_fvg = int(df_h1['low'].iloc[bos_idx:].idxmin()) if len(df_h1) > bos_idx else None
    gaps = get_internal_gaps(df_h1, stype, bos_idx, require_fresh=fresh_flag, peak_idx=peak_idx_fvg)
    z_lo = ENTRY_ZONE_LO if zone_lo is None else zone_lo
    # FVG biasa: cukup field C1 (entry) & C3 (OCL) valid — tanpa syarat volume "kuat"
    gaps = [g for g in gaps
            if g.get('c3_open', 0) > 0
            and g.get('c1_close', 0) > 0]
    # Filter FVG yang straddle CHOCH
    if choch_level:
        if stype == "Long":
            gaps = [g for g in gaps if g['bottom'] >= choch_level]
        else:
            gaps = [g for g in gaps if g['top'] <= choch_level]
    # Filter ZONA ENTRY: GAP-nya sendiri (ujung masuk/entrance — top[Long]/bottom[Short], sisi
    # yang sama dgn trigger1/gap_entry_point) harus di retrace z_lo..HI dari range BOS.
    # Sebelumnya dicek pakai C1.close; sekarang gap-nya sendiri yg harus berada di 61.8%+ itu.
    if choch_level and len(df_h1) > bos_idx:
        if stype == "Long":
            B = float(df_h1['high'].iloc[bos_idx:].max())
            L = float(choch_level)
            rng = B - L
            if rng > 0:
                lo = B - ENTRY_ZONE_HI * rng   # batas terdalam (CHOCH)
                hi = B - z_lo * rng            # batas terdangkal
                gaps = [g for g in gaps if lo <= g.get('top', 0) <= hi]
        else:
            B = float(df_h1['low'].iloc[bos_idx:].min())
            L = float(choch_level)
            rng = L - B
            if rng > 0:
                lo = B + z_lo * rng            # batas terdangkal
                hi = B + ENTRY_ZONE_HI * rng   # batas terdalam (CHOCH)
                gaps = [g for g in gaps if lo <= g.get('bottom', 0) <= hi]
    # MAX_GAP_PCT: gap tidak boleh terlalu besar
    result = []
    for g in gaps:
        gap_size = g['top'] - g['bottom']
        ocl      = float(g.get('c3_open', g['bottom'] if stype == 'Short' else g['top']))
        if ocl > 0 and MAX_GAP_PCT > 0 and gap_size / ocl > MAX_GAP_PCT:
            continue
        # Fresh-C1: cek apakah FVG masih valid (ujung wick C1 belum tersentuh,
        # atau sudah tersentuh tapi harga belum lari 2R ke arah BOS).
        # Injek _sl_dist ke gap dict agar c1_is_fresh bisa pakai BOS range yg akurat.
        if fresh_flag:
            g['_sl_dist'] = SL_CAP_RANGE * rng if rng > 0 else 0
            if not c1_is_fresh(df_h1, g, stype):
                continue
        result.append(g)
    return result


def c1_is_fresh(df, gap, stype):
    """FVG hangus jika setelah puncak terbentuk ada candle yang menyentuh ujung wick C1
    (bottom gap untuk Long = high[C1], top gap untuk Short = low[C1]) — gap sudah 100% terisi.
    Cek dimulai SETELAH peak_idx karena leg impulsif ke puncak wajar melewati area C1."""
    c3i = gap.get('c3_idx')
    if c3i is None:
        return True
    peak_idx = gap.get('_peak_idx')
    start_k  = (int(peak_idx) + 1) if peak_idx is not None else (int(c3i) + 1)
    start_k  = max(start_k, int(c3i) + 1)
    if stype == "Long":
        c1_wick = float(gap.get('bottom', 0))   # high[C1]
    else:
        c1_wick = float(gap.get('top', 0))       # low[C1]
    if c1_wick <= 0:
        return True
    for k in range(start_k, len(df)):
        if stype == "Long" and float(df['low'].iloc[k]) <= c1_wick:
            return False
        if stype == "Short" and float(df['high'].iloc[k]) >= c1_wick:
            return False
    return True


def gap_entry_point(df, gap, stype, peak_idx):
    """Trigger1 FVG = titik AWAL masuk gap yang MASIH KOSONG (bukan C1.close lagi).
    Default = ujung C3 (top gap untuk Long = low[C3], bottom gap untuk Short = high[C3]) —
    ini sisi gap yang PERTAMA disentuh candle saat harga retrace balik ke arah gap.
    Kalau ANTARA C3 dan puncak ada candle H1 yang sudah masuk SEBAGIAN ke gap (partial fill —
    belum full sampai C1, karena full-fill itu domain c1_is_fresh/freshness terpisah dan
    gap-nya sudah dibuang duluan kalau itu terjadi), trigger digeser ke titik TERDALAM yang
    sudah pernah disentuh candle tsb. Supaya bot tidak memantau level yang secara historis
    sudah "terpakai" — sisa area kosong yang tersisa itulah yang jadi trigger baru."""
    c3i = gap.get('c3_idx')
    if c3i is None:
        return float(gap['top']) if stype == 'Long' else float(gap['bottom'])
    scan_end = int(peak_idx) if peak_idx is not None else (len(df) - 1)
    scan_end = min(scan_end, len(df) - 1)
    if stype == 'Long':
        raw_edge = float(gap['top'])      # low[C3] = ujung gap paling atas (paling dekat harga)
        far_edge = float(gap['bottom'])   # high[C1] = ujung gap paling dalam
        deepest  = raw_edge
        for k in range(int(c3i) + 1, scan_end + 1):
            lo_k = float(df['low'].iloc[k])
            if lo_k < deepest:
                deepest = max(lo_k, far_edge)   # jangan lewat far_edge (itu domain c1_is_fresh)
        return deepest
    else:
        raw_edge = float(gap['bottom'])   # high[C3] = ujung gap paling bawah (paling dekat harga)
        far_edge = float(gap['top'])      # low[C1] = ujung gap paling dalam
        deepest  = raw_edge
        for k in range(int(c3i) + 1, scan_end + 1):
            hi_k = float(df['high'].iloc[k])
            if hi_k > deepest:
                deepest = min(hi_k, far_edge)
        return deepest


def c2_wick_still_valid(df, gap, stype, sl_dist):
    """Cek apakah C2 wick masih valid sebagai entry saat ENTRY_C2_WICK=True.
    Kondisi HANGUS: C1.close sudah tersentuh DAN setelah itu harga jalan ke arah BOS
    melebihi C2_WICK_SKIP_R × sl_dist dari C1.close, tanpa pernah menyentuh C2 wick duluan.
    Kondisi VALID: C1.close belum tersentuh (normal/fresh), atau sudah tersentuh tapi
    harga sempat balik ke C2 wick sebelum lari jauh.
    sl_dist = jarak SL sebenarnya yg dipakai (10% BOS range)."""
    c3i = gap.get('c3_idx')
    c1c = float(gap.get('c1_close', 0))
    c2_entry = float(gap.get('c2_low' if stype == 'Long' else 'c2_high', 0))
    if c3i is None or c1c <= 0 or c2_entry <= 0 or sl_dist <= 0:
        return True   # data kurang -> asumsikan valid, biarkan filter lain yg handle

    skip_threshold = 2.0 * sl_dist
    n = len(df)
    c1_touched = False

    for k in range(int(c3i) + 1, n):
        lo = float(df['low'].iloc[k])
        hi = float(df['high'].iloc[k])
        if stype == 'Long':
            if not c1_touched:
                if lo <= c1c:
                    c1_touched = True
                # sebelum C1 tersentuh -> C2 wick pasti belum masalah
            else:
                # C1 sudah tersentuh: cek apakah C2 wick sempat tersentuh duluan
                if lo <= c2_entry:
                    return True   # harga balik ke C2 wick -> masih valid
                # cek apakah harga sudah lari ke arah BOS > skip_threshold dari C1.close
                if hi >= c1c + skip_threshold:
                    return False  # jalan duluan > 2R -> C2 wick hangus
        else:  # Short
            if not c1_touched:
                if hi >= c1c:
                    c1_touched = True
            else:
                if hi >= c2_entry:
                    return True   # harga balik ke C2 wick -> masih valid
                if lo <= c1c - skip_threshold:
                    return False  # jalan duluan > 2R -> C2 wick hangus

    return True  # tidak ada kondisi hangus terdeteksi di data yg tersedia


# ============================================================
# FUNGSI ORDER
# ============================================================

def place_market_order(symbol, side, entry, sl, trail_dist):
    """
    Market order dengan trailing stop.
    trail_dist = jarak trailing dalam harga (= TRAIL_STOP × dist).
    SL awal = entry - dist (Long) / entry + dist (Short).
    """
    try:
        info    = get_instrument_info(symbol)
        res_bal = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        acct    = res_bal['result']['list'][0]
        balance = float(acct['totalEquity'])
        avail   = float(acct.get('totalAvailableBalance') or balance)
        risk_usd = balance * RISK_PCT
        dist     = abs(entry - sl)
        if dist == 0:
            print(f"⚠️ {symbol}: dist entry-SL = 0, skip.")
            return None

        min_dist = entry * 0.002   # 0.2% — sinkron dengan outer check dan backtest MIN_DIST_PCT
        if dist < min_dist:
            dist = min_dist
            sl   = entry - dist if side == "Buy" else entry + dist

        raw_qty = risk_usd / dist
        qty     = round_qty(raw_qty, info['qty_step'])
        if qty < info['min_qty']:
            print(f"⚠️ {symbol}: Qty {qty} < minOrderQty {info['min_qty']}, skip.")
            return None

        order_value = qty * entry
        if order_value < 5.0:
            print(f"⚠️ {symbol}: Order value ~${order_value:.2f} < $5 minimum Bybit, skip "
                  f"(balance ${balance:.2f}, risk ${risk_usd:.2f}, dist {dist:.6f}).")
            return None

        sl_r         = round_price(sl,         info['tick_size'])
        trail_dist_r = round_price(trail_dist,  info['tick_size'])
        if trail_dist_r <= 0:
            trail_dist_r = round_price(dist * TRAIL_STOP, info['tick_size'])

        lev_int = 10
        try:
            max_lev = float(info.get('max_leverage', 10))
            lev_int = int(min(LEVERAGE, max_lev))
            res_lev = session.set_leverage(category=CATEGORY, symbol=symbol,
                                           buyLeverage=str(lev_int), sellLeverage=str(lev_int))
            if res_lev.get('retCode', -1) not in (0, 110043):
                print(f"   ⚠️ {symbol}: set_leverage gagal: {res_lev.get('retMsg','')} "
                      f"(code:{res_lev.get('retCode')}) — coba lanjut")
        except Exception as e:
            if '110043' not in str(e):
                print(f"   ⚠️ {symbol}: set_leverage error: {e} — coba lanjut")

        required_margin = (qty * entry) / lev_int
        if required_margin > avail * 0.9:
            print(f"⚠️ {symbol}: Margin tidak cukup — butuh ~${required_margin:.2f} "
                  f"(lev {lev_int}x), avail ${avail:.2f} / equity ${balance:.2f}. Skip.")
            return None

        print(f"   Balance:{balance:.2f} Avail:{avail:.2f} Risk:{risk_usd:.2f} Dist:{dist:.6f} "
              f"Trail:{trail_dist:.6f} Qty:{qty} SL:{sl_r} Lev:{lev_int}x "
              f"Margin:~${required_margin:.2f}")

        res = session.place_order(
            category=CATEGORY, symbol=symbol, side=side,
            orderType="Market", qty=str(qty),
            stopLoss=str(sl_r),
            positionIdx=_pidx(side),
            timeInForce="IOC"
        )
        if res['retCode'] == 0:
            return res['result']['orderId']
        print(f"⚠️ {symbol}: Order ditolak → {res.get('retMsg','')} (code:{res['retCode']})")
        return None
    except Exception as e:
        print(f"⚠️ {symbol}: place_order error → {e}")
        return None


def close_position(symbol, side, qty_str):
    """
    Force-close posisi dengan market order reduceOnly.
    Dipakai untuk trail timeout: tutup posisi yang peak-nya stuck 3 hari.
    """
    try:
        close_side = 'Sell' if side == 'Buy' else 'Buy'
        info  = get_instrument_info(symbol)
        qty_r = round_qty(float(qty_str), info['qty_step'])
        if qty_r <= 0:
            print(f"⚠️ {symbol}: close_position qty=0, skip.")
            return False
        res = session.place_order(
            category=CATEGORY, symbol=symbol,
            side=close_side, orderType="Market",
            qty=str(qty_r), reduceOnly=True,
            positionIdx=_pidx(side), timeInForce="IOC"
        )
        if res.get('retCode') == 0:
            print(f"⏹️  {symbol}: Posisi ditutup (trail timeout) @ market")
            return True
        print(f"⚠️ {symbol}: close_position gagal → {res.get('retMsg','')} (code:{res.get('retCode')})")
        return False
    except Exception as e:
        print(f"⚠️ {symbol}: close_position error → {e}")
        return False


def place_limit_order(symbol, side, entry_p, sl_p):
    """
    Limit order GTC di entry_p, SL + trailing stop langsung dalam satu order.
    """
    try:
        info    = get_instrument_info(symbol)
        res_bal = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        acct    = res_bal['result']['list'][0]
        balance = float(acct['totalEquity'])
        avail   = float(acct.get('totalAvailableBalance') or balance)
        risk_usd = balance * RISK_PCT
        dist     = abs(entry_p - sl_p)
        if dist == 0:
            print(f"⚠️ {symbol}: dist entry-SL = 0, skip.")
            return None

        min_dist = entry_p * 0.002
        if dist < min_dist:
            dist  = min_dist
            sl_p  = entry_p - dist if side == "Buy" else entry_p + dist

        raw_qty = risk_usd / dist
        qty     = round_qty(raw_qty, info['qty_step'])
        if qty < info['min_qty']:
            print(f"⚠️ {symbol}: Qty {qty} < minOrderQty {info['min_qty']}, skip.")
            return None

        order_value = qty * entry_p
        if order_value < MIN_ORDER_USD:
            if order_value >= ORDER_BUMP_FLOOR:
                # order sudah dekat $5 -> naikkan qty agar >= $5 (SL tetap, over-risk <=1.25x)
                old_ov = order_value
                qty = round_qty(MIN_ORDER_USD / entry_p, info['qty_step'])
                if qty * entry_p < MIN_ORDER_USD:
                    qty = round_qty(qty + info['qty_step'], info['qty_step'])
                order_value = qty * entry_p
                new_risk = qty * dist
                print(f"⬆️ {symbol}: order ${old_ov:.2f}->${order_value:.2f} "
                      f"(risk ${new_risk:.2f} ~ {new_risk/risk_usd:.2f}x target).")
            else:
                print(f"⚠️ {symbol}: Order ~${order_value:.2f} < ${ORDER_BUMP_FLOOR:.0f} "
                      f"(terlalu jauh dari ${MIN_ORDER_USD:.0f}), skip "
                      f"(balance ${balance:.2f}, risk ${risk_usd:.2f}, dist {dist:.6f}).")
                return None

        entry_r  = round_price(entry_p,                     info['tick_size'])
        sl_r     = round_price(sl_p,                        info['tick_size'])
        trail_r  = round_price(TRAIL_STOP * dist,           info['tick_size'])
        active_r = round_price(
            entry_p + TRAIL_ACT_R * dist if side == "Buy"
            else entry_p - TRAIL_ACT_R * dist,             info['tick_size'])

        lev_int = 10
        try:
            max_lev = float(info.get('max_leverage', 10))
            lev_int = int(min(LEVERAGE, max_lev))
            res_lev = session.set_leverage(category=CATEGORY, symbol=symbol,
                                           buyLeverage=str(lev_int), sellLeverage=str(lev_int))
            if res_lev.get('retCode', -1) not in (0, 110043):   # 110043 = sudah di leverage ini
                print(f"   ⚠️ {symbol}: set_leverage gagal: {res_lev.get('retMsg','')} "
                      f"(code:{res_lev.get('retCode')}) — coba lanjut")
        except Exception as e:
            if '110043' not in str(e):
                print(f"   ⚠️ {symbol}: set_leverage error: {e} — coba lanjut")

        # Pre-check margin pakai available balance (bukan totalEquity) — sudah dikurangi open orders
        required_margin = (qty * entry_p) / lev_int
        if required_margin > avail * 0.9:
            print(f"⚠️ {symbol}: Margin tidak cukup — butuh ~${required_margin:.2f} "
                  f"(lev {lev_int}x), avail ${avail:.2f} / equity ${balance:.2f}. Skip.")
            return None

        print(f"   Balance:{balance:.2f} Avail:{avail:.2f} Risk:{risk_usd:.2f} Dist:{dist:.6f} "
              f"Trail:{trail_r} ActiveP:{active_r} Qty:{qty} Entry:{entry_r} SL:{sl_r} "
              f"Lev:{lev_int}x Margin:~${required_margin:.2f}")

        if USE_TP:
            tp_r = round_price(entry_p + RR_TP * dist if side == "Buy" else entry_p - RR_TP * dist, info['tick_size'])
            res = session.place_order(
                category=CATEGORY, symbol=symbol, side=side,
                orderType="Limit", qty=str(qty), price=str(entry_r),
                stopLoss=str(sl_r), takeProfit=str(tp_r),
                positionIdx=_pidx(side), timeInForce="GTC")
        else:
            res = session.place_order(
                category=CATEGORY, symbol=symbol, side=side,
                orderType="Limit", qty=str(qty), price=str(entry_r),
                stopLoss=str(sl_r), trailingStop=str(trail_r), activePrice=str(active_r),
                positionIdx=_pidx(side), timeInForce="GTC")
        if res['retCode'] == 0:
            return res['result']['orderId']
        print(f"⚠️ {symbol}: Limit order ditolak → {res.get('retMsg','')} (code:{res['retCode']})")
        return None
    except Exception as e:
        print(f"⚠️ {symbol}: place_limit_order error → {e}")
        return None


def place_market_entry(coin, side, curr_price, sl_p, tp_p):
    """Entry MARKET (untuk inducement) dgn SL+TP langsung. Sizing by risk. Return (order_id, qty) atau (None,None)."""
    try:
        info = get_instrument_info(coin)
        res_bal = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        acct = res_bal['result']['list'][0]
        balance = float(acct['totalEquity'])
        avail   = float(acct.get('totalAvailableBalance') or balance)
        risk_usd = balance * RISK_PCT
        dist = abs(curr_price - sl_p)
        if dist <= 0: return None, None
        min_dist = curr_price * 0.002
        if dist < min_dist:
            dist = min_dist
            sl_p = curr_price - dist if side == "Buy" else curr_price + dist
        qty = round_qty(risk_usd / dist, info['qty_step'])
        if qty < info['min_qty']:
            print(f"⚠️ {coin}: induce qty {qty} < min {info['min_qty']}, skip."); return None, None
        if qty * curr_price < MIN_ORDER_USD:
            if qty * curr_price >= ORDER_BUMP_FLOOR:
                qty = round_qty(MIN_ORDER_USD / curr_price, info['qty_step'])
                if qty * curr_price < MIN_ORDER_USD:
                    qty = round_qty(qty + info['qty_step'], info['qty_step'])
            else:
                print(f"⚠️ {coin}: induce order ~${qty*curr_price:.2f} terlalu kecil, skip."); return None, None
        lev_int = int(min(LEVERAGE, float(info.get('max_leverage', 10))))
        try:
            session.set_leverage(category=CATEGORY, symbol=coin, buyLeverage=str(lev_int), sellLeverage=str(lev_int))
        except Exception as e:
            if '110043' not in str(e): print(f"   ⚠️ {coin}: set_leverage: {e}")
        required_margin = (qty * curr_price) / lev_int
        if required_margin > avail * 0.85:
            print(f"⚠️ {coin}: induce margin ~${required_margin:.2f} > avail ${avail:.2f}, skip."); return None, None
        tick = info['tick_size']
        sl_r = round_price(sl_p, tick)
        order_kwargs = dict(category=CATEGORY, symbol=coin, side=side,
                            orderType="Market", qty=str(qty),
                            stopLoss=str(sl_r), positionIdx=_pidx(side), timeInForce="IOC")
        if tp_p is not None:
            tp_r = round_price(tp_p, tick)
            order_kwargs['takeProfit'] = str(tp_r)
        else:
            tp_r = None
        res = session.place_order(**order_kwargs)
        if res['retCode'] == 0:
            print(f"   market entry: qty {qty} SL {sl_r}" + (f" TP {tp_r}" if tp_r else "") +
                  f" (margin ~${required_margin:.2f}, risk ${risk_usd:.2f})")
            return res['result']['orderId'], qty
        print(f"⚠️ {coin}: induce order ditolak → {res.get('retMsg','')} (code:{res['retCode']})")
        return None, None
    except Exception as e:
        print(f"⚠️ {coin}: place_market_entry error → {e}")
        return None, None


def check_inducement_entry(coin, df_h1, sh_h1, sl_h1):
    """Inducement entry (market, KEBALIK arah BOS besar). Berdampingan dgn limit FVG.
    BOS besar Long: inducement long 1-1 di pita 0-61% (dekat puncak); low-nya disapu M5 -> entry SHORT.
    BOS besar Short: cerminannya -> entry LONG. SL = 10% range BOS besar, TP 1:RR_TP."""
    if not INDUCEMENT_ENTRY or (not ALLOW_HEDGE and coin in active_positions):
        return False
    for stype in ("Long", "Short"):
        a = bos_anchors(df_h1, sh_h1, sl_h1, stype)
        if not a:
            continue
        # BOS besar WAJIB punya FVG di zona (sama syarat dgn jalur FVG limit).
        # Tak ada FVG -> BOS ini tak dipakai untuk entry FVG limit MAUPUN entry IDM.
        # NB1: require_fresh=False -> cek ini cuma soal "BOS-nya valid/pernah ada FVG",
        # bukan soal FVG-nya masih bisa dientry. Kalau ikut REQUIRE_FRESH_C1 global,
        # begitu limit FVG TERISI (entry = C1.close, jadi otomatis "disentuh"),
        # _get_fvgs balik kosong dan IDM jadi ikut mati padahal harusnya berdampingan.
        # NB2: zone_lo PAKAI STATIS (ENTRY_ZONE_LO), BUKAN deepest_retrace_lo() yg dinamis.
        # zone_lo dinamis itu utk keperluan PENEMPATAN limit FVG (area yg sudah dilewati
        # retrace tak dipakai lagi) — floor-nya naik terus seiring retrace makin dalam.
        # Kalau dipakai di sini, makin dalam retrace (= makin kuat alasan IDM utk entry
        # reversal), makin besar juga kemungkinan FVG lama "terhapus" dari hasil, sehingga
        # gate ini malah memblokir IDM justru pas momen yg paling IDM butuhkan.
        if not _get_fvgs(df_h1, stype, a['bos_idx'], a['choch_level'], zone_lo=ENTRY_ZONE_LO, require_fresh=False):
            continue
        B = a['B']; rng = a['bos_rng']
        # pita TITIK TRIGGER (level IDM) = 35-55% range BOS besar (dari puncak/lembah ke arah choch)
        if stype == "Long":
            band_lo, band_hi = B - INDUCEMENT_ZONE_HI * rng, B - INDUCEMENT_ZONE_LO * rng
        else:
            band_lo, band_hi = B + INDUCEMENT_ZONE_LO * rng, B + INDUCEMENT_ZONE_HI * rng
        # Jendela waktu: bos kecil dicari HANYA dari choch sampai PUNCAK (impuls), bukan setelah puncak.
        ts_lo = float(df_h1['ts'].iloc[a['bos_idx']])
        ts_hi = float(df_h1['ts'].iloc[a['peak_idx']])
        # Struktur bos kecil: H1 atau M5. TRIGGER (sweep): SELALU M5 close.
        df_m5 = get_data(coin, "5", limit=300)
        if df_m5 is None or len(df_m5) < 3:
            continue
        df_struct = df_h1 if INDUCEMENT_TF == "60" else df_m5
        idm = find_inducement(df_struct, stype, band_lo, band_hi, n=INDUCEMENT_SWING, ts_lo=ts_lo, ts_hi=ts_hi)
        if idm is None:
            continue
        prot = idm['prot']
        # SAPUAN di M5: candle M5 SETELAH puncak yang MENYENTUH level IDM (touch, tak harus tembus).
        # Entry HANYA bila sentuhan PERTAMA = candle M5 CLOSED TERAKHIR (sapuan baru, edge-trigger).
        # IDM harus fresh live: trigger hanya valid kalau belum pernah disentuh
        # sebelum bot jalan. Ini mencegah replay saat redeploy.
        bot_start_ms = bot_start_ts * 1000
        # Cek apakah trigger SUDAH pernah disentuh sebelum bot jalan (historis)
        m5_hist = df_m5[(df_m5['ts'] > ts_hi) & (df_m5['ts'] < bot_start_ms)]
        if stype == "Long":
            already_swept = len(m5_hist[m5_hist['low'] <= prot]) > 0
        else:
            already_swept = len(m5_hist[m5_hist['high'] >= prot]) > 0
        if already_swept:
            continue   # trigger sudah pernah disentuh sebelum bot jalan → skip, tidak fresh

        # Cek sweep dari candle M5 setelah puncak DAN setelah bot_start_ts (live)
        # HANYA candle CLOSED yang dipakai (candle terakhir/iloc[-1] masih berjalan,
        # OHLC-nya bisa berubah, dan low/high-nya BELUM final).
        m5_after = df_m5[(df_m5['ts'] > ts_hi) & (df_m5['ts'] >= bot_start_ms)]
        if len(m5_after) < 2:
            continue
        m5_after_closed = m5_after.iloc[:-1]   # buang candle yg masih berjalan
        # Ambil baris LANGSUNG dari hasil filter (bukan df_m5.loc[breaches[0]] via index terpisah
        # -> pernah kejadian salah ambil candle SEBELUM sweep asli, kemungkinan isu alignment index).
        if stype == "Long":
            breach_rows = m5_after_closed[m5_after_closed['low'] <= prot]
        else:
            breach_rows = m5_after_closed[m5_after_closed['high'] >= prot]
        if breach_rows.empty:
            # ── Log tiap siklus: IDM menunggu trigger tersentuh (paritas dgn FVG "menunggu sentuhan") ──
            e_stype_wait = "Short" if stype == "Long" else "Long"
            curr_price_idm = float(df_m5['close'].iloc[-1])
            _pct_idm = abs(curr_price_idm - prot) / prot * 100 if prot else 0
            print(f"👁️  IDM {coin} {e_stype_wait} | now:{curr_price_idm:.6f} trigger:{prot:.6f} | "
                  f"menunggu sentuhan ({_pct_idm:.2f}% lagi)")
            continue                       # IDM belum disapu (closed) sejak bot jalan -> tunggu
        # Sweep terjadi live setelah bot jalan → masuk idm_pending.
        sig = (stype, round(a['choch_level'], 10), round(a['swing_val'], 10))
        if inducement_done.get((coin, stype)) == sig:
            continue                       # struktur ini sudah pernah di-entry -> jangan ulang
        if stype == "Long":
            side, e_stype = "Sell", "Short"
        else:
            side, e_stype = "Buy", "Long"
        curr = float(df_m5.iloc[-1]['close'])
        trig = breach_rows.iloc[0]         # candle M5 yg BENAR-BENAR menyapu trigger (closed, pertama)
        # Guard defensif: pastikan trig yg kepilih memang betul2 menyapu prot. Kalau tidak,
        # ini indikasi bug serupa lagi -> skip drpd lanjut dgn data yg salah.
        if (stype == "Long" and float(trig['low']) > prot) or (stype == "Short" and float(trig['high']) < prot):
            print(f"⚠️ {coin}: IDM {stype} trig candle tidak valid (low/high tak menyapu prot={prot:.6g}), skip.")
            continue
        sl_dist = SL_CAP_RANGE * rng
        if _akey(coin, e_stype) in active_positions or _akey(coin, e_stype) in idm_pending:
            continue                       # sisi IDM ini sudah terbuka / limit sudah terpasang

        # Filter konfluensi funding: blokir pasang limit IDM baru selama funding window AND gak searah.
        if FUNDING_FILTER and in_funding_window() and not funding_favors(e_stype, coin):
            rate = get_funding_rate(coin)
            print(f"   {coin}: IDM {e_stype} skip -> funding window aktif, gak searah (rate={rate})")
            continue

        if IDM_LIMIT_ENTRY or IDM_M5_ENGULF:
            # Filter momentum
            if INDUCEMENT_MOMENTUM_FILTER:
                eaten = momentum_eaten(df_h1, a['peak_idx'], stype)
                if eaten is not True:
                    reason = "data kurang (<min candle sejak puncak)" if eaten is None else "tak ada candle makan candle"
                    print(f"   {coin}: IDM {stype} skip -> momentum filter gagal ({reason})")
                    continue

            if IDM_M5_ENGULF:
                # ── Cross-check: kalau FVG (arah searah BOS) untuk BOS besar SAMA PERSIS sudah
                # tersentuh trigger-nya duluan, arah IDM ini (kebalikan BOS) jangan diambil —
                # sudah "milik" FVG. Cegah risk dobel di coin+arah yang sama.
                if _fvg_trigger_touched_for_bos(coin, a['swing_val'], a['choch_level']):
                    print(f"⏭️ {coin}: IDM {stype} skip -> trigger FVG (searah BOS) sudah tersentuh duluan "
                          f"untuk BOS besar ini, arah {e_stype} sudah diambil FVG")
                    continue
                # ── M5 ENGULF MODE: simpan state monitor, entry nanti saat engulfing dikonfirmasi ──
                print(f"🎯 {coin}: IDM {stype} trigger={prot:.6g} tersapu → monitor M5 engulfing ({e_stype})")
                idm_pending[_akey(coin, e_stype)] = {
                    'coin': coin, 'side': side, 'e_stype': e_stype,
                    'order_id': None,
                    'entry': None, 'sl': None, 'placed_ts': time.time(),
                    'trigger': prot, 'rng': rng, 'sl_dist': sl_dist,
                    'swing_val': a['swing_val'], 'choch_level': a['choch_level'],
                    'peak_val': a['peak_val'], 'bos_type': e_stype,
                    # M5 monitor state — `trig` = candle M5 yang SUDAH TERBUKTI menyapu trigger H1
                    # (closed terakhir). Langsung jadi fokus pertama (sama seperti FVG) — TIDAK ADA
                    # LAGI trigger2/tunggu candle setelahnya. Validasi kualitas diserahkan ke filter
                    # EMA20 di check_m5_engulfing.
                    'm5_triggered': True,   # sentuhan trigger H1 sudah pasti terjadi di candle `trig`
                    'm5_focus_hi': float(trig['high']), 'm5_focus_lo': float(trig['low']),
                    'm5_focus_idx': -1,
                    'm5_focus_initialized': False,   # fokus scan sebenarnya baru diisi setelah gate EMA H1 selesai
                    'm5_hangus': False,
                    # Gate EMA20 H1 (dicek sekali saat trigger tersentuh, lihat h1_ema_gate()).
                    # trigger_ts langsung dari waktu candle `trig` (candle M5 yg terbukti menyapu
                    # trigger H1) — sudah pasti tahu persis kapan trigger tersentuh, tak perlu tunggu.
                    'h1_ema_resolved': False, 'h1_ema_dir': None, 'h1_ema_trigger_ts': float(trig['ts']),
                    'h1_decision_ts_close': None,
                }
                inducement_done[(coin, stype)] = sig
                save_state()
                rec = (
                    f"════ IDM M5 ENGULF MONITOR ════\n"
                    f"  {coin} | menunggu engulfing {e_stype} M5 | trigger={prot:.6g}\n"
                    f"  BOS BESAR ({stype}): break={a['swing_val']:.6g} choch={a['choch_level']:.6g} "
                    f"puncak={a['peak_val'] if a['peak_val'] is not None else a['B']:.6g} range={rng:.6g}\n"
                    f"  IDM trigger={prot:.6g}@{idm['prot_idx']} | batal jika >±{IDM_CANCEL_RANGE_PCT*100:.0f}% range\n"
                    f"  SWEEP CANDLE M5 (trig): ts={int(trig['ts'])} low={float(trig['low']):.6g} "
                    f"high={float(trig['high']):.6g} close={float(trig['close']):.6g} — langsung jadi "
                    f"fokus pertama, mulai cari engulfing"
                )
                log_entry(rec)
                if (not ALLOW_HEDGE) and coin in pending:
                    for d, st in list(pending[coin].items()):
                        if st.get('order_id'): cancel_order(coin, st['order_id'])
                    pending.pop(coin, None)
                return True

            # ── LIMIT MODE (IDM_M5_ENGULF=False, IDM_LIMIT_ENTRY=True) ──
            if _fvg_trigger_touched_for_bos(coin, a['swing_val'], a['choch_level']):
                print(f"⏭️ {coin}: IDM {stype} skip -> trigger FVG (searah BOS) sudah tersentuh duluan "
                      f"untuk BOS besar ini, arah {e_stype} sudah diambil FVG")
                continue
            pidx = idm.get('prot_idx')
            if pidx is None or pidx < 0 or pidx >= len(df_struct):
                continue
            idm_candle = df_struct.iloc[pidx]
            hi_c, lo_c = float(idm_candle['high']), float(idm_candle['low'])
            rng_c = hi_c - lo_c
            if rng_c <= 0:
                continue
            if e_stype == "Long":
                entry_p = lo_c + IDM_LIMIT_FIB * rng_c
                sl_p = entry_p - sl_dist
            else:
                entry_p = hi_c - IDM_LIMIT_FIB * rng_c
                sl_p = entry_p + sl_dist
            print(f"🎯 {coin}: INDUCEMENT {stype} disentuh (level {prot:.6g}) → LIMIT {e_stype} @ "
                  f"{entry_p:.6g} ({IDM_LIMIT_FIB*100:.0f}% range candle H1 IDM {lo_c:.6g}-{hi_c:.6g} @idx{pidx}) | SL {sl_p:.6g}")
            oid = place_limit_order(coin, side, entry_p, sl_p)
            if oid:
                idm_pending[_akey(coin, e_stype)] = {
                    'coin': coin, 'side': side, 'e_stype': e_stype, 'order_id': oid,
                    'entry': entry_p, 'sl': sl_p, 'placed_ts': time.time(),
                    'trigger': prot, 'rng': rng,
                    'swing_val': a['swing_val'], 'choch_level': a['choch_level'],
                    'peak_val': a['peak_val'], 'bos_type': e_stype,
                }
                inducement_done[(coin, stype)] = sig
                save_state()
                rec = (
                    f"════ LIMIT INDUCEMENT ({IDM_LIMIT_FIB*100:.0f}% range candle H1 IDM) ════\n"
                    f"  {coin} | LIMIT {e_stype} @ {entry_p:.6g} | SL {sl_p:.6g}\n"
                    f"  BOS BESAR ({stype}): break={a['swing_val']:.6g} choch={a['choch_level']:.6g} "
                    f"puncak={a['peak_val'] if a['peak_val'] is not None else a['B']:.6g} range={rng:.6g}\n"
                    f"  IDM trigger={prot:.6g}@{idm['prot_idx']} (terakhir-di-pita; semua {[round(x,6) for x in idm.get('all_triggers',[prot])]})\n"
                    f"  CANDLE H1 IDM @idx{pidx}: low={lo_c:.6g} high={hi_c:.6g} "
                    f"→ entry {IDM_LIMIT_FIB*100:.0f}% = {entry_p:.6g}\n"
                    f"  Momentum filter: {'ON, lolos (candle makan candle)' if INDUCEMENT_MOMENTUM_FILTER else 'OFF'}"
                )
                log_entry(rec)
                if (not ALLOW_HEDGE) and coin in pending:
                    for d, st in list(pending[coin].items()):
                        if st.get('order_id'): cancel_order(coin, st['order_id'])
                    pending.pop(coin, None)
                return True
            continue

        # --- jalur lama: MARKET di harga sweep (kalau IDM_LIMIT_ENTRY=False) ---
        if e_stype == "Short":
            sl_p, tp_p = curr + sl_dist, curr - RR_TP * sl_dist
        else:
            sl_p, tp_p = curr - sl_dist, curr + RR_TP * sl_dist
        print(f"🎯 {coin}: INDUCEMENT {stype} disapu (level {prot:.6g}, pita {band_lo:.6g}-{band_hi:.6g}) "
              f"→ entry {e_stype} MARKET @ ~{curr:.6g} | SL {sl_p:.6g} TP {tp_p:.6g}")
        if _akey(coin, e_stype) in active_positions:
            continue                       # sisi IDM ini sudah terbuka -> jangan dobel
        oid, qty = place_market_entry(coin, side, curr, sl_p, tp_p)
        if oid:
            active_positions[_akey(coin, e_stype)] = {
                'coin': coin,
                'side': side, 'entry': curr, 'sl': sl_p, 'dist': abs(curr - sl_p),
                'trail_dist': 0, 'trail_engaged': False, 'trail_set': True,
                'last_price': curr, 'entry_time': time.time(),
                'peak': curr, 'peak_time': time.time(),
                'swing_val': a['swing_val'], 'bos_type': e_stype, 'rev_count': 0,
                'orig_ocl': curr, 'choch_level': a['choch_level'], 'peak_val': a['peak_val'],
                'swing2': a['peak_val'], 'kind': 'inducement',
            }
            inducement_done[(coin, stype)] = sig   # tandai struktur ini sudah di-entry (anti entry-ulang)
            save_state()
            rec = (
                f"════ ENTRY INDUCEMENT ════\n"
                f"  {coin} | entry {e_stype} MARKET @ ~{curr:.6g} qty {qty}\n"
                f"  BOS BESAR ({stype}): swing-1(break)={a['swing_val']:.6g} | choch={a['choch_level']:.6g} | "
                f"swing-2(puncak/lembah)={a['peak_val'] if a['peak_val'] is not None else a['B']:.6g} | range={rng:.6g}\n"
                f"  BOS KECIL (induce {INDUCEMENT_TF} {INDUCEMENT_SWING}-{INDUCEMENT_SWING}, dari choch→puncak): "
                f"swing-1={idm['micro_val']:.6g}@{idm['micro_idx']} | "
                f"choch-TRIGGER={prot:.6g}@{idm['prot_idx']} "
                f"({(((a['B']-prot) if stype=='Long' else (prot-a['B']))/rng*100 if rng>0 else 0):.0f}% dari puncak, "
                f"terakhir-di-pita; {idm.get('n_trigger',1)} leg di pita, semua trigger {[round(x,6) for x in idm.get('all_triggers',[prot])]}) | "
                f"pita35-55%={band_lo:.6g}-{band_hi:.6g}\n"
                f"  TRIGGER(M5 close): ts={int(trig['ts'])} low={float(trig['low']):.6g} high={float(trig['high']):.6g} "
                f"close={float(trig['close']):.6g} (menyapu IDM {prot:.6g})\n"
                f"  SL={sl_p:.6g} (10% range) | TP={tp_p:.6g} (1:{RR_TP})"
            )
            log_entry(rec)
            if (not ALLOW_HEDGE) and coin in pending:   # one-way: batalkan limit FVG; hedge: biarkan
                for d, st in list(pending[coin].items()):
                    if st.get('order_id'):
                        cancel_order(coin, st['order_id'])
                pending.pop(coin, None)
            return True
    return False


def cancel_order(symbol, order_id):
    """Batalkan pending order di Bybit."""
    try:
        res = session.cancel_order(category=CATEGORY, symbol=symbol, orderId=order_id)
        if res['retCode'] == 0:
            print(f"   ✅ {symbol}: Order {order_id[:8]}… dibatalkan.")
        else:
            print(f"   ⚠️ {symbol}: Cancel gagal → {res.get('retMsg','')} (code:{res['retCode']})")
    except Exception as e:
        print(f"   ⚠️ {symbol}: cancel_order error → {e}")


def _order_exists(symbol, order_id):
    """True jika limit order masih aktif (belum filled/cancelled) di Bybit."""
    try:
        res = session.get_open_orders(category=CATEGORY, symbol=symbol, orderId=order_id)
        if res['retCode'] == 0:
            for o in res['result']['list']:
                if o.get('orderId') == order_id and \
                        o.get('orderStatus') in ('New', 'PartiallyFilled', 'Untriggered'):
                    return True
            return False
    except Exception:
        pass
    return False


def _order_was_filled(symbol, order_id):
    """True jika order sudah Filled (cek history Bybit)."""
    try:
        res = session.get_order_history(
            category=CATEGORY, symbol=symbol, orderId=order_id, limit=1
        )
        if res['retCode'] == 0 and res['result']['list']:
            return res['result']['list'][0].get('orderStatus') == 'Filled'
    except Exception:
        pass
    return False


def get_open_position(symbol, want_side=None):
    try:
        res = session.get_positions(category=CATEGORY, symbol=symbol)
        if res['retCode'] == 0:
            for pos in res['result']['list']:
                if float(pos['size']) <= 0:
                    continue
                if ALLOW_HEDGE and want_side is not None and pos.get('side') != want_side:
                    continue            # hedge: ambil HANYA sisi yg diminta (Buy/Sell)
                return pos
        return None
    except:
        return None


def move_sl(symbol, new_sl, side="Buy"):
    try:
        res = session.set_trading_stop(
            category=CATEGORY, symbol=symbol,
            stopLoss=str(new_sl),
            positionIdx=_pidx(side)
        )
        return res['retCode'] == 0
    except:
        return False


# ============================================================
# TRAILING SL + REVERSE POSITION
# ============================================================

def _get_actual_exit_price(symbol):
    """
    Query Bybit closed PnL untuk ambil harga exit actual posisi terakhir.
    Lebih akurat dari last_price (mark price di cek sebelumnya).
    """
    try:
        res = session.get_closed_pnl(category=CATEGORY, symbol=symbol, limit=1)
        if res['retCode'] == 0 and res['result']['list']:
            last = res['result']['list'][0]
            exit_p = float(last.get('avgExitPrice', 0))
            if exit_p > 0:
                return exit_p
    except Exception as e:
        print(f"⚠️ {symbol}: get_closed_pnl error: {e}")
    return None


def check_trailing_sl(key):
    """Dipanggil tiap M5 close untuk SATU posisi (key = 'COIN' one-way / 'COIN|Long' hedge).
    Cek apakah posisi tutup. Jika ya hapus dari active_positions; jika buka set trailing."""
    if key not in active_positions:
        return
    p    = active_positions[key]
    coin = p.get('coin', key)
    side = p.get('side')
    pos  = get_open_position(coin, side)

    if pos is None:
        actual_exit = _get_actual_exit_price(coin)
        exit_str    = f"{actual_exit:.6f}" if actual_exit else "?"
        entry       = p['entry']
        orig_ocl    = p.get('orig_ocl', entry)

        # (reverse-on-SL dibuang — SMC inti: SL kena = trade selesai, tidak balik arah)
        print(f"📭 {coin} {p.get('bos_type','')}: Posisi tutup @ {exit_str}.")
        done_setups[coin] = {
            'swing_val': p.get('swing_val'),
            'stype'    : p.get('bos_type'),
            'used_ocl' : orig_ocl,
        }
        del active_positions[key]
        return

    # Posisi masih buka — update last_price, peak, dan cek trail timeout
    try:
        curr_price = float(pos['markPrice'])
        active_positions[key]['last_price'] = curr_price

        entry = p['entry']
        dist  = p.get('dist', 0)
        side  = p['side']

        # Track peak (favorable extreme) dan waktu terakhir peak bergerak
        peak      = p.get('peak', entry)
        peak_time = p.get('peak_time', p.get('entry_time', time.time()))
        new_peak  = max(peak, curr_price) if side == 'Buy' else min(peak, curr_price)
        if new_peak != peak:
            active_positions[key]['peak']      = new_peak
            active_positions[key]['peak_time'] = time.time()
            peak_time = time.time()

        # Trail timeout: close jika peak tidak bergerak selama TRAIL_TIMEOUT_DAYS hari
        timeout_sec = TRAIL_TIMEOUT_DAYS * 24 * 3600
        if time.time() - peak_time > timeout_sec:
            qty_pos = pos.get('size', '0')
            hours_stuck = (time.time() - peak_time) / 3600
            print(f"⏰ {coin}: Trail timeout {TRAIL_TIMEOUT_DAYS} hari "
                  f"(peak stuck {hours_stuck:.1f}h) — force close @ market")
            if close_position(coin, side, qty_pos):
                done_setups[coin] = {
                    'swing_val': p.get('swing_val'),
                    'stype'    : p.get('bos_type'),
                    'used_ocl' : p.get('orig_ocl', entry),
                }
                del active_positions[key]
            return

        # Pasang trailing stop via set_trading_stop saat pertama posisi terdeteksi
        # activePrice = entry + TRAIL_ACT_R×dist → trail aktif setelah +1.5R profit (sinkron backtest)
        if (not USE_TP) and TRAIL_STOP > 0 and dist > 0 and not p.get('trail_set', False):
            trail_dist = p.get('trail_dist', TRAIL_STOP * dist)
            info       = get_instrument_info(coin)
            tick       = info.get('tick_size', 0.0001)
            trail_r    = round_price(trail_dist, tick)
            active_p   = round_price(entry + TRAIL_ACT_R * dist if side == "Buy" else entry - TRAIL_ACT_R * dist, tick)
            print(f"🔧 {coin}: Pasang trail: trailingStop={trail_r} activePrice={active_p} "
                  f"(entry={entry:.6f} dist={dist:.6f} = {dist/entry*100:.3f}%, act={TRAIL_ACT_R}R)")
            if trail_r > 0 and active_p > 0:
                try:
                    res_ts = session.set_trading_stop(
                        category=CATEGORY, symbol=coin,
                        trailingStop=str(trail_r),
                        activePrice=str(active_p),
                        positionIdx=_pidx(side)
                    )
                    if res_ts['retCode'] == 0:
                        active_positions[key]['trail_set'] = True
                        print(f"📍 {coin}: Trailing stop {trail_r} dipasang "
                              f"(aktif @ {active_p} = entry+{TRAIL_ACT_R}R)")
                    else:
                        print(f"⚠️ {coin}: Gagal set trailing stop: "
                              f"{res_ts.get('retMsg','')} (code:{res_ts['retCode']})")
                except Exception as e:
                    print(f"⚠️ {coin}: set_trading_stop error: {e}")

        if (not USE_TP) and dist > 0 and not p.get('trail_engaged', False):
            if side == "Buy"  and curr_price >= entry + TRAIL_ACT_R * dist:
                active_positions[key]['trail_engaged'] = True
                print(f"✅ {coin}: Trail engaged @ {curr_price:.6f} (+{TRAIL_ACT_R}R)")
            elif side == "Sell" and curr_price <= entry - TRAIL_ACT_R * dist:
                active_positions[key]['trail_engaged'] = True
                print(f"✅ {coin}: Trail engaged @ {curr_price:.6f} (+{TRAIL_ACT_R}R)")
    except Exception:
        pass


# ============================================================
# KONEKSI
# ============================================================

def test_connection():
    try:
        res = session.get_server_time()
        if res['retCode'] == 0:
            print(f"✅ Koneksi Bybit OK | Server time: {res['result']['timeSecond']}")
            return True
        print(f"❌ Bybit error: {res}")
        return False
    except Exception as e:
        print(f"❌ Gagal konek: {e}")
        return False


# ============================================================
# REPLAY H1 — reconstruct state saat startup (fvg)
# ============================================================

def replay_h1(coin, df_h1):
    sh_h1, sl_h1 = find_last_swing_bos(df_h1)
    if not sh_h1 or not sl_h1:
        return None

    closed_h1 = df_h1.iloc[-2]
    is_long = False; is_short = False
    swing_val = None; bos_idx = None

    brk_idx = None
    for sh in sh_h1[-3:]:
        if closed_h1['close'] > sh['val']:
            is_long = True; swing_val = sh['val']; brk_idx = sh['idx']
    for sl in sl_h1[-3:]:
        if closed_h1['close'] < sl['val']:
            is_short = True; swing_val = sl['val']; brk_idx = sl['idx']

    if not (is_long or is_short):
        return None

    stype = "Short" if is_short else "Long"
    bos_idx, choch_level, _pk = impulse_anchors(stype, swing_val, brk_idx, sh_h1, sl_h1, df_h1)
    if bos_idx is None or choch_level is None:
        return None

    gaps = _get_fvgs(df_h1, stype, bos_idx, choch_level)
    if not gaps:
        return None

    bos_ts = df_h1['ts'].iloc[bos_idx]
    state  = {
        'type'        : stype,
        'phase'       : 'WAIT_FVG_TOUCH',
        'fvg_list'    : gaps,
        'fvg_idx'     : 0,
        'bos_ts'      : bos_ts,
        'bos_idx'     : bos_idx,
        'swing_val'   : swing_val,
        'choch_level' : choch_level,
    }

    choch_str = f"{choch_level:.6g}" if choch_level else "—"
    print(f"\n📊 {coin}: BOS {stype} | Swing: {swing_val:.6g} | {len(gaps)} FVG")
    print(f"   ⛔ CHOCH batal: {choch_str}")
    for gi, g in enumerate(gaps):
        ocl      = g.get('c3_open', 0)
        sbr_lvl  = g.get('c1_close', 0)
        gap_size = g['top'] - g['bottom']
        ref_p    = ocl if ocl > 0 else (g['bottom'] if stype == 'Short' else g['top'])
        lbl      = ("RBS" if stype == "Long" else "SBR") if SBR_MODE else "OCL"
        entry_v  = sbr_lvl if SBR_MODE and sbr_lvl > 0 else ocl
        mode_lbl = f"{lbl}:{entry_v:.6g}"
        print(f"   FVG {gi+1}: bot:{g['bottom']:.6g} top:{g['top']:.6g} "
              f"{mode_lbl} gap:{abs(gap_size)/ref_p*100:.3f}%" if ref_p > 0 else
              f"   FVG {gi+1}: bot:{g['bottom']:.6g} top:{g['top']:.6g} {mode_lbl}")
    return state


def reconstruct_state():
    for coin in SYMBOLS:
        try:
            time.sleep(1)
            df_h1 = get_data(coin, "60", limit=100)
            if df_h1 is None:
                continue
            state = replay_h1(coin, df_h1)
            if state:
                pending[coin] = state
        except Exception as e:
            print(f"⚠️ Replay {coin}: {e}")
    print(f"🔍 Selesai. {len(pending)} coin dimonitor.\n")


# ============================================================
# CORE LOOP — fvg strategy
# BOS H1 → FVG (C3 vol > avg20H) → OCL touch M5
# → Touch vol filter → Entry market + trailing stop
# ============================================================

# ============================================================
# CORE LOOP — SMC inti
# BOS H1 -> FVG -> Limit entry @ C1.close -> SL C1 invalidation -> Trailing
# ============================================================

def pick_bos_swing(df, sh_h1, sl_h1, stype):
    """Pilih swing-1 BOS: swing 5-5 terbaru yang di-break & menghasilkan struktur LENGKAP (choch 5-5 sah).
    Return (swing_val, brk_idx) atau (None, None)."""
    idx_arr = df.index
    up = (stype == "Long")
    swings = sh_h1 if up else sl_h1
    ext = df['high'] if up else df['low']           # break dihitung pakai WICK (high/low), konsisten dgn puncak
    def _broken(s):
        later = ext[idx_arr > s['idx']]
        if len(later) == 0: return False
        return bool((later > s['val']).any()) if up else bool((later < s['val']).any())
    cands = sorted([s for s in swings[-8:] if _broken(s)], key=lambda x: x['idx'], reverse=True)
    for s in cands:
        bi, ch, pk = impulse_anchors(stype, s['val'], s['idx'], sh_h1, sl_h1, df)
        if bi is not None and ch is not None:
            return s['val'], s['idx']
    if cands:
        return cands[0]['val'], cands[0]['idx']
    return None, None


def apply_latest_leg(df, sh, sl, stype, swing_val, brk_idx, choch_level, peak_val, B, peak_idx, bos_idx):
    """FORWARD-CHAINING sub-puncak fraktal HALUS (n=SUBLEG_BARS) -> baca leg kiri->kanan tapi
    pakai swing tervalidasi (saring noise bar). choch & swing-1 selalu ikut LEG TERAKHIR;
    ambang retrace 50% diukur PER-LEG. Telusuri tiap sub-puncak halus setelah swing-1:
      - high baru TANPA retrace>=50% leg -> EXTENSION (puncak tumbuh, choch tetap)
      - high baru SETELAH retrace>=50%   -> REBREAK -> leg baru:
            swing-1 = puncak lama, choch = protective low/high HALUS TERBARU di leg baru, swing-2 = high baru
            (tak ada protective halus di leg baru -> None / tak ada BOS)
    Return (swing_val, brk_idx, choch_level, peak_val, bos_idx) atau None."""
    fsh, fsl = find_last_swing_bos(df, n=SUBLEG_BARS)
    if stype == "Long":
        peaks = sorted([x for x in fsh if x['idx'] > brk_idx and x['val'] > swing_val], key=lambda x: x['idx'])
    else:
        peaks = sorted([x for x in fsl if x['idx'] > brk_idx and x['val'] < swing_val], key=lambda x: x['idx'])
    if not peaks:
        return (swing_val, brk_idx, choch_level, peak_val, bos_idx)

    def prot_between(i_lo, i_hi):   # protective swing HALUS TERBARU (idx terbesar) di (i_lo, i_hi)
        if stype == "Long":
            c = [x for x in fsl if i_lo <= x['idx'] < i_hi]
        else:
            c = [x for x in fsh if i_lo <= x['idx'] < i_hi]
        return max(c, key=lambda x: x['idx']) if c else None

    def retr(s2v, chv, i_from, i_to):   # retrace >= RETRACE_LOCK leg [chv..s2v] SETELAH candle swing-2?
        a = i_from + 1                  # JANGAN hitung candle swing-2 sendiri (low/high-nya bagian pembentuk swing-2)
        if a > i_to:
            return False
        if stype == "Long":
            half = s2v - RETRACE_LOCK * (s2v - chv)
            return float(df['low'].iloc[a:i_to + 1].min()) <= half
        else:
            half = s2v + RETRACE_LOCK * (chv - s2v)
            return float(df['high'].iloc[a:i_to + 1].max()) >= half

    def rebreak_choch(i_lo, i_hi):   # choch leg rebreak: swing HALUS terbaru, ATAU titik retrace TERDALAM
        nch = prot_between(i_lo, i_hi)
        if nch is not None:
            return nch['val'], nch['idx']
        seg_lo = min(i_lo + 1, i_hi)     # fallback: tak ada swing halus (mis. 1 candle besar) -> retrace terdalam
        if stype == "Long":
            s = df['low'].iloc[seg_lo:i_hi + 1]
            return (float(s.min()), int(s.idxmin())) if len(s) else None
        else:
            s = df['high'].iloc[seg_lo:i_hi + 1]
            return (float(s.max()), int(s.idxmax())) if len(s) else None

    higher = (lambda a, b: a > b) if stype == "Long" else (lambda a, b: a < b)
    # leg 0: choch = choch DALAM (launch) dari impulse_anchors, BUKAN fine-low terbaru
    cur_s1v, cur_s1i = swing_val, brk_idx
    cur_chv, cur_chi = choch_level, bos_idx
    cur_s2v, cur_s2i = peaks[0]['val'], peaks[0]['idx']
    # chain sisa sub-puncak halus
    for p in peaks[1:]:
        if not higher(p['val'], cur_s2v):
            continue
        if retr(cur_s2v, cur_chv, cur_s2i, p['idx']):     # REBREAK (retrace >=50% leg sebenarnya)
            rc = rebreak_choch(cur_s2i, p['idx'])
            if rc is None:
                return None
            cur_s1v, cur_s1i = cur_s2v, cur_s2i
            cur_chv, cur_chi = rc
            cur_s2v, cur_s2i = p['val'], p['idx']
        else:                                             # EXTENSION: choch TETAP, cuma puncak tumbuh
            cur_s2v, cur_s2i = p['val'], p['idx']
    # puncak MENTAH B di luar sub-puncak halus terakhir
    final_peak_val = cur_s2v
    if higher(B, cur_s2v):
        if retr(cur_s2v, cur_chv, cur_s2i, peak_idx):     # REBREAK ke B
            rc = rebreak_choch(cur_s2i, peak_idx)
            if rc is None:
                return None
            cur_s1v, cur_s1i = cur_s2v, cur_s2i
            cur_chv, cur_chi = rc
            final_peak_val = None     # puncak = B mentah (belum jadi swing)
        # else: EXTENSION ke B -> final_peak_val tetap cur_s2v
    return (cur_s1v, cur_s1i, cur_chv, final_peak_val, cur_chi)




def bos_anchors(df, sh_h1, sl_h1, stype):
    """Struktur BOS besar (tanpa perlu FVG) untuk arah `stype`.
    Return dict {swing_val, brk_idx, choch_level, peak_val, B, bos_idx, bos_rng} atau None bila tak ada/invalid."""
    if not sh_h1 or not sl_h1:
        return None
    swing_val, brk_idx = pick_bos_swing(df, sh_h1, sl_h1, stype)
    if swing_val is None:
        return None
    bos_idx, choch_level, peak_val = impulse_anchors(stype, swing_val, brk_idx, sh_h1, sl_h1, df)
    if bos_idx is None or choch_level is None:
        return None
    if stype == "Long":
        sub = df['high'].iloc[bos_idx:]; B = float(sub.max()); peak_idx = int(sub.idxmax())
    else:
        sub = df['low'].iloc[bos_idx:];  B = float(sub.min()); peak_idx = int(sub.idxmin())
    # === ATURAN LEG TERBARU (extension vs rebreak) — bersama jalur FVG ===
    res = apply_latest_leg(df, sh_h1, sl_h1, stype, swing_val, brk_idx, choch_level, peak_val, B, peak_idx, bos_idx)
    if res is None:
        return None
    swing_val, brk_idx, choch_level, peak_val, bos_idx = res
    bos_rng = (B - choch_level) if stype == "Long" else (choch_level - B)
    if bos_rng <= 0:
        return None
    # invalidasi: choch ditembus historis ATAU rebreak swing-2
    if choch_is_broken(df, bos_idx, choch_level, stype):
        return None
    if REBREAK_INVALID and peak_val is not None and \
       rebreak_invalid(df, bos_idx, peak_val, choch_level, stype, RETRACE_LOCK):
        return None
    return {'swing_val': swing_val, 'brk_idx': brk_idx, 'choch_level': choch_level,
            'peak_val': peak_val, 'B': B, 'bos_idx': bos_idx, 'peak_idx': peak_idx, 'bos_rng': bos_rng}


def find_inducement(df_tf, big_stype, band_lo, band_hi, n=1, ts_lo=None, ts_hi=None):
    """Inducement = RANTAI mini-BOS dari choch->puncak (jendela ts_lo..ts_hi).
    Cara: telusuri record-high berturut (Long). Tiap kali record ditembus record berikutnya = 1 leg/IDM.
      - TRIGGER leg = low TERENDAH di antara dua record (raw low candle, eksklusif candle record).
      - Short = cermin: record-LOW, trigger = high TERTINGGI antar record.
    IDM AKTIF = leg TERAKHIR yang trigger-nya jatuh di pita [band_lo,band_hi] (35-60% range BOS besar).
      Kalau leg terakhir terlalu dangkal (<35%, di luar pita) -> mundur ke leg sebelumnya yg di pita.
    Return {prot(trigger), prot_idx, micro_val(peak ditembus), micro_idx, n_trigger, all_triggers} atau None."""
    if df_tf is None or len(df_tf) < (2 * n + 1):
        return None
    sh_tf, sl_tf = find_last_swing_bos(df_tf, n=n)
    if not sh_tf or not sl_tf:
        return None
    ts_col = df_tf['ts']
    def _in_win(idx):
        if ts_lo is None:
            return True
        t = float(ts_col.iloc[idx])
        return ts_lo <= t <= ts_hi
    up = (big_stype == "Long")
    piv = [s for s in (sh_tf if up else sl_tf) if _in_win(s['idx'])]
    piv.sort(key=lambda s: s['idx'])
    if len(piv) < 2:
        return None
    lo_a = df_tf['low'].values; hi_a = df_tf['high'].values
    legs = []   # (trigger_val, trigger_idx, broken_peak_val, broken_peak_idx)
    rec_v, rec_i = piv[0]['val'], piv[0]['idx']
    for s in piv[1:]:
        is_break = (s['val'] > rec_v) if up else (s['val'] < rec_v)
        if not is_break:
            continue                                   # lower-high (Long) / higher-low (Short) -> bukan record, lewati
        a_seg, b_seg = rec_i + 1, s['idx'] + 1          # low/high antar record, INKLUSIF candle record-break akhir
        if b_seg > a_seg:
            if up:
                seg = lo_a[a_seg:b_seg]; off = int(seg.argmin()); tval = float(seg.min())
            else:
                seg = hi_a[a_seg:b_seg]; off = int(seg.argmax()); tval = float(seg.max())
            legs.append((tval, a_seg + off, rec_v, rec_i))
        rec_v, rec_i = s['val'], s['idx']              # record maju
    if not legs:
        return None
    inband = [lg for lg in legs if band_lo <= lg[0] <= band_hi]
    if not inband:
        return None
    best = inband[-1]                                  # leg TERAKHIR (terdekat puncak) yg di pita
    return {'prot': best[0], 'prot_idx': best[1], 'micro_val': best[2], 'micro_idx': best[3],
            'n_trigger': len(inband), 'all_triggers': [round(lg[0], 10) for lg in legs]}


def build_setup_from_bos(coin, df_h1_live, sh_h1, sl_h1, closed_h1, verbose=True, force_dir=None):
    """Deteksi BOS H1 terbaru -> FVG -> bangun setup WAIT_APPROACH.
    force_dir='Long'/'Short' => deteksi HANYA arah itu (untuk monitoring dua arah).
    Return (setup_dict, logline) atau (None, None). TIDAK menyentuh pending."""
    if not sh_h1 or not sl_h1:
        return None, None
    is_long = False; is_short = False; swing_val = None; brk_idx = None
    if force_dir in (None, "Long"):
        sv, bi = pick_bos_swing(df_h1_live, sh_h1, sl_h1, "Long")
        if sv is not None: is_long = True; swing_val = sv; brk_idx = bi
    if force_dir in (None, "Short"):
        sv, bi = pick_bos_swing(df_h1_live, sh_h1, sl_h1, "Short")
        if sv is not None: is_short = True; swing_val = sv; brk_idx = bi
    if not (is_long or is_short):
        if verbose: print(f"   {coin}: tidak ada BOS {force_dir or 'H1'}")
        return None, None
    if force_dir == "Long":
        stype = "Long"
    elif force_dir == "Short":
        stype = "Short"
    else:
        stype = "Short" if is_short else "Long"
    bos_idx, choch_level, peak_val = impulse_anchors(stype, swing_val, brk_idx, sh_h1, sl_h1, df_h1_live)
    if swing_val is None or bos_idx is None or choch_level is None:
        if verbose:
            if swing_val is None:
                print(f"   {coin}: tak ada swing 5-5 yang ter-break ({stype})")
            else:
                if stype == "Long":
                    pk_idx = int(df_h1_live['high'].iloc[brk_idx:].idxmax())
                    pk_val = float(df_h1_live['high'].iloc[brk_idx:].max())
                    cand_list = sl_h1; what = "swingLow"
                else:
                    pk_idx = int(df_h1_live['low'].iloc[brk_idx:].idxmin())
                    pk_val = float(df_h1_live['low'].iloc[brk_idx:].min())
                    cand_list = sh_h1; what = "swingHigh"
                tags = []
                for x in cand_list:
                    if x['idx'] < brk_idx:   pos = "✗sblm-break"
                    elif x['idx'] >= pk_idx: pos = "✗stlh-puncak"
                    else:                    pos = "✓DALAM"
                    tags.append(f"{x['val']:.6g}@{x['idx']}[{pos}]")
                body = ', '.join(tags) if tags else '(tak ada swing 5-5 sama sekali)'
                print(f"   {coin}: BOS {stype} tak lengkap — break={swing_val:.6g}@{brk_idx} puncak={pk_val:.6g}@{pk_idx} | {what}5-5 kandidat choch: {body}")
        return None, None
    # Puncak/lembah B + indeksnya (ekstrem langsung, tanpa nunggu)
    if stype == "Long":
        sub = df_h1_live['high'].iloc[bos_idx:]; _B = float(sub.max()); peak_idx = int(sub.idxmax())
    else:
        sub = df_h1_live['low'].iloc[bos_idx:]; _B = float(sub.min()); peak_idx = int(sub.idxmin())
    # === ATURAN LEG TERBARU (extension vs rebreak) — sama dgn jalur inducement ===
    res = apply_latest_leg(df_h1_live, sh_h1, sl_h1, stype, swing_val, brk_idx, choch_level, peak_val, _B, peak_idx, bos_idx)
    if res is None:
        if verbose: print(f"   {coin}: BOS {stype} — swing-2 ditembus & leg baru tanpa choch 5-5 (tunggu BOS baru)")
        return None, None
    swing_val, brk_idx, choch_level, peak_val, bos_idx = res
    bos_rng = (_B - choch_level) if stype == "Long" else (choch_level - _B)
    # CHoCH invalidation HISTORIS: kalau SETELAH puncak ada candle yang CLOSE menembus choch -> BOS mati
    # (walau harga sekarang sudah balik). Sebelumnya cuma cek close terakhir -> bocor.
    seg_cl = df_h1_live['close'].iloc[peak_idx:]
    choch_broken = bool((seg_cl < choch_level).any()) if stype == "Long" else bool((seg_cl > choch_level).any())
    if choch_broken:
        if verbose: print(f"   {coin}: BOS {stype} sudah CHoCH — harga pernah close lewat choch {choch_level:.6g} (mati, tunggu BOS baru)")
        return None, None
    # Invalidasi struktur: swing-2 = puncak swing 5-5; bila harga retrace >= RETRACE_LOCK
    # lalu CLOSE melewati swing-2 -> BOS invalid (struktur baru), tunggu BOS baru.
    if REBREAK_INVALID and peak_val is not None and \
       rebreak_invalid(df_h1_live, bos_idx, peak_val, choch_level, stype, RETRACE_LOCK):
        if verbose: print(f"   {coin}: BOS {stype} INVALID — retrace>={RETRACE_LOCK*100:.0f}% lalu close lewati swing-2 {peak_val:.6g} (tunggu BOS baru)")
        return None, None
    # === GATE: BOS besar WAJIB punya IDM mini-BOS di dalamnya (lebih ketat, simetris dgn jalur IDM) ===
    if REQUIRE_IDM_FOR_FVG:
        if stype == "Long":
            ib_lo, ib_hi = _B - INDUCEMENT_ZONE_HI * bos_rng, _B - INDUCEMENT_ZONE_LO * bos_rng
        else:
            ib_lo, ib_hi = _B + INDUCEMENT_ZONE_LO * bos_rng, _B + INDUCEMENT_ZONE_HI * bos_rng
        its_lo = float(df_h1_live['ts'].iloc[bos_idx])
        its_hi = float(df_h1_live['ts'].iloc[peak_idx])
        df_idm = df_h1_live if INDUCEMENT_TF == "60" else get_data(coin, "5", limit=300)
        idm_chk = None
        if df_idm is not None:
            idm_chk = find_inducement(df_idm, stype, ib_lo, ib_hi, n=INDUCEMENT_SWING, ts_lo=its_lo, ts_hi=its_hi)
        if idm_chk is None:
            if verbose:
                print(f"   {coin}: BOS {stype} TAK ada IDM mini-BOS {INDUCEMENT_SWING}-{INDUCEMENT_SWING} "
                      f"di pita {INDUCEMENT_ZONE_LO*100:.0f}-{INDUCEMENT_ZONE_HI*100:.0f}% (skip FVG limit) | "
                      f"break:{swing_val:.6g} choch:{choch_level:.6g} puncak:{_B:.6g}")
            return None, None
    zlo = deepest_retrace_lo(df_h1_live, bos_idx, choch_level, stype)
    gaps = _get_fvgs(df_h1_live, stype, bos_idx, choch_level, zone_lo=zlo)
    if not gaps:
        if verbose:
            raw = get_internal_gaps(df_h1_live, stype, bos_idx)
            Bp = _B; rng = bos_rng
            z618 = (Bp - zlo * rng) if stype == "Long" else (Bp + zlo * rng)
            tags = []
            for g in raw:
                edge = float(g.get('top', 0)) if stype == "Long" else float(g.get('bottom', 0))
                r = ((Bp - edge) if stype == "Long" else (edge - Bp)) / rng * 100 if rng > 0 else 0
                if stype == "Long" and g['bottom'] < choch_level:
                    why = "choch"
                elif stype == "Short" and g['top'] > choch_level:
                    why = "choch"
                else:
                    if stype == "Long":
                        lo = Bp - ENTRY_ZONE_HI * rng; hi = Bp - zlo * rng
                    else:
                        lo = Bp + zlo * rng; hi = Bp + ENTRY_ZONE_HI * rng
                    if not (lo <= edge <= hi):
                        why = "dilewati" if r < zlo * 100 else "zona"
                    else:
                        gs = g['top'] - g['bottom']; ocl = float(g.get('c3_open', 0))
                        if ocl > 0 and MAX_GAP_PCT > 0 and gs / ocl > MAX_GAP_PCT:
                            why = f"gap{gs / ocl * 100:.2f}%"
                        elif REQUIRE_FRESH_C1 and not c1_is_fresh(df_h1_live, g, stype):
                            why = "stale"
                        else:
                            why = "OK"
                tags.append(f"{r:.0f}%:{why}")
            print(f"   {coin}: BOS {stype} tdk ada FVG di zona | break={swing_val:.6g} "
                  f"choch={choch_level:.6g} puncak={Bp:.6g} | rawFVG={len(raw)} "
                  f"[{', '.join(tags)}] (zona>={zlo*100:.1f}%@{z618:.6g}, maxgap={MAX_GAP_PCT*100:.2f}%)")
        return None, None
    bos_ts = df_h1_live['ts'].iloc[bos_idx]
    g0 = gaps[0]
    c1_c = float(g0.get('c1_close', 0)); c1_l = float(g0.get('c1_low', 0)); c1_h = float(g0.get('c1_high', 0))
    if not (c1_c > 0 and c1_h > c1_l):
        return None, None
    gap_s = float(g0['top']) - float(g0['bottom'])
    # Trigger1 = titik AWAL masuk gap yang masih kosong (ujung C3: low[C3] utk Long / high[C3] utk
    # Short) — DIGESER ke titik terdalam yg sudah pernah diisi candle H1 antara C3->puncak kalau ada
    # partial fill (bukan C1.close lagi, dan bukan selalu ujung C3 mentah kalau sudah kesentuh sebagian).
    trig1 = gap_entry_point(df_h1_live, g0, stype, peak_idx)
    if stype == 'Long':
        entry_adj = trig1                 # trigger1 = sentuhan M5 dimulai di sini
        dist = 0.0; sl_entry = entry_adj  # akan di-override SL_FIXED_RANGE di bawah
    else:
        entry_adj = trig1                 # trigger1 = sentuhan M5 dimulai di sini
        dist = 0.0; sl_entry = entry_adj

    import datetime as _dt
    _h_s = _dt.datetime.utcfromtimestamp(df_h1_live.iloc[-1]['ts_ms'] / 1000).hour if 'ts_ms' in df_h1_live.columns else -1
    if _h_s >= 0:
        _sesi = 'Asia' if _h_s < 8 else ('London' if _h_s < 13 else 'NY')
        _allowed = SESSION_FILTER.get(coin)
        if _allowed is not None and _sesi not in _allowed:
            return None, None
    # Filter konfluensi funding: cuma ambil entry baru SELAMA funding window AND gak searah.
    if FUNDING_FILTER and in_funding_window() and not funding_favors(stype, coin):
        if verbose:
            rate = get_funding_rate(coin)
            print(f"   {coin}: BOS {stype} skip FVG limit -> funding window aktif, gak searah (rate={rate})")
        return None, None
    # SL: mode FIXED 10% range BOS (di setiap situasi), atau ikut C1 dengan cap 10% range
    if SL_FIXED_RANGE and bos_rng > 0:
        dist = SL_CAP_RANGE * bos_rng
        sl_entry = entry_adj - dist if stype == 'Long' else entry_adj + dist
    elif SL_CAP_RANGE > 0 and bos_rng > 0 and dist > SL_CAP_RANGE * bos_rng:
        dist = SL_CAP_RANGE * bos_rng
        sl_entry = entry_adj - dist if stype == 'Long' else entry_adj + dist
    # Floor Bybit: kalau dist kepecil, perbesar (jaga-jaga range BOS sangat sempit)
    min_d = entry_adj * 0.002
    if dist < min_d:
        if MIN_DIST_FLOOR:
            dist = min_d; sl_entry = entry_adj - dist if stype == 'Long' else entry_adj + dist
        else:
            return None, None
    # (guard done_setups dihapus — anti-retrade kini lewat REQUIRE_FRESH_C1)
    choch_str = f"{choch_level:.6g}" if choch_level else "—"
    _slr = (dist / bos_rng * 100) if bos_rng > 0 else 0
    logline = (f"\n📊 {coin} | BOS {stype} | break:{swing_val:.6g} puncak:{_B:.6g} CHOCH:{choch_str} | "
               f"C1close:{c1_c:.6f} Trigger1(gap):{trig1:.6f} SL:{sl_entry:.6f} "
               f"dist:{dist/entry_adj*100:.3f}% (SL {_slr:.1f}% range) Gap:{gap_s/entry_adj*100:.3f}%")
    setup = {
        'type': stype, 'phase': 'WAIT_APPROACH', 'entry': entry_adj, 'sl': sl_entry,
        'dist': dist, 'orig_ocl': trig1,   # trigger1 = ujung gap (SAMA dgn entry_adj), bukan C1 close lagi
        'fvg_list': gaps, 'bos_ts': bos_ts, 'bos_rng': bos_rng,
        'created_ts': time.time(),
        'bos_idx': bos_idx, 'swing_val': swing_val, 'choch_level': choch_level,
        'peak_val': _B, 'swing2': peak_val, 'brk_idx': brk_idx,
        # M5 engulfing monitor state (1x engulfing, filter EMA20 M5 5-candle-sebelum di check_m5_engulfing)
        'm5_c1c_touched': False,
        'm5_focus_hi': 0.0, 'm5_focus_lo': 0.0, 'm5_focus_idx': 0,
        'm5_focus_initialized': False,   # fokus scan sebenarnya baru diisi setelah gate EMA H1 selesai
        # Gate EMA20 H1 (dicek sekali saat trigger tersentuh, lihat h1_ema_gate())
        'h1_ema_resolved': False, 'h1_ema_dir': None, 'h1_ema_trigger_ts': None,
        'h1_decision_ts_close': None,
    }
    return setup, logline


def _count_slots():
    """Jumlah WAIT_FILL di semua coin & arah (untuk plafon MAX_CONCURRENT)."""
    nf = 0
    for d in pending.values():
        for s in d.values():
            if s.get('phase') == 'WAIT_FILL':
                nf += 1
    for d in struct_pending.values():
        for s in d.values():
            if s.get('phase') == 'WAIT_FILL':
                nf += 1
    return nf


def _ts_wib(ts_ms):
    """Konversi epoch ms (UTC) ke string waktu WIB, buat label log."""
    import datetime
    try:
        return (datetime.datetime.utcfromtimestamp(int(ts_ms) / 1000)
                + datetime.timedelta(hours=7)).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return str(ts_ms)


def check_m5_engulfing(coin, setup, df_m5, bos_rng, df_h1=None):
    """Monitor M5 setelah trigger H1 tersentuh (C1 close untuk FVG lama / ujung gap utk FVG /
    trigger IDM). Cari konfirmasi engulfing untuk market order.
    Return: dict {'entry': float, 'sl': float, 'side': str} jika engulfing terkonfirmasi,
            {'cancelled': True} jika dibatalkan oleh cross-check IDM vs FVG,
            None jika belum ada konfirmasi / masih menunggu fase berikutnya.

    STATE MACHINE 3 FASE (eksplisit, masing-masing di-retry tiap siklus sampai berhasil —
    TIDAK ADA fase yang mengasumsikan fase berikutnya langsung berhasil di siklus yang sama):

      FASE 1 — WAIT_TRIGGER (state: 'm5_c1c_touched'):
        Cari candle M5 pertama yang menyentuh trigger H1 (c1c/orig_ocl). Begitu ketemu, LANGSUNG
        lanjut ke FASE 2 di siklus yang sama (tidak perlu tunggu siklus berikutnya) — tapi TIDAK
        mengisi m5_focus_hi/lo/idx sama sekali (beda dari desain lama yang langsung menjadikan
        candle trigger-touch ini sebagai fokus awal — itu sumber bug lama: fokus scan bisa mulai
        dari SEBELUM H1 close yang jadi acuan gate EMA).

      FASE 2 — WAIT_GATE (state: 'h1_ema_resolved', 'h1_decision_ts_close'):
        Jalankan h1_ema_gate() sekali saja begitu trigger tersentuh. Bisa butuh beberapa candle H1
        close sebelum lolos (return None di siklus2 sebelumnya, retry otomatis tiap siklus). Begitu
        lolos: simpan arah final (h1_ema_dir) DAN 'h1_decision_ts_close' (waktu CLOSE candle H1 yang
        jadi acuan keputusan gate) — dipakai FASE 3. Cross-check IDM vs FVG juga dicek di sini
        (lihat _idm_trigger_touched_for_bos).

      FASE 3 — WAIT_INIT_FOCUS (state: 'm5_focus_initialized'):
        SETELAH gate lolos, cari candle M5 PERTAMA yang CLOSE setelah 'h1_decision_ts_close' —
        candle SEBELUM itu yang dijadikan m5_focus_idx awal (supaya _scan_engulf, yang mulai dari
        focus_idx+1, mulai PERSIS dari candle M5 pertama yang close setelah H1 close tsb). Kalau
        df_m5 yang tersedia BELUM mencapai candle itu (window fetch belum sampai), fase ini
        me-return None dan DICOBA LAGI di siklus berikutnya — TIDAK PERNAH jatuh ke fokus manapun
        sebelum fase ini benar2 berhasil. Ini yang membedakan dari desain lama: sebelumnya reset
        fokus hanya dicoba SEKALI (barengan dgn fase gate) — kalau df_m5 kebetulan belum cukup
        panjang di siklus itu, reset gagal PERMANEN & fokus lama (dari FASE 1, sebelum H1 close)
        yang kepakai selamanya. Fase 3 ini independen & retry-able, jadi tidak bisa gagal permanen.

      Baru setelah FASE 3 selesai (m5_focus_initialized=True), scan engulfing (_scan_engulf) mulai
      jalan dari fokus yang sudah pasti berada tepat setelah H1 close yang lolos gate EMA.

    FILTER EMA20 M5 (FVG maupun IDM), TIGA syarat, semuanya harus lolos:
      1. Salah satu dari 5 candle SEBELUM candle engulfing sudah menyentuh EMA20 M5 (EMA20 candle
         itu harus benar-benar masuk range candle: low<=EMA20<=high — bukan cuma "high>=ema"/
         "low<=ema" saja, supaya harga yang sudah lama di satu sisi EMA tidak dihitung tersentuh).
      2. Candle ENGULFING itu sendiri harus close di sisi EMA20 yang benar: Long → close di ATAS
         EMA20 candle itu; Short → close di BAWAH EMA20 candle itu.
      3. Candle SEBELUM engulfing (i-1): body-nya (open-close, bukan wick) harus ada bagian yang
         keluar searah EMA — Long: max(open,close) candle i-1 > EMA20 candle i-1 (body pernah di
         atas EMA); Short: min(open,close) candle i-1 < EMA20 candle i-1 (body pernah di bawah EMA).
    Kalau salah satu dari 3 syarat gagal, engulfing itu DITOLAK (bukan entry) — TAPI candle yang
    ditolak itu tetap jadi fokus baru (persis seperti aturan dasar pindah fokus biasa — close yang
    melewati fokus lama selalu memindahkan fokus, terlepas apakah akhirnya lolos jadi entry atau
    tidak), dan monitor lanjut cari engulfing BERIKUTNYA dari fokus baru itu.
    """
    if df_m5 is None or len(df_m5) < 2:
        return None
    stype   = setup['type']
    c1c     = float(setup.get('orig_ocl', 0))   # trigger H1 (ujung gap utk FVG / trigger IDM)
    if c1c <= 0:
        return None

    # EMA20 dihitung di atas SELURUH data yang tersedia (sebelum difilter created_ts) supaya ada
    # histori pemanasan yang cukup untuk EMA, bukan cuma dari titik setup dibuat.
    df_m5 = df_m5.copy()
    df_m5['ema20'] = df_m5['close'].ewm(span=20, adjust=False).mean()

    # ── Filter: hanya proses candle M5 yang terbentuk SETELAH setup dibuat ──
    # Mencegah bot mereplay engulfing historis saat redeploy.
    # PENTING: created_ts adalah waktu wall-clock (time.time()), yg biasanya jatuh DI TENGAH
    # candle M5 yang sedang berjalan saat itu (bukan pas candle baru mulai). Candle yg sedang
    # berjalan itu 'ts'-nya (waktu OPEN) otomatis lebih awal dari created_ts -> kalau filter
    # persis "ts >= created_ts", candle itu ke-exclude padahal high/low finalnya baru terbentuk
    # SETELAH setup dibuat (wick besarnya bisa saja terjadi setelah creation, sebelum candle
    # close). Makanya dikurangi 1 interval candle (5 menit) supaya candle yg sedang berjalan
    # persis saat creation tetap ikut kescan.
    created_ts_ms = setup.get('created_ts', 0) * 1000 - 300000   # detik → ms, mundur 1 candle M5
    if created_ts_ms > 0 and 'ts' in df_m5.columns:
        df_m5 = df_m5[df_m5['ts'] >= created_ts_ms].reset_index(drop=True)
        if len(df_m5) < 2:
            return None   # belum ada candle baru sejak setup dibuat

    # ── Init: cari candle M5 PALING AWAL yang menyentuh trigger (scan maju) ──
    # Scan semua candle kecuali yg berjalan (iloc[-1])
    n = len(df_m5)
    closed_end = n - 1   # index eksklusif: loop sampai < closed_end (tidak termasuk candle berjalan)
    if not setup.get('m5_c1c_touched'):
        for i in range(closed_end):
            lo = float(df_m5['low'].iloc[i])
            hi = float(df_m5['high'].iloc[i])
            touched = (stype == 'Long' and lo <= c1c) or (stype == 'Short' and hi >= c1c)
            if touched:
                setup['m5_c1c_touched'] = True
                if setup.get('h1_ema_trigger_ts') is None and 'ts' in df_m5.columns:
                    setup['h1_ema_trigger_ts'] = float(df_m5['ts'].iloc[i])
                print(f"   {coin} {stype}: trigger H1 ({c1c:.6g}) tersentuh M5 idx={i} "
                      f"hi={hi:.6g} lo={lo:.6g} — cek gate EMA20 H1 dulu")
                break
        if not setup.get('m5_c1c_touched'):
            return None   # belum tersentuh
        # CATATAN: 'm5_focus_hi/lo/idx' SENGAJA TIDAK di-set di sini lagi (beda dari desain lama).
        # Fokus awal untuk scan engulfing HANYA boleh diinisialisasi SETELAH gate EMA H1 selesai
        # DAN df_m5 sudah mencapai candle setelah H1 close (lihat fase WAIT_INIT_FOCUS di bawah) —
        # supaya tidak ada jalur manapun yang bisa mulai scan dari fokus "sebelum H1 close".

    # ── GATE EMA20 H1: sekali saja saat trigger baru tersentuh, sebelum mulai scan M5 ──
    if not setup.get('h1_ema_resolved'):
        trig_ts = setup.get('h1_ema_trigger_ts')
        if trig_ts is None:
            trig_ts = float(df_m5['ts'].iloc[0]) if 'ts' in df_m5.columns else 0
            setup['h1_ema_trigger_ts'] = trig_ts
        resolved, final_dir, info, decision_ts_close = h1_ema_gate(df_h1, trig_ts, stype)
        if not resolved:
            print(f"   {coin} {stype}: menunggu gate EMA20 H1 — {info}")
            return None
        setup['h1_ema_resolved'] = True
        setup['h1_ema_dir'] = final_dir
        setup['h1_decision_ts_close'] = decision_ts_close   # dipakai fase WAIT_INIT_FOCUS di bawah
        _mode = "IDM/FVG (searah)" if final_dir == stype else "EMA (arah DIBALIK)"
        log_entry(f"   {coin}: gate EMA20 H1 selesai -> arah entry = {final_dir} [mode {_mode}] | {info}")

        # ── Cross-check (FVG only): kalau gate EMA membalik arah FVG ini jadi KEBALIKAN BOS besar
        # (sama dengan arah yang diambil IDM), dan IDM untuk BOS besar SAMA PERSIS sudah tersentuh
        # trigger-nya, batalkan FVG ini — arah itu sudah "milik" IDM, cegah risk dobel.
        if not setup.get('is_idm') and final_dir != stype:
            _sv = setup.get('swing_val'); _cl = setup.get('choch_level')
            if _idm_trigger_touched_for_bos(coin, _sv, _cl):
                log_entry(f"🚫 {coin}: FVG {stype} dibatalkan — gate EMA membalik arah jadi {final_dir} "
                          f"(kebalikan BOS), tapi IDM untuk BOS besar ini sudah tersentuh duluan")
                return {'cancelled': True}
    stype = setup['h1_ema_dir']   # dari sini pakai arah HASIL gate EMA H1 (bisa sama, bisa dibalik)

    # ── FASE WAIT_INIT_FOCUS: inisialisasi fokus M5 pertama SETELAH H1 close yang lolos gate ──
    # Terpisah dari blok gate di atas (yang cuma jalan SEKALI) — fase ini DICOBA ULANG TIAP SIKLUS
    # sampai berhasil, supaya tidak gagal permanen kalau di siklus yang sama dengan gate resolve,
    # df_m5 yang ter-fetch belum sampai ke candle setelah H1 close (window fetch terbatas dsb).
    # Sebelum fase ini berhasil, TIDAK ADA scan engulfing yang boleh jalan sama sekali.
    if not setup.get('m5_focus_initialized'):
        decision_ts_close = setup.get('h1_decision_ts_close')
        if decision_ts_close is None or 'ts' not in df_m5.columns:
            return None   # seharusnya tidak terjadi (gate sudah resolved), jaga-jaga saja
        reset_idx = None
        for k in range(closed_end):
            # candle M5 k dianggap "close" pada ts+5menit; cari yang close-nya > decision_ts_close
            if float(df_m5['ts'].iloc[k]) + 300_000 > decision_ts_close:
                reset_idx = k
                break
        if reset_idx is None:
            # df_m5 belum mencapai candle M5 yang close setelah H1 close — tunggu siklus berikutnya,
            # JANGAN mulai scan dari fokus manapun dulu.
            print(f"   {coin} {stype}: menunggu candle M5 setelah H1 close ({_ts_wib(decision_ts_close)}) "
                  f"untuk inisialisasi fokus pertama...")
            return None
        # Fokus PERTAMA = candle M5 di reset_idx itu sendiri (candle yang CLOSE persis setelah H1
        # close) — BUKAN candle sebelumnya. _scan_engulf mulai dari start_idx+1, jadi dengan fokus di
        # reset_idx, candle PERTAMA yang benar2 discan sebagai calon engulfing adalah reset_idx+1
        # (candle SETELAH candle-yang-baru-close-setelah-H1-close-itu) — persis sesuai maksud: candle
        # M5 yang close setelah H1 close jadi fokus/acuan, candle setelahnya yang mulai dicari engulfing.
        setup['m5_focus_idx'] = reset_idx
        setup['m5_focus_hi']  = float(df_m5['high'].iloc[reset_idx])
        setup['m5_focus_lo']  = float(df_m5['low'].iloc[reset_idx])
        setup['m5_focus_initialized'] = True
        log_entry(f"   {coin} {stype}: fokus M5 pertama diinisialisasi setelah H1 close -> "
                  f"M5 {_ts_wib(df_m5['ts'].iloc[reset_idx]) if 'ts' in df_m5.columns else reset_idx}")

    def _ema_touched(i):
        """5 candle SEBELUM candle engulfing (i-5..i-1): minimal 1 harus BENAR-BENAR menyentuh
        EMA20 — nilai EMA20 candle itu harus masuk ke dalam range candle (low<=ema<=high).
        Ini BUKAN sekadar 'high>=ema' atau 'low<=ema' saja, karena kalau harga sudah lama berada
        jauh di satu sisi EMA (mis. rally panjang di atas EMA yang lamban), high>=ema akan SELALU
        true meski tidak pernah ada sentuhan beneran ke garis EMA-nya. Dicek dengan candle range
        yang memuat nilai EMA supaya valid untuk kedua arah (Long maupun Short)."""
        if 'ema20' not in df_m5.columns:
            return True
        for j in range(max(0, i - 5), i):
            ema_j = float(df_m5['ema20'].iloc[j])
            lo_j  = float(df_m5['low'].iloc[j])
            hi_j  = float(df_m5['high'].iloc[j])
            if lo_j <= ema_j <= hi_j:
                return True
        return False

    # ── Scan generik candle fokus->engulfing (4 aturan dasar: fokus/pindah fokus/range base/engulfing) ──
    def _scan_engulf(start_idx, f_hi, f_lo):
        range_base = False
        for i in range(start_idx + 1, closed_end):
            lo = float(df_m5['low'].iloc[i])
            hi = float(df_m5['high'].iloc[i])
            cl = float(df_m5['close'].iloc[i])
            op = float(df_m5['open'].iloc[i])

            # Masuk range_base jika candle masih dalam range fokus (tidak melewati hi atau lo)
            if not range_base and hi < f_hi and lo > f_lo:
                range_base = True
                log_entry(f"   {coin} {stype}: range base mode aktif @ {_ts_wib(df_m5['ts'].iloc[i]) if 'ts' in df_m5.columns else i} "
                          f"(candle base hi={f_hi:.6g} lo={f_lo:.6g})")

            # ── Cek engulfing ──
            # Engulfing valid: close melewati hi/lo fokus DAN tidak sweep sisi berlawanan sekaligus
            # DAN bukan dalam range_base mode (di range_base, close melewati fokus = pindah fokus,
            # bukan langsung engulfing — hanya sweep searah TANPA close yang menahan fokus).
            long_sweep_opp  = (lo <= f_lo)
            short_sweep_opp = (hi >= f_hi)

            # Entry = high/low candle SEBELUM engulfing (prev candle) — TIDAK BERUBAH.
            # SL = FIXED proporsional dari entry ± SL_ENGULF_PCT% range BOS besar (bukan lagi dari
            # low/high candle sebelum engulfing) — supaya jarak entry-SL selalu proporsional ke
            # ukuran BOS, tidak random besar/kecil tergantung bentuk candle di titik itu.
            # Long: entry = high prev candle, SL = entry - buffer.
            # Short: entry = low prev candle, SL = entry + buffer.
            prev_c_hi = float(df_m5['high'].iloc[i-1])
            prev_c_lo = float(df_m5['low'].iloc[i-1])

            if stype == 'Long' and cl > f_hi and not long_sweep_opp and not range_base:
                ema_i    = float(df_m5['ema20'].iloc[i])
                ema_prev = float(df_m5['ema20'].iloc[i-1])
                rejected = False
                if not _ema_touched(i):
                    log_entry(f"   {coin} {stype}: engulfing @ {_ts_wib(df_m5['ts'].iloc[i]) if 'ts' in df_m5.columns else i} "
                              f"DITOLAK (5 candle sebelum tak sentuh EMA20)")
                    rejected = True
                elif not (cl > ema_i):
                    log_entry(f"   {coin} {stype}: engulfing @ {_ts_wib(df_m5['ts'].iloc[i]) if 'ts' in df_m5.columns else i} "
                              f"DITOLAK (close={cl:.6g} tidak di atas EMA20={ema_i:.6g})")
                    rejected = True
                elif not (max(float(df_m5['open'].iloc[i-1]), float(df_m5['close'].iloc[i-1])) > ema_prev):
                    log_entry(f"   {coin} {stype}: engulfing @ {_ts_wib(df_m5['ts'].iloc[i]) if 'ts' in df_m5.columns else i} "
                              f"DITOLAK (body candle sebelum engulfing tidak pernah di atas EMA20={ema_prev:.6g})")
                    rejected = True
                if rejected:
                    # Candle yang ditolak TETAP jadi fokus baru (aturan dasar biasa: close melewati
                    # fokus lama = pindah fokus), lalu lanjut cari engulfing berikutnya dari sini.
                    f_hi = hi; f_lo = lo
                    setup['m5_focus_hi'] = hi; setup['m5_focus_lo'] = lo; setup['m5_focus_idx'] = i
                    range_base = False
                    continue
                entry_p  = prev_c_hi
                sl_buf   = SL_ENGULF_PCT * bos_rng
                sl_raw   = entry_p - sl_buf
                sl_price = sl_raw
                print(f"   {coin} {stype}: ENGULFING M5 idx={i} close={cl:.6g} > focus_hi={f_hi:.6g} "
                      f"→ entry={entry_p:.6g} (high candle sebelum engulfing) SL={sl_price:.6g} "
                      f"(fixed entry-{SL_ENGULF_PCT*100:.0f}% range BOS)")
                setup['m5_focus_idx'] = i
                return {'entry': entry_p, 'sl': sl_price, 'side': 'Buy',
                        'engulf_idx': i, 'focus_hi': f_hi, 'focus_lo': f_lo,
                        'engulf_ohlc': {'open': op, 'high': hi, 'low': lo, 'close': cl},
                        'prev_candle_ohlc': {'high': prev_c_hi, 'low': prev_c_lo},
                        'sl_raw': sl_raw, 'sl_buffer': sl_buf}
            if stype == 'Short' and cl < f_lo and not short_sweep_opp and not range_base:
                ema_i    = float(df_m5['ema20'].iloc[i])
                ema_prev = float(df_m5['ema20'].iloc[i-1])
                rejected = False
                if not _ema_touched(i):
                    log_entry(f"   {coin} {stype}: engulfing @ {_ts_wib(df_m5['ts'].iloc[i]) if 'ts' in df_m5.columns else i} "
                              f"DITOLAK (5 candle sebelum tak sentuh EMA20)")
                    rejected = True
                elif not (cl < ema_i):
                    log_entry(f"   {coin} {stype}: engulfing @ {_ts_wib(df_m5['ts'].iloc[i]) if 'ts' in df_m5.columns else i} "
                              f"DITOLAK (close={cl:.6g} tidak di bawah EMA20={ema_i:.6g})")
                    rejected = True
                elif not (min(float(df_m5['open'].iloc[i-1]), float(df_m5['close'].iloc[i-1])) < ema_prev):
                    log_entry(f"   {coin} {stype}: engulfing @ {_ts_wib(df_m5['ts'].iloc[i]) if 'ts' in df_m5.columns else i} "
                              f"DITOLAK (body candle sebelum engulfing tidak pernah di bawah EMA20={ema_prev:.6g})")
                    rejected = True
                if rejected:
                    # Sama seperti Long: candle yang ditolak TETAP jadi fokus baru, lanjut cari
                    # engulfing berikutnya dari sini.
                    f_hi = hi; f_lo = lo
                    setup['m5_focus_hi'] = hi; setup['m5_focus_lo'] = lo; setup['m5_focus_idx'] = i
                    range_base = False
                    continue
                entry_p  = prev_c_lo
                sl_buf   = SL_ENGULF_PCT * bos_rng
                sl_raw   = entry_p + sl_buf
                sl_price = sl_raw
                print(f"   {coin} {stype}: ENGULFING M5 idx={i} close={cl:.6g} < focus_lo={f_lo:.6g} "
                      f"→ entry={entry_p:.6g} (low candle sebelum engulfing) SL={sl_price:.6g} "
                      f"(fixed entry+{SL_ENGULF_PCT*100:.0f}% range BOS)")
                setup['m5_focus_idx'] = i
                return {'entry': entry_p, 'sl': sl_price, 'side': 'Sell',
                        'engulf_idx': i, 'focus_hi': f_hi, 'focus_lo': f_lo,
                        'engulf_ohlc': {'open': op, 'high': hi, 'low': lo, 'close': cl},
                        'prev_candle_ohlc': {'high': prev_c_hi, 'low': prev_c_lo},
                        'sl_raw': sl_raw, 'sl_buffer': sl_buf}

            # ── Update fokus ──
            if range_base:
                # Range base mode: engulfing TIDAK pernah valid langsung di mode ini (sudah dicegah
                # oleh "not range_base" di atas). Fokus TETAP (tidak pindah) hanya jika:
                #   (a) candle sepenuhnya di dalam range fokus (tidak menyentuh hi/lo sama sekali), atau
                #   (b) candle sweep SEARAH engulfing TANPA close melewati fokus.
                #   Long (searah=atas): hi menyentuh/lewat focus_hi TAPI close <= focus_hi → tahan fokus
                #   Short (searah=bawah): lo menyentuh/lewat focus_lo TAPI close >= focus_lo → tahan fokus
                # Semua kondisi LAIN (close lolos searah, atau gerak ke arah berlawanan) → pindah fokus.
                fully_inside = (hi < f_hi) and (lo > f_lo)
                if stype == 'Long':
                    sweep_searah_only = (hi >= f_hi) and (cl <= f_hi) and (lo > f_lo)
                else:
                    sweep_searah_only = (lo <= f_lo) and (cl >= f_lo) and (hi < f_hi)
                should_shift = not (fully_inside or sweep_searah_only)
            else:
                # Mode normal: sentuhan ke arah mana pun = pindah fokus
                wick_out = (hi >= f_hi) or (lo <= f_lo)
                close_break_dn = (stype == 'Long' and cl < f_lo)
                close_break_up = (stype == 'Short' and cl > f_hi)
                should_shift = wick_out or close_break_dn or close_break_up

            if should_shift:
                f_hi = hi; f_lo = lo
                setup['m5_focus_hi']  = hi; setup['m5_focus_lo']  = lo
                setup['m5_focus_idx'] = i
                range_base = False   # reset ke mode normal setelah pindah fokus
                log_entry(f"   {coin} {stype}: fokus pindah ke M5 {_ts_wib(df_m5['ts'].iloc[i]) if 'ts' in df_m5.columns else i} "
                          f"hi={hi:.6g} lo={lo:.6g} close={cl:.6g}")

        return None   # belum ada engulfing di data yang tersedia

    focus_idx = setup.get('m5_focus_idx', 0)
    focus_hi  = float(setup['m5_focus_hi'])
    focus_lo  = float(setup['m5_focus_lo'])
    return _scan_engulf(focus_idx, focus_hi, focus_lo)


def update_h1_bias(coin, df_h1):
    """Tentukan/perbarui bias arah entry M5 (mode eksperimental) dari cross EMA3/EMA20 di H1.
      - EMA3 cross EMA20 dari BAWAH ke ATAS  -> bias = 'Long'  (mode eksperimental hanya cari Long di M5)
      - EMA3 cross EMA20 dari ATAS ke BAWAH  -> bias = 'Short' (mode eksperimental hanya cari Short di M5)
    Bias TETAP di arah terakhir sampai ada cross H1 berlawanan berikutnya (bukan sinyal sekali pakai).

    PENTING (anti-bug redeploy) — sama persis pola timestamp yang dipakai check_experimental_engulf:
    state disimpan sbg TIMESTAMP candle H1 terakhir yang sudah diproses, BUKAN index array (karena
    window fetch H1 bergeser tiap siklus, index absolut candle yang sama bisa berubah antar siklus).
    Saat bot baru start / redeploy, TIDAK langsung scan mundur ke histori H1 lawas mencari cross —
    kalau begitu, cross yang sudah terjadi berjam-jam/berhari lalu bisa "ditemukan lagi" dan langsung
    dianggap valid utk entry, padahal sudah basi. Sebagai gantinya, inisialisasi hanya mencatat
    timestamp candle H1 closed TERAKHIR saat itu sbg titik awal, dan bias awal = None (netral, belum
    ada arah). Hanya cross yang terjadi PERSIS pada candle H1 baru/live setelah titik awal itu yang
    akan dipakai utk menetapkan/mengubah bias — cross lama sebelum bot jalan diabaikan sepenuhnya.
    """
    global h1_bias_state
    if df_h1 is None or len(df_h1) < 25 or 'ts' not in df_h1.columns:
        return
    df = df_h1.copy().reset_index(drop=True)
    df['ema3']  = df['close'].ewm(span=3,  adjust=False).mean()
    df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
    n = len(df)
    closed_end = n - 1   # exclude candle H1 yang masih berjalan (belum closed)

    def _idx_of_ts(ts_val):
        matches = df.index[df['ts'] == ts_val]
        return int(matches[0]) if len(matches) else None

    st = h1_bias_state.get(coin)
    if st is None:
        # Inisialisasi pertama kali (termasuk setelah redeploy) — mulai dari candle H1 closed
        # TERAKHIR. Bias awal LANGSUNG diambil dari posisi EMA3 vs EMA20 SAAT INI (bukan nunggu
        # cross baru dulu) — ini BUKAN "menganggap cross lama sbg trigger baru" (yang dihindari),
        # melainkan cuma membaca kondisi tren H1 yang sedang berlangsung sekarang: kalau EMA3
        # sedang di ATAS EMA20 -> bias Long (tetap Long selama EMA3 belum cross balik ke bawah);
        # kalau di BAWAH -> bias Short. Cross histori itu sendiri TIDAK di-scan/di-log sama sekali,
        # hanya cross yang terjadi live setelah titik ini yang akan mengubah bias selanjutnya.
        last_idx = closed_end - 1
        if last_idx < 1:
            return   # data H1 belum cukup, tunggu siklus berikutnya
        e3_0, e20_0 = float(df['ema3'].iloc[last_idx]), float(df['ema20'].iloc[last_idx])
        init_bias = 'Long' if e3_0 > e20_0 else ('Short' if e3_0 < e20_0 else None)
        st = {'last_ts': float(df['ts'].iloc[last_idx]), 'bias': init_bias}
        h1_bias_state[coin] = st
        log_entry(f"👁️  H1 BIAS {coin}: mulai monitoring EMA3/EMA20 H1 dari candle live terakhir "
                  f"({_ts_wib(df['ts'].iloc[last_idx])}) — bias awal = {init_bias or 'netral (EMA3≈EMA20)'} "
                  f"(EMA3={e3_0:.6g}, EMA20={e20_0:.6g})")
        return

    last_i = _idx_of_ts(st['last_ts'])
    if last_i is None:
        # Candle acuan lama sudah keluar window fetch (jarang, window 100 candle H1 ≈ 4 hari) —
        # reset ke candle live terakhir, bias TETAP dipertahankan (bukan direset ke None) karena
        # ini cuma pergeseran window, bukan redeploy.
        last_idx = closed_end - 1
        if last_idx < 1:
            return
        st['last_ts'] = float(df['ts'].iloc[last_idx])
        log_entry(f"⚠️ H1 BIAS {coin}: candle acuan lama di luar window data — di-reset ke candle live terakhir")
        return

    for i in range(last_i + 1, closed_end):
        e3, e20   = float(df['ema3'].iloc[i]),   float(df['ema20'].iloc[i])
        e3p, e20p = float(df['ema3'].iloc[i-1]), float(df['ema20'].iloc[i-1])
        if e3p <= e20p and e3 > e20:
            st['bias'] = 'Long'
            log_entry(f"🔀 H1 BIAS {coin}: EMA3 cross EMA20 ke ATAS @ {_ts_wib(df['ts'].iloc[i])} "
                      f"(EMA3={e3:.6g} > EMA20={e20:.6g}) → bias entry M5 = LONG")
        elif e3p >= e20p and e3 < e20:
            st['bias'] = 'Short'
            log_entry(f"🔀 H1 BIAS {coin}: EMA3 cross EMA20 ke BAWAH @ {_ts_wib(df['ts'].iloc[i])} "
                      f"(EMA3={e3:.6g} < EMA20={e20:.6g}) → bias entry M5 = SHORT")

    if closed_end - 1 >= 0:
        st['last_ts'] = float(df['ts'].iloc[closed_end - 1])


def _experimental_execute_entry(coin, stype, df_m5, entry_i, note=""):
    """Eksekusi MARKET entry mode eksperimental — dipakai baik saat cross EMA3/EMA20 ditemukan di
    window SEBELUM engulfing maupun SESUDAH engulfing (logikanya identik, cuma titik entry beda).
    entry_i = index candle df_m5 tempat entry terjadi (harga = close candle itu).
    SL = entry AKTUAL ± EXPERIMENTAL_SL_PCT% dari harga entry sebenarnya (bukan dari ujung candle).
    """
    curr_price = float(df_m5['close'].iloc[entry_i])
    side = 'Buy' if stype == 'Long' else 'Sell'
    sl_buf = curr_price * (EXPERIMENTAL_SL_PCT / 100.0)
    sl_p = (curr_price - sl_buf) if stype == 'Long' else (curr_price + sl_buf)
    print(f"✅ EXPERIMENTAL {coin} {stype}: EMA3 cross EMA20 {note} → MARKET ENTRY")
    log_entry(f"════ EXPERIMENTAL MARKET {stype} {coin} — EMA3 cross EMA20 {note} ════\n"
              f"  entry~{curr_price:.6g} SL={sl_p:.6g} ({EXPERIMENTAL_SL_PCT:.2f}% dari entry)")
    oid, qty = place_market_entry(coin, side, curr_price, sl_p, 0)
    if oid:
        dist = abs(curr_price - sl_p)
        active_positions[_akey(coin, stype)] = {
            'coin': coin, 'side': side, 'entry': curr_price, 'sl': sl_p,
            'dist': dist, 'trail_dist': TRAIL_STOP * dist,
            'trail_engaged': False, 'trail_set': False,
            'last_price': curr_price, 'entry_time': time.time(),
            'peak': curr_price, 'peak_time': time.time(),
            'bos_type': stype, 'rev_count': 0,
            'orig_ocl': curr_price, 'kind': 'experimental',
        }
    return oid


def check_experimental_engulf(coin, df_m5):
    """MODE EKSPERIMENTAL — independen sepenuhnya dari jalur IDM/FVG (tidak ada BOS H1, tidak ada
    gate EMA H1, tidak ada cross-check). Begitu bot start, tiap coin langsung monitoring M5 dari
    fokus paling awal yang tersedia, cari engulfing (Long & Short dipantau bersamaan).

    PENTING — SEMUA state lintas-siklus disimpan sebagai TIMESTAMP (ts, ms), BUKAN index array.
    df_m5 di-fetch ULANG tiap siklus (window 300 candle terbaru yang BERGESER seiring waktu) — index
    absolut candle yang sama akan BERBEDA antar siklus (candle yg tadinya index 250 bisa jadi index
    249 di siklus berikutnya begitu window geser). Kalau state disimpan sbg index array, di siklus
    berikutnya index itu akan merujuk ke candle yang SALAH SAMA SEKALI — bug ini pernah terjadi
    (fokus tidak pernah pindah, log kosong total setelah init). Sekarang tiap kali dipanggil, ts yang
    tersimpan di-map ULANG ke index lokal df_m5 versi saat ini di awal fungsi.

    ATURAN (per arah):
      1. Fokus & pindah fokus & range-base: PERSIS sama seperti _scan_engulf() di check_m5_engulfing
         (candle fokus geser tiap kali ada candle close yang melewati hi/lo fokus; kalau harga masuk
         penuh ke dalam range fokus dulu, masuk range_base mode).
      2. Begitu ada candle yang close melewati fokus (calon engulfing, candle index i):
         a. Candle SEBELUM engulfing (i-1) wick-nya WAJIB menyentuh EMA20 M5 (low<=ema20<=high) —
            HANYA candle i-1 yang dicek (beda dari filter M5 biasa yang cek 5 candle sebelum).
         b. Kalau (a) lolos, cek cross EMA3/EMA20 SEARAH engulfing (Long: EMA3 dari <=EMA20 jadi
            >EMA20; Short: EMA3 dari >=EMA20 jadi <EMA20) di window EXPERIMENTAL_EMA_CROSS_BARS
            candle SEBELUM engulfing (i-N..i-1). Kalau KETEMU → cross itu dianggap valid, entry
            MARKET langsung terjadi SAAT INI (candle engulfing i, harga = close candle i) — tidak
            perlu nunggu candle berikutnya lagi karena EMA sudah cross duluan sebelum engulfing
            terbentuk.
         c. Kalau TIDAK ketemu di (b), mulai "menunggu cross EMA3/EMA20" ke DEPAN selama
            EXPERIMENTAL_EMA_CROSS_BARS candle (termasuk candle engulfing itu sendiri sebagai
            candle ke-1, jadi candle ke-1 s/d ke-(EXPERIMENTAL_EMA_CROSS_BARS)). Begitu cross
            searah terjadi di salah satu candle dalam window itu → entry MARKET saat itu juga.
         d. Kalau sampai candle ke-(EXPERIMENTAL_EMA_CROSS_BARS) SESUDAH masih belum ada cross →
            dibatalkan, fokus pindah ke candle terakhir yang sudah diperiksa, lanjut cari
            engulfing berikutnya.
         Jadi total window pencarian cross = i-N (sebelum) s/d i+N-1 (sesudah), N=EXPERIMENTAL_EMA_CROSS_BARS.
      3. Entry = MARKET (bukan limit). SL = harga entry AKTUAL (saat/di titik cross ditemukan) ±
         EXPERIMENTAL_SL_PCT% dari entry — dihitung ulang di titik entry sebenarnya, BUKAN dari
         ujung candle sebelum engulfing (itu bisa lebar/sempit random tergantung besar candle;
         sekarang selalu proporsional & konsisten terhadap harga, sesuai permintaan user).

    State disimpan di experimental_pending[coin] (SEMUA berbasis TIMESTAMP, bukan index):
      'm5_focus_ts'/'m5_focus_hi'/'m5_focus_lo' : fokus aktif SAAT INI.
      'range_base' : bool — status range-base mode aktif tidaknya.
      'ema_wait'  : dict atau None — kalau sedang menunggu cross EMA3/EMA20 setelah lolos syarat (2a):
          {'stype': 'Long'/'Short', 'engulf_ts': float, 'prev_hi': float, 'prev_lo': float,
           'entry_p': float, 'sl_p': float, 'last_checked_ts': float}
    """
    global experimental_pending
    if df_m5 is None or len(df_m5) < 10 or 'ts' not in df_m5.columns:
        return
    df_m5 = df_m5.copy().reset_index(drop=True)
    df_m5['ema3']  = df_m5['close'].ewm(span=3,  adjust=False).mean()
    df_m5['ema20'] = df_m5['close'].ewm(span=20, adjust=False).mean()
    n = len(df_m5)
    closed_end = n - 1   # exclude candle yang masih berjalan

    def _idx_of_ts(ts_val):
        """Cari index lokal di df_m5 SAAT INI untuk timestamp yang tersimpan. None kalau tidak ada
        (misal window fetch sudah bergeser jauh sehingga candle itu tidak lagi ter-cover)."""
        matches = df_m5.index[df_m5['ts'] == ts_val]
        return int(matches[0]) if len(matches) else None

    st = experimental_pending.get(coin)
    if st is None:
        # Inisialisasi pertama kali (termasuk setelah redeploy — state ini murni in-memory, TIDAK
        # persist): fokus = candle M5 TERAKHIR yang sudah closed SAAT INI, BUKAN candle paling awal
        # di window fetch (candle 300 candle lalu). Kalau pakai candle awal, begitu redeploy bot akan
        # "menemukan" lagi engulfing2 LAMA dari histori yang sebenarnya sudah basi.
        last_idx = closed_end - 1
        if last_idx < 0:
            return   # data terlalu pendek, tunggu siklus berikutnya
        st = {
            'm5_focus_ts': float(df_m5['ts'].iloc[last_idx]),
            'm5_focus_hi': float(df_m5['high'].iloc[last_idx]),
            'm5_focus_lo': float(df_m5['low'].iloc[last_idx]),
            'range_base': False,
            'ema_wait': None,
        }
        experimental_pending[coin] = st
        print(f"👁️  EXPERIMENTAL {coin}: mulai monitoring M5 dari candle live terakhir "
              f"({_ts_wib(df_m5['ts'].iloc[last_idx])}, "
              f"hi={st['m5_focus_hi']:.6g} lo={st['m5_focus_lo']:.6g}) — engulfing lama diabaikan")
        return   # baru inisialisasi — scan candle berikutnya mulai siklus berikutnya (data belum ada)

    # ── Kalau sedang menunggu cross EMA3/EMA20 (fase 2b/2c) ──
    ew = st.get('ema_wait')
    if ew is not None:
        start_i = _idx_of_ts(ew['engulf_ts'])
        if start_i is None:
            # Candle engulfing acuan sudah keluar dari window fetch (jarang terjadi, window 300
            # candle = ~25 jam data M5) — batalkan wait, mulai scan fokus dari awal window baru.
            log_entry(f"⚠️ EXPERIMENTAL {coin} {ew['stype']}: candle engulfing acuan sudah di luar "
                      f"window data — wait dibatalkan")
            st['ema_wait'] = None
            return
        last_checked_ts = ew.get('last_checked_ts')
        last_checked_i = _idx_of_ts(last_checked_ts) if last_checked_ts is not None else start_i - 1
        if last_checked_i is None:
            last_checked_i = start_i - 1
        for i in range(last_checked_i + 1, closed_end):
            bar_no = i - start_i + 1   # candle engulfing sendiri = bar_no 1
            if bar_no > EXPERIMENTAL_EMA_CROSS_BARS:
                break
            ema3_i  = float(df_m5['ema3'].iloc[i])
            ema20_i = float(df_m5['ema20'].iloc[i])
            ema3_prev  = float(df_m5['ema3'].iloc[i-1])
            ema20_prev = float(df_m5['ema20'].iloc[i-1])
            crossed = False
            # "Cross" = EMA3 menyentuh/sama dengan EMA20 (>=/<=), TIDAK perlu benar2 melewati (>/<)
            # — cukup bersinggungan dari sisi yang berlawanan di candle sebelumnya.
            if ew['stype'] == 'Long' and ema3_prev < ema20_prev and ema3_i >= ema20_i:
                crossed = True
            elif ew['stype'] == 'Short' and ema3_prev > ema20_prev and ema3_i <= ema20_i:
                crossed = True
            if crossed:
                note = (f"@ candle ke-{bar_no}/{EXPERIMENTAL_EMA_CROSS_BARS} SESUDAH engulfing "
                        f"({_ts_wib(df_m5['ts'].iloc[i])})")
                _experimental_execute_entry(coin, ew['stype'], df_m5, i, note=note)
                # Fokus baru = candle CROSS (tempat entry beneran terjadi), BUKAN candle engulfing
                # (awal window tunggu). Kalau dipakai candle engulfing, scan berikutnya akan
                # MENGULANG candle-candle di antara engulfing dan cross (yang bisa beberapa candle
                # kalau cross-nya baru terjadi di bar ke-2/3/dst) — dan bisa MENEMUKAN LAGI pola
                # engulfing yang sama persis, memicu ema_wait baru & entry KEDUA untuk struktur yang
                # identik. Dengan fokus di candle cross, scan berikutnya mulai dari candle SETELAH
                # cross — tidak pernah menoleh balik ke candle yang sudah dipakai entry.
                st['m5_focus_hi'] = float(df_m5['high'].iloc[i]); st['m5_focus_lo'] = float(df_m5['low'].iloc[i])
                st['m5_focus_ts'] = float(df_m5['ts'].iloc[i])
                st['ema_wait'] = None
                return
        # Belum cross sampai batas window → cek apakah window sudah habis
        last_bar_no = (closed_end - 1) - start_i + 1
        if last_bar_no >= EXPERIMENTAL_EMA_CROSS_BARS:
            log_entry(f"🚫 EXPERIMENTAL {coin} {ew['stype']}: EMA3 tidak cross sampai "
                      f"{EXPERIMENTAL_EMA_CROSS_BARS} candle — dibatalkan, fokus pindah")
            # Fokus ke candle TERAKHIR yang sudah diperiksa dalam window tunggu (bukan candle
            # engulfing awal) — supaya konsisten dgn fix di atas, scan berikutnya tidak menoleh
            # balik ke candle2 yang sudah pernah diperiksa selama fase tunggu.
            last_i = min(start_i + EXPERIMENTAL_EMA_CROSS_BARS - 1, closed_end - 1)
            st['m5_focus_hi'] = float(df_m5['high'].iloc[last_i]); st['m5_focus_lo'] = float(df_m5['low'].iloc[last_i])
            st['m5_focus_ts'] = float(df_m5['ts'].iloc[last_i])
            st['ema_wait'] = None
        else:
            st['ema_wait']['last_checked_ts'] = float(df_m5['ts'].iloc[closed_end - 1])
            # Log status periodik supaya jelas bot masih hidup selama menunggu cross (tidak ada
            # event lain yang bikin log muncul selama fase ini bisa berlangsung s/d 5 candle).
            wait_cycles = ew.get('wait_cycles', 0) + 1
            st['ema_wait']['wait_cycles'] = wait_cycles
            if wait_cycles % EXPERIMENTAL_IDLE_LOG_EVERY == 0:
                log_entry(f"⏳ EXPERIMENTAL {coin} {ew['stype']}: masih menunggu EMA3 cross EMA20 "
                          f"(candle ke-{last_bar_no}/{EXPERIMENTAL_EMA_CROSS_BARS}, sudah {wait_cycles} siklus)")
        return   # entry belum terjadi (nunggu cross ATAU baru saja dibatalkan) — scan fokus baru mulai siklus berikutnya

    # ── Scan fokus normal (belum ada calon engulfing yang lolos EMA-prev) ──
    focus_idx = _idx_of_ts(st.get('m5_focus_ts'))
    if focus_idx is None:
        # Fokus lama sudah keluar window (jarang) — reset fokus ke candle live terakhir lagi.
        focus_idx = closed_end - 1
        if focus_idx < 0:
            return
        st['m5_focus_ts'] = float(df_m5['ts'].iloc[focus_idx])
        st['m5_focus_hi'] = float(df_m5['high'].iloc[focus_idx])
        st['m5_focus_lo'] = float(df_m5['low'].iloc[focus_idx])
        log_entry(f"⚠️ EXPERIMENTAL {coin}: fokus lama di luar window data — di-reset ke candle live terakhir")
    f_hi = st['m5_focus_hi']; f_lo = st['m5_focus_lo']
    range_base = st.get('range_base', False)
    _focus_idx_before = focus_idx   # dipakai di akhir utk deteksi "tidak ada event sama sekali"
    for i in range(focus_idx + 1, closed_end):
        lo = float(df_m5['low'].iloc[i]); hi = float(df_m5['high'].iloc[i])
        cl = float(df_m5['close'].iloc[i])

        if not range_base and hi < f_hi and lo > f_lo:
            range_base = True
            log_entry(f"   EXPERIMENTAL {coin}: range base mode aktif @ "
                      f"{_ts_wib(df_m5['ts'].iloc[i]) if 'ts' in df_m5.columns else i} "
                      f"(candle base hi={f_hi:.6g} lo={f_lo:.6g})")

        long_sweep_opp  = (lo <= f_lo)
        short_sweep_opp = (hi >= f_hi)
        prev_hi = float(df_m5['high'].iloc[i-1]); prev_lo = float(df_m5['low'].iloc[i-1])

        engulf_long  = cl > f_hi and not long_sweep_opp  and not range_base
        engulf_short = cl < f_lo and not short_sweep_opp and not range_base
        if engulf_long or engulf_short:
            estype = 'Long' if engulf_long else 'Short'
            # Filter bias H1: hanya lanjutkan kalau arah engulfing SEARAH dengan bias H1
            # (EMA3/EMA20 cross terakhir di H1). Bias None (belum ada cross H1 live sejak bot
            # start) -> ditolak dulu, tunggu cross H1 pertama.
            if EXPERIMENTAL_H1_BIAS_FILTER:
                h1_bias = h1_bias_state.get(coin, {}).get('bias')
                if h1_bias != estype:
                    log_entry(f"   EXPERIMENTAL {coin} {estype}: engulfing @ "
                              f"{_ts_wib(df_m5['ts'].iloc[i]) if 'ts' in df_m5.columns else i} "
                              f"DITOLAK (bias H1 EMA3/EMA20 = {h1_bias or 'belum ada'}, butuh {estype})")
                    f_hi = hi; f_lo = lo; focus_idx = i; range_base = False
                    continue
            ema_prev = float(df_m5['ema20'].iloc[i-1])
            prev_ok = (EXPERIMENTAL_EMA_PREV is False) or (prev_lo <= ema_prev <= prev_hi)
            if not prev_ok:
                log_entry(f"   EXPERIMENTAL {coin} {estype}: engulfing @ "
                          f"{_ts_wib(df_m5['ts'].iloc[i]) if 'ts' in df_m5.columns else i} "
                          f"DITOLAK (candle sebelum engulfing tak sentuh EMA20)")
                f_hi = hi; f_lo = lo; focus_idx = i; range_base = False
                continue
            # Cek cross EMA3/EMA20 SEARAH engulfing di window EXPERIMENTAL_EMA_CROSS_BARS candle
            # SEBELUM engulfing (i-N..i-1). Kalau ketemu → EMA sudah cross duluan sebelum candle
            # engulfing ini terbentuk, jadi syarat cross sudah terpenuhi dari awal — entry MARKET
            # LANGSUNG di candle engulfing ini (harga = close candle i), tidak perlu nunggu lagi.
            # Kalau ADA beberapa cross searah di window itu, dipakai yang PALING DEKAT ke engulfing.
            cross_before_i = None
            for j in range(max(1, i - EXPERIMENTAL_EMA_CROSS_BARS), i):
                e3_j, e20_j   = float(df_m5['ema3'].iloc[j]),   float(df_m5['ema20'].iloc[j])
                e3_jp, e20_jp = float(df_m5['ema3'].iloc[j-1]), float(df_m5['ema20'].iloc[j-1])
                if estype == 'Long' and e3_jp < e20_jp and e3_j >= e20_j:
                    cross_before_i = j
                elif estype == 'Short' and e3_jp > e20_jp and e3_j <= e20_j:
                    cross_before_i = j
            if cross_before_i is not None:
                bars_before = i - cross_before_i
                note = (f"@ candle ke-{bars_before} SEBELUM engulfing "
                        f"({_ts_wib(df_m5['ts'].iloc[cross_before_i])}) → entry di engulfing "
                        f"({_ts_wib(df_m5['ts'].iloc[i])})")
                _experimental_execute_entry(coin, estype, df_m5, i, note=note)
                # Fokus baru = candle engulfing (tempat entry beneran terjadi) — scan berikutnya
                # mulai dari candle setelahnya, tidak menoleh balik.
                st['m5_focus_hi'] = hi; st['m5_focus_lo'] = lo; st['m5_focus_ts'] = float(df_m5['ts'].iloc[i])
                st['range_base'] = False
                st['ema_wait'] = None
                return
            # Belum ada cross di belakang → mulai tunggu cross EMA3/EMA20 ke DEPAN
            # SL BARU dihitung nanti di titik entry AKTUAL (cross terjadi) — persentase dari harga
            # entry sebenarnya, BUKAN dari ujung candle sebelum engulfing (itu bisa lebar/sempit
            # random tergantung besar candle; sekarang selalu proporsional EXPERIMENTAL_SL_PCT dari
            # harga entry beneran).
            entry_p_est = prev_hi if estype == 'Long' else prev_lo   # cuma estimasi utk log, bukan SL final
            log_entry(f"   EXPERIMENTAL {coin} {estype}: engulfing @ "
                      f"{_ts_wib(df_m5['ts'].iloc[i]) if 'ts' in df_m5.columns else i} lolos syarat EMA20-prev, "
                      f"belum ada cross {EXPERIMENTAL_EMA_CROSS_BARS} candle sebelumnya "
                      f"→ menunggu EMA3 cross EMA20 ke depan (maks {EXPERIMENTAL_EMA_CROSS_BARS} candle) | "
                      f"entry~{entry_p_est:.6g}")
            st['ema_wait'] = {
                'stype': estype, 'engulf_ts': float(df_m5['ts'].iloc[i]), 'prev_hi': prev_hi, 'prev_lo': prev_lo,
                'last_checked_ts': float(df_m5['ts'].iloc[i-1]),
            }
            st['m5_focus_ts'] = float(df_m5['ts'].iloc[focus_idx]); st['m5_focus_hi'] = f_hi; st['m5_focus_lo'] = f_lo
            st['range_base'] = range_base
            return

        # Update fokus (aturan dasar sama seperti _scan_engulf biasa)
        if range_base:
            if hi < f_hi and lo > f_lo:
                pass   # candle masih sepenuhnya di dalam rentang fokus — tetap diam
            else:
                # Sweep salah satu sisi (low<=f_lo atau high>=f_hi) → LANGSUNG pindah fokus ke
                # candle ini, apapun posisi close-nya (tidak perlu break/close tembus lagi).
                f_hi = hi; f_lo = lo; focus_idx = i; range_base = False
                log_entry(f"   EXPERIMENTAL {coin}: fokus pindah ke M5 "
                          f"{_ts_wib(df_m5['ts'].iloc[i]) if 'ts' in df_m5.columns else i} "
                          f"hi={hi:.6g} lo={lo:.6g} close={cl:.6g} (sweep dari range_base)")
        else:
            if hi > f_hi or lo < f_lo:
                f_hi = hi; f_lo = lo; focus_idx = i
                log_entry(f"   EXPERIMENTAL {coin}: fokus pindah ke M5 "
                          f"{_ts_wib(df_m5['ts'].iloc[i]) if 'ts' in df_m5.columns else i} "
                          f"hi={hi:.6g} lo={lo:.6g} close={cl:.6g}")

    st['m5_focus_ts'] = float(df_m5['ts'].iloc[focus_idx]); st['m5_focus_hi'] = f_hi; st['m5_focus_lo'] = f_lo
    st['range_base'] = range_base

    # Log status periodik kalau TIDAK ADA event apapun (fokus tidak pindah sama sekali) di
    # pemanggilan ini — supaya jelas bot masih hidup meski market lagi sideways/diam lama (mis.
    # range_base aktif terus-menerus tanpa breakout).
    if focus_idx == _focus_idx_before:
        idle_cycles = st.get('idle_cycles', 0) + 1
        st['idle_cycles'] = idle_cycles
        if idle_cycles % EXPERIMENTAL_IDLE_LOG_EVERY == 0:
            _mode = "range_base" if range_base else "normal"
            log_entry(f"⏳ EXPERIMENTAL {coin}: masih diam (mode={_mode}, fokus hi={f_hi:.6g} "
                      f"lo={f_lo:.6g}, sudah {idle_cycles} siklus tanpa event)")
    else:
        st['idle_cycles'] = 0


def process_setup(coin, setup, df_h1_live, curr_h1, df_m5=None):
    """Proses 1 setup (1 arah). Mutasi setup in-place.
    Return: 'remove' | 'keep' (WAIT_APPROACH) | 'lock' (WAIT_FILL) | 'fill' (posisi sudah dibuka)."""
    stype = setup['type']; choch_level = setup.get('choch_level'); bos_idx = setup.get('bos_idx', 0)
    # CHOCH invalidation (HISTORIS): kalau harga pernah close menembus choch setelah puncak -> mati
    if choch_level:
        bi0 = setup.get('bos_idx', 0)
        bts0 = setup.get('bos_ts', 0)
        rows0 = df_h1_live.index[df_h1_live['ts'] == bts0]
        if len(rows0) > 0:
            bi0 = int(rows0[0])
        if choch_is_broken(df_h1_live, bi0, choch_level, stype):
            if setup.get('order_id'): cancel_order(coin, setup['order_id'])
            print(f"🔄 {coin} {stype}: CHOCH {choch_level:.6f} sudah ditembus (historis). Setup batal.")
            return 'remove'
    # CHOCH/puncak invalidation (WICK M5, real-time): beda dari cek historis di atas yang pakai
    # CLOSE H1 — ini langsung batal begitu wick candle M5 menyentuh choch ATAU melewati puncak
    # (tren masih lanjut, puncak belum final). Tidak perlu tunggu candle H1 close.
    bos_stype_fvg = stype   # untuk FVG, `stype` = arah BOS besar itu sendiri
    if df_m5 is not None and 'ts' in df_m5.columns:
        _df_m5_chk = df_m5[df_m5['ts'] >= setup.get('created_ts', 0) * 1000]
        invalid, why = struct_touch_invalidated(_df_m5_chk, bos_stype_fvg, choch_level, setup.get('peak_val'))
        if invalid:
            if setup.get('order_id'): cancel_order(coin, setup['order_id'])
            print(f"🚫 {coin} {stype}: FVG setup batal — {why}")
            log_entry(f"🚫 {coin} {stype}: FVG setup batal — {why}")
            return 'remove'
    # Invalidasi struktur (historis): retrace >= RETRACE_LOCK lalu close lewati swing-2 (puncak 5-5)
    if REBREAK_INVALID:
        sw2 = setup.get('swing2')
        bi  = setup.get('bos_idx', 0)
        bts = setup.get('bos_ts', 0)
        rows = df_h1_live.index[df_h1_live['ts'] == bts]
        if len(rows) > 0:
            bi = int(rows[0])
        if sw2 is not None and rebreak_invalid(df_h1_live, bi, sw2, choch_level, stype, RETRACE_LOCK):
            if setup.get('order_id'): cancel_order(coin, setup['order_id'])
            print(f"🧱 {coin} {stype}: retrace>={RETRACE_LOCK*100:.0f}% lalu lewati swing-2 {sw2:.6f} — struktur baru, setup batal.")
            return 'remove'
    bos_ts_val = setup.get('bos_ts', 0)
    bos_rows   = df_h1_live.index[df_h1_live['ts'] == bos_ts_val]
    if len(bos_rows) > 0:
        bos_idx = int(bos_rows[0]); setup['bos_idx'] = bos_idx
    if bos_idx < len(df_h1_live):
        fresh = _get_fvgs(df_h1_live, stype, bos_idx, choch_level,
                          zone_lo=deepest_retrace_lo(df_h1_live, bos_idx, choch_level, stype))
        if fresh: setup['fvg_list'] = fresh
    if not setup.get('fvg_list'):
        if setup.get('order_id'): cancel_order(coin, setup['order_id'])
        print(f"🗑️ {coin} {stype}: Tidak ada FVG tersisa / tidak fresh.")
        return 'remove'
    curr_price = float(curr_h1['close'])

    # ── Cek FVG setup hangus: harga lari >= 20% BOS range dari C3 ujung ke arah BOS ──
    if setup.get('phase') == 'WAIT_APPROACH' and setup.get('bos_rng', 0) > 0:
        c3_trig      = float(setup.get('orig_ocl', 0))
        cancel_dist  = FVG_CANCEL_RANGE_PCT * float(setup['bos_rng'])
        if c3_trig > 0 and cancel_dist > 0:
            hi_now = float(curr_h1.get('high', curr_price))
            lo_now = float(curr_h1.get('low',  curr_price))
            hangus = (stype == 'Long'  and lo_now <= c3_trig - cancel_dist) or                      (stype == 'Short' and hi_now >= c3_trig + cancel_dist)
            if hangus:
                print(f"🚫 {coin} {stype}: FVG hangus — harga lari "
                      f">={FVG_CANCEL_RANGE_PCT*100:.0f}% range BOS dari trigger1 "
                      f"({c3_trig:.6g}) ke arah BOS tanpa engulfing.")
                return 'remove'

    # ── WAIT_APPROACH ──
    if setup['phase'] == 'WAIT_APPROACH':
        entry = setup['entry']; dist = setup['dist']
        # NB: side_order TIDAK dihitung di sini lagi — arah final baru pasti setelah gate EMA20 H1
        # selesai (bisa searah stype asli, bisa dibalik "mode EMA"). Dipakai langsung dari
        # engulf['side'] yang sudah dihitung check_m5_engulfing() memakai arah hasil gate.

        # ── PATH A: FVG monitor M5 setelah trigger1 (ujung gap) tersentuh ──
        # Tidak pakai APPROACH_R. Monitor dimulai begitu trigger1 tersentuh di M5.
        if M5_ENGULF_FILTER and df_m5 is not None:
            c3_trig = float(setup.get('orig_ocl', entry))
            bos_rng = float(setup.get('bos_rng') or abs(float(setup.get('peak_val') or 0) - float(setup.get('choch_level') or 0)))
            if setup.get('m5_c1c_touched'):
                _dir_now = setup.get('h1_ema_dir') or stype
                print(f"👁️  {coin} {stype} | now:{curr_price:.6f} trigger1:{c3_trig:.6f} | "
                      f"arah:{_dir_now} | monitor engulfing M5...")
            else:
                _pct_fvg = abs(curr_price - c3_trig) / c3_trig * 100 if c3_trig else 0
                print(f"👁️  {coin} {stype} | now:{curr_price:.6f} trigger1:{c3_trig:.6f} | "
                      f"menunggu sentuhan ({_pct_fvg:.2f}% lagi)")
            active_count = len(active_positions) + _count_slots()
            if active_count >= MAX_CONCURRENT:
                print(f"\u23f8\ufe0f  {coin}: slot penuh ({active_count}/{MAX_CONCURRENT})")
                return 'keep'
            engulf = check_m5_engulfing(coin, setup, df_m5, bos_rng, df_h1=df_h1_live)
            if engulf and engulf.get('cancelled'):
                return 'remove'   # cross-check: arah dibalik ini sudah "milik" IDM, batalkan FVG
            if engulf:
                # Pasang LIMIT ORDER di ujung candle fokus (bukan market order)
                limit_entry = engulf['entry']   # high fokus (Long) / low fokus (Short)
                limit_sl    = engulf['sl']       # entry fixed ± SL_ENGULF_PCT% range BOS besar
                side_order  = engulf['side']     # 'Buy'/'Sell' — SUDAH pakai arah hasil gate EMA20 H1
                final_dir   = setup.get('h1_ema_dir', stype)
                oid = place_limit_order(coin, side_order, limit_entry, limit_sl)
                if oid:
                    setup['phase']    = 'WAIT_FILL'
                    setup['order_id'] = oid
                    setup['entry']    = limit_entry
                    setup['sl']       = limit_sl
                    setup['dist']     = abs(limit_entry - limit_sl)
                    _mode_lbl = "" if final_dir == stype else " [MODE EMA — arah dibalik]"
                    print(f"\U0001f4cd {coin} {final_dir}: ENGULFING M5 → LIMIT @ {limit_entry:.6f} "
                          f"SL:{limit_sl:.6f} | break:{setup.get('swing_val'):.6g} "
                          f"puncak:{setup.get('peak_val'):.6g}{_mode_lbl}")
                    _eo = engulf.get('engulf_ohlc', {})
                    log_entry(
                        f"════ FVG ENGULF LIMIT {final_dir} {coin} @ {limit_entry:.6g}{_mode_lbl} ════\n"
                        f"  Candle ENGULFING: open={_eo.get('open',0):.6g} high={_eo.get('high',0):.6g} "
                        f"low={_eo.get('low',0):.6g} close={_eo.get('close',0):.6g} "
                        f"→ entry = {limit_entry:.6g} (high/low candle sebelum engulfing)\n"
                        f"  SL: entry={limit_entry:.6g} fixed ± buffer({SL_ENGULF_PCT*100:.0f}% range BOS)="
                        f"{engulf.get('sl_buffer',0):.6g} → SL final={limit_sl:.6g}"
                    )
                    return 'lock'
            return 'keep'
        # ── PATH B: Fallback limit (M5_ENGULF_FILTER=False) ──
        # Jalur lama ini TIDAK lewat gate EMA20 H1 (murni retracement H1), jadi side_order tetap
        # pakai stype asli.
        side_order = "Buy" if stype == "Long" else "Sell"
        thr = APPROACH_R * dist
        approaching = (stype == 'Long'  and curr_price <= entry + thr) or                       (stype == 'Short' and curr_price >= entry - thr)
        r_now  = ((curr_price - entry) if stype == 'Long' else (entry - curr_price)) / dist if dist > 0 else 0
        to_arm = r_now - APPROACH_R
        r_info = (f"{r_now:.2f}R dari entry" if approaching else
                  f"{r_now:.2f}R dari entry (pasang di {APPROACH_R:.1f}R, kurang {to_arm:.2f}R lagi)")
        print(f"\U0001f441\ufe0f  {coin} {stype} | now:{curr_price:.6f} entry:{entry:.6f} | {r_info} | "
              f"{'\u2705 DALAM RANGE' if approaching else '\u23f3 menunggu'}")
        if approaching:
            direction_valid = (stype == 'Long' and curr_price > entry) or                               (stype == 'Short' and curr_price < entry)
            if not direction_valid:
                print(f"\u26d4 {coin} {stype}: harga {curr_price:.6f} sudah lewat zona {entry:.6f} \u2014 batal.")
                return 'remove'
            active_count = len(active_positions) + _count_slots()
            if active_count >= MAX_CONCURRENT:
                print(f"\u23f8\ufe0f  {coin}: slot penuh ({active_count}/{MAX_CONCURRENT})")
                return 'keep'
            order_id = place_limit_order(coin, side_order, entry, setup['sl'])
            if order_id:
                setup['phase'] = 'WAIT_FILL'; setup['order_id'] = order_id
                print(f"\U0001f4cd {coin} {stype}: Limit dipasang @ {entry:.6f} (dalam {APPROACH_R}R) | "
                      f"break:{setup.get('swing_val'):.6g} puncak:{setup.get('peak_val'):.6g} CHOCH:{setup.get('choch_level'):.6g}")
                return 'lock'
            return 'remove'
        return 'keep'

    # ── WAIT_FILL ──
    if setup['phase'] == 'WAIT_FILL':
        # NB: sebelumnya di sini ada cancel kalau harga lari >APPROACH_R×dist dari entry.
        # Sudah dihapus atas permintaan — limit FVG SEKARANG hanya dibatalkan kalau BOS baru
        # terdeteksi di H1 (swing_val berubah), bukan karena harga lari jauh. Lihat run_bot loop
        # (blok "Re-deteksi DUA ARAH") untuk logika pembatalan BOS-baru itu.
        # final_stype = arah SESUNGGUHNYA dari order yang terpasang — hasil gate EMA20 H1 kalau
        # lewat Path A (bisa sama dgn stype, bisa dibalik "mode EMA"), atau stype asli kalau lewat
        # Path B (fallback lama, tak ada gate EMA, h1_ema_dir selalu None).
        final_stype = setup.get('h1_ema_dir') or stype
        # BUG FIX: sebelumnya di sini langsung percaya get_open_position(coin, side) — tapi itu bisa
        # menemukan POSISI LAIN yang sudah ada duluan di sisi yang sama (mis. dari limit order LAIN
        # yang sudah fill duluan), padahal order limit UNTUK SETUP INI ('order_id') sendiri belum
        # filled sama sekali. Akibatnya SL posisi yang sudah ada tertimpa oleh SL setup yang order-nya
        # belum fill. Sekarang wajib cek order_id milik setup ini benar2 Filled dulu.
        oid = setup.get('order_id')
        if oid and not _order_was_filled(coin, oid):
            if _order_exists(coin, oid):
                return 'keep'   # order masih pending di exchange, belum fill — jangan sentuh apapun
            else:
                # Order tidak ada lagi di exchange TAPI juga bukan Filled (kemungkinan cancelled/expired
                # di luar kendali bot) — aman untuk dibuang, bukan dianggap fill.
                print(f"⚠️ {coin} {stype}: order {oid} sudah tidak ada & bukan Filled — setup dibuang.")
                return 'remove'
        pos = get_open_position(coin, 'Buy' if final_stype == 'Long' else 'Sell')
        if pos:
            entry_p = setup['entry']; sl_p = setup['sl']
            side_order = "Buy" if final_stype == "Long" else "Sell"
            actual_entry = float(pos.get('avgPrice', entry_p))
            actual_dist  = abs(actual_entry - sl_p)
            min_dist = actual_entry * 0.002
            if actual_dist < min_dist:
                actual_dist = min_dist
                sl_p = actual_entry - actual_dist if side_order == "Buy" else actual_entry + actual_dist
                print(f"⚠️ {coin}: SL diperlebar ke {sl_p:.6f}")
            trail_d = TRAIL_STOP * actual_dist
            info = get_instrument_info(coin); tick = info.get('tick_size', 0.0001)
            sl_r = round_price(sl_p, tick); trail_r = round_price(trail_d, tick)
            active_p = round_price(
                actual_entry + TRAIL_ACT_R * actual_dist if side_order == "Buy"
                else actual_entry - TRAIL_ACT_R * actual_dist, tick)
            trail_set_ok = False
            for _attempt in range(3):
                try:
                    if USE_TP:
                        tp_r = round_price(actual_entry + RR_TP * actual_dist if side_order == "Buy" else actual_entry - RR_TP * actual_dist, tick)
                        res_ts = session.set_trading_stop(category=CATEGORY, symbol=coin, stopLoss=str(sl_r), takeProfit=str(tp_r), positionIdx=_pidx(side_order))
                    else:
                        res_ts = session.set_trading_stop(category=CATEGORY, symbol=coin, stopLoss=str(sl_r), trailingStop=str(trail_r), activePrice=str(active_p), positionIdx=_pidx(side_order))
                    if res_ts.get('retCode', -1) == 0:
                        trail_set_ok = True
                        print(f"🛡️  {coin}: SL={sl_r} " + (f"TP={tp_r} (1:{RR_TP})" if USE_TP else f"Trail={trail_r} act={active_p}"))
                        break
                    else:
                        print(f"⚠️ {coin}: set_trading_stop gagal: {res_ts.get('retMsg','')}"); time.sleep(2)
                except Exception as e:
                    print(f"⚠️ {coin}: set_trading_stop error: {e}"); time.sleep(2)
            if not trail_set_ok:
                print(f"⚠️ {coin}: Trail gagal — retry M5 berikutnya")
            active_positions[_akey(coin, final_stype)] = {
                'coin': coin,
                'side': side_order, 'entry': actual_entry, 'sl': sl_p, 'dist': actual_dist,
                'trail_dist': trail_d, 'trail_engaged': False, 'trail_set': trail_set_ok,
                'last_price': actual_entry, 'entry_time': time.time(),
                'peak': actual_entry, 'peak_time': time.time(),
                'swing_val': setup.get('swing_val'), 'bos_type': final_stype, 'rev_count': 0,
                'orig_ocl': setup.get('orig_ocl', setup.get('entry')),
                'choch_level': setup.get('choch_level'), 'peak_val': setup.get('peak_val'),
                'swing2': setup.get('swing2'),
            }
            done_setups[coin] = {'swing_val': setup.get('swing_val'), 'stype': stype, 'used_ocl': setup.get('entry')}
            print(f"✅ {coin} {stype}: Limit filled! Entry:{actual_entry:.6f} SL:{sl_p:.6f} | "
                  f"break:{setup.get('swing_val'):.6g} puncak:{setup.get('peak_val'):.6g} CHOCH:{setup.get('choch_level'):.6g}")
            return 'fill'
        else:
            oid = setup.get('order_id')
            if oid and not _order_exists(coin, oid):
                if _order_was_filled(coin, oid):
                    print(f"📭 {coin} {stype}: Limit filled lalu tutup (SL) — selesai.")
                    return 'remove'
                else:
                    print(f"📤 {coin} {stype}: Limit hilang (cancel) — kembali menunggu.")
                    setup['phase'] = 'WAIT_APPROACH'; setup.pop('order_id', None)
                    return 'keep'
        return 'lock'
    return 'keep'


# ============================================================
#  MODE STRUKTURAL — BOS H1 -> IDM (wajib) -> BOS M5 lawan arah -> CHoCH balik -> entry 50%/SL 100%
#  Independen total dari jalur EMA/FVG/IDM lama di atas (check_m5_engulfing / process_setup /
#  check_inducement_entry / check_idm_pending) — TIDAK menyentuh state 'pending' atau 'idm_pending'.
# ============================================================

def _build_idm_legs_flex(df_tf, big_stype, n_range=(1, 2, 3, 4), ts_lo=None, ts_hi=None):
    """Sama seperti _build_idm_legs, TAPI swing point-nya digabung dari BEBERAPA n sekaligus
    (default 1-1 sampai 4-4) alih-alih satu n tetap. Prinsip: swing manapun yang valid di rentang
    1-1..4-4 dianggap kandidat IDM yang sah — n bukan prioritas/filter kualitas, cuma syarat minimal
    'ada retrace yang belum ditembus di kiri-kanan candle itu'. Begitu candle-candle berjalan &
    swing point BARU (dengan n berapapun dalam rentang) muncul lebih dekat ke sekarang & levelnya
    break record sebelumnya, itu jadi leg baru — leg TERAKHIR (paling dekat titik acuan sekarang)
    yang otomatis jadi IDM aktif saat ini, TANPA syarat leg mana yang n-nya berapa.
    Return list leg (trigger_val, trigger_idx, broken_peak_val, broken_peak_idx), urut kronologis."""
    if df_tf is None:
        return []
    up = (big_stype == "Long")
    seen_idx = set()
    piv = []
    for n in n_range:
        if len(df_tf) < (2 * n + 1):
            continue
        sh_tf, sl_tf = find_last_swing_bos(df_tf, n=n)
        cand = sh_tf if up else sl_tf
        for s in cand:
            if s['idx'] in seen_idx:
                continue   # sudah ada dari n lain (kandidat idx sama) -> jangan dobel
            seen_idx.add(s['idx'])
            piv.append(s)
    if len(piv) < 2:
        return []
    ts_col = df_tf['ts']
    def _in_win(idx):
        if ts_lo is None:
            return True
        t = float(ts_col.iloc[idx])
        return ts_lo <= t <= ts_hi
    piv = [s for s in piv if _in_win(s['idx'])]
    piv.sort(key=lambda s: s['idx'])
    if len(piv) < 2:
        return []
    lo_a = df_tf['low'].values; hi_a = df_tf['high'].values
    legs = []
    rec_v, rec_i = piv[0]['val'], piv[0]['idx']
    for s in piv[1:]:
        is_break = (s['val'] > rec_v) if up else (s['val'] < rec_v)
        if not is_break:
            continue
        a_seg, b_seg = rec_i + 1, s['idx'] + 1          # INKLUSIF candle record-break akhir
        if b_seg > a_seg:
            if up:
                seg = lo_a[a_seg:b_seg]; off = int(seg.argmin()); tval = float(seg.min())
            else:
                seg = hi_a[a_seg:b_seg]; off = int(seg.argmax()); tval = float(seg.max())
            legs.append((tval, a_seg + off, rec_v, rec_i))
        rec_v, rec_i = s['val'], s['idx']
    return legs


def _build_idm_legs(df_tf, big_stype, n=1, ts_lo=None, ts_hi=None):
    """Bangun rantai leg IDM — PERSIS logika inti find_inducement (record-break berturut) — tapi
    TANPA memfilter ke satu leg 'terbaik'. Dipakai saat butuh SEMUA leg (bukan cuma leg terakhir)
    supaya bisa difilter lagi (mis. filter FVG-backed). Return list leg (trigger_val, trigger_idx,
    broken_peak_val, broken_peak_idx), urut kronologis (leg terakhir = paling dekat puncak)."""
    if df_tf is None or len(df_tf) < (2 * n + 1):
        return []
    sh_tf, sl_tf = find_last_swing_bos(df_tf, n=n)
    if not sh_tf or not sl_tf:
        return []
    ts_col = df_tf['ts']
    def _in_win(idx):
        if ts_lo is None:
            return True
        t = float(ts_col.iloc[idx])
        return ts_lo <= t <= ts_hi
    up = (big_stype == "Long")
    piv = [s for s in (sh_tf if up else sl_tf) if _in_win(s['idx'])]
    piv.sort(key=lambda s: s['idx'])
    if len(piv) < 2:
        return []
    lo_a = df_tf['low'].values; hi_a = df_tf['high'].values
    legs = []
    rec_v, rec_i = piv[0]['val'], piv[0]['idx']
    for s in piv[1:]:
        is_break = (s['val'] > rec_v) if up else (s['val'] < rec_v)
        if not is_break:
            continue
        a_seg, b_seg = rec_i + 1, s['idx'] + 1          # INKLUSIF candle record-break akhir
        if b_seg > a_seg:
            if up:
                seg = lo_a[a_seg:b_seg]; off = int(seg.argmin()); tval = float(seg.min())
            else:
                seg = hi_a[a_seg:b_seg]; off = int(seg.argmax()); tval = float(seg.max())
            legs.append((tval, a_seg + off, rec_v, rec_i))
        rec_v, rec_i = s['val'], s['idx']
    return legs


def _pick_fvg_backed_idm(legs, gaps, stype, df_h1_live=None, peak_idx=None):
    """Dari daftar leg IDM (rantai record-break, biasanya BANYAK), pilih leg yang PALING DEKAT
    PUNCAK tapi TEPAT DI BAWAHNYA ada FVG asli — bukan cuma leg IDM lain lagi.
    Alasan: kalau leg TERAKHIR (paling dekat puncak, yang biasanya jadi default) ternyata di
    bawahnya cuma ketemu leg IDM lain (tanpa FVG di antaranya), itu IDM lemah — turun ke leg
    SEBELUMNYA, ulangi sampai ketemu yang bawahnya FVG asli. Kalau tidak ada satupun → None.
    (Long: 'bawah' = harga lebih rendah. Short: 'bawah'/lawan = harga lebih tinggi.)
    PENTING: FVG yang SUDAH SEBAGIAN TERISI (partial fill, tapi belum 100% hangus — itu domain
    c1_is_fresh terpisah) dipakai LEVEL EFEKTIFNYA (sisa area yang masih kosong, via
    gap_entry_point), BUKAN top/bottom mentah saat gap pertama terbentuk. Kalau candle setelah
    gap terbentuk sudah menembus masuk sampai suatu level, bagian yang sudah terisi itu tidak lagi
    dianggap 'FVG di situ' untuk keperluan pemilihan IDM — supaya tidak salah menganggap FVG hadir
    di level yang sebenarnya sudah terisi candle lain (termasuk leg IDM lain di dekatnya)."""
    if not legs or not gaps:
        return None
    legs_sorted = sorted(legs, key=lambda lg: lg[0], reverse=(stype == "Long"))
    # Level FVG EFEKTIF (sisa belum terisi) — dihitung sekali per gap, dipakai di semua perbandingan.
    if df_h1_live is not None:
        eff = [gap_entry_point(df_h1_live, g, stype, peak_idx) for g in gaps]
    else:
        eff = [(float(g['top']) if stype == 'Long' else float(g['bottom'])) for g in gaps]
    for lg in legs_sorted:
        trig = lg[0]
        lower_legs = [o[0] for o in legs_sorted if o is not lg and
                      ((o[0] < trig) if stype == "Long" else (o[0] > trig))]
        nearest_lower_leg = (max(lower_legs) if stype == "Long" else min(lower_legs)) if lower_legs else None
        if stype == "Long":
            cand = [e for e in eff if e <= trig]
            nearest_gap = max(cand) if cand else None
        else:
            cand = [e for e in eff if e >= trig]
            nearest_gap = min(cand) if cand else None
        if nearest_gap is None:
            continue   # gak ada FVG (sisa kosong) sama sekali di bawah leg ini -> skip, coba leg lebih rendah
        if nearest_lower_leg is None or \
           (stype == "Long" and nearest_gap > nearest_lower_leg) or \
           (stype == "Short" and nearest_gap < nearest_lower_leg):
            return lg   # FVG (sisa kosong) lebih dekat ke leg ini drpd leg IDM lain -> "bawahnya langsung FVG"
    return None


def build_h1_struct_setup(coin, df_h1_live, sh_h1, sl_h1, verbose=False, force_dir=None):
    """Deteksi BOS H1 terbaru -> FVG (WAJIB ADA) -> IDM (WAJIB, & harus leg yg tepat di bawahnya
    ada FVG asli) -> setup WAIT_IDM_TOUCH. BOS tanpa FVG dianggap LEMAH dan di-skip; begitu juga
    kalau semua leg IDM di pita ternyata di bawahnya cuma ketemu IDM lain (bukan FVG).
    Sama sekali TIDAK memakai gate EMA20 H1, trig1/C1-close FVG, ataupun entry IDM langsung.
    force_dir='Long'/'Short' => deteksi HANYA arah itu. Return (setup_dict, logline) atau (None, None).
    """
    if not sh_h1 or not sl_h1:
        return None, None
    is_long = False; is_short = False; swing_val = None; brk_idx = None
    if force_dir in (None, "Long"):
        sv, bi = pick_bos_swing(df_h1_live, sh_h1, sl_h1, "Long")
        if sv is not None: is_long = True; swing_val = sv; brk_idx = bi
    if force_dir in (None, "Short"):
        sv, bi = pick_bos_swing(df_h1_live, sh_h1, sl_h1, "Short")
        if sv is not None: is_short = True; swing_val = sv; brk_idx = bi
    if not (is_long or is_short):
        if verbose: print(f"   {coin}: (struct) tidak ada BOS {force_dir or 'H1'}")
        return None, None
    stype = force_dir if force_dir else ("Short" if is_short else "Long")
    bos_idx, choch_level, peak_val = impulse_anchors(stype, swing_val, brk_idx, sh_h1, sl_h1, df_h1_live)
    if swing_val is None or bos_idx is None or choch_level is None:
        if verbose: print(f"   {coin}: (struct) BOS {stype} tak lengkap (swing/choch 5-5 belum terbentuk)")
        return None, None
    if stype == "Long":
        sub = df_h1_live['high'].iloc[bos_idx:]; _B = float(sub.max()); peak_idx = int(sub.idxmax())
    else:
        sub = df_h1_live['low'].iloc[bos_idx:]; _B = float(sub.min()); peak_idx = int(sub.idxmin())
    res = apply_latest_leg(df_h1_live, sh_h1, sl_h1, stype, swing_val, brk_idx, choch_level, peak_val, _B, peak_idx, bos_idx)
    if res is None:
        if verbose: print(f"   {coin}: (struct) BOS {stype} — swing-2 ditembus & leg baru tanpa choch 5-5")
        return None, None
    swing_val, brk_idx, choch_level, peak_val, bos_idx = res
    bos_rng = (_B - choch_level) if stype == "Long" else (choch_level - _B)
    if bos_rng <= 0:
        return None, None
    seg_cl = df_h1_live['close'].iloc[peak_idx:]
    choch_broken = bool((seg_cl < choch_level).any()) if stype == "Long" else bool((seg_cl > choch_level).any())
    if choch_broken:
        if verbose: print(f"   {coin}: (struct) BOS {stype} sudah CHoCH — mati, tunggu BOS baru")
        return None, None
    if REBREAK_INVALID and peak_val is not None and \
       rebreak_invalid(df_h1_live, bos_idx, peak_val, choch_level, stype, RETRACE_LOCK):
        if verbose: print(f"   {coin}: (struct) BOS {stype} INVALID — rebreak swing-2")
        return None, None
    # FVG dihitung DULU (bukan cuma info lagi) — dipakai memilih leg IDM mana yang valid.
    zlo = deepest_retrace_lo(df_h1_live, bos_idx, choch_level, stype)
    gaps = _get_fvgs(df_h1_live, stype, bos_idx, choch_level, zone_lo=zlo)
    if not gaps:
        if verbose:
            print(f"   {coin}: (struct) BOS {stype} TAK ADA FVG sama sekali — BOS dianggap lemah, skip")
        return None, None
    # IDM WAJIB — dan HARUS leg yang tepat di bawahnya (Long) / atasnya (Short) ada FVG asli, bukan
    # cuma leg IDM lain lagi (rantai record-break bisa dapat banyak leg; leg paling dekat puncak
    # sering kali "cuma umpan" kalau di bawahnya ternyata IDM lain, bukan FVG).
    if stype == "Long":
        ib_lo, ib_hi = _B - INDUCEMENT_ZONE_HI * bos_rng, _B - INDUCEMENT_ZONE_LO * bos_rng
    else:
        ib_lo, ib_hi = _B + INDUCEMENT_ZONE_LO * bos_rng, _B + INDUCEMENT_ZONE_HI * bos_rng
    its_lo = float(df_h1_live['ts'].iloc[bos_idx]); its_hi = float(df_h1_live['ts'].iloc[peak_idx])
    df_idm = df_h1_live if INDUCEMENT_TF == "60" else get_data(coin, "5", limit=300)
    all_legs = _build_idm_legs(df_idm, stype, n=INDUCEMENT_SWING, ts_lo=its_lo, ts_hi=its_hi) if df_idm is not None else []
    inband_legs = [lg for lg in all_legs if ib_lo <= lg[0] <= ib_hi]
    if not inband_legs:
        if verbose:
            print(f"   {coin}: (struct) BOS {stype} TAK ada IDM mini-BOS {INDUCEMENT_SWING}-{INDUCEMENT_SWING} "
                  f"di pita {INDUCEMENT_ZONE_LO*100:.0f}-{INDUCEMENT_ZONE_HI*100:.0f}% | "
                  f"break:{swing_val:.6g} choch:{choch_level:.6g} puncak:{_B:.6g}")
        return None, None
    chosen = _pick_fvg_backed_idm(inband_legs, gaps, stype, df_h1_live=df_h1_live, peak_idx=peak_idx)
    if chosen is None:
        if verbose:
            print(f"   {coin}: (struct) BOS {stype} ada {len(inband_legs)} leg IDM di pita, tapi TAK ADA "
                  f"yg tepat di bawahnya FVG asli (semua cuma ketemu IDM lain) — BOS dianggap lemah, skip")
        return None, None
    idm_chk = {'prot': chosen[0], 'prot_idx': chosen[1], 'micro_val': chosen[2], 'micro_idx': chosen[3],
               'n_trigger': len(inband_legs)}
    # Filter sesi & funding — sama seperti jalur lama, biar konsisten kebijakan risikonya
    import datetime as _dt
    _h_s = _dt.datetime.utcfromtimestamp(df_h1_live.iloc[-1]['ts_ms'] / 1000).hour if 'ts_ms' in df_h1_live.columns else -1
    if _h_s >= 0:
        _sesi = 'Asia' if _h_s < 8 else ('London' if _h_s < 13 else 'NY')
        _allowed = SESSION_FILTER.get(coin)
        if _allowed is not None and _sesi not in _allowed:
            return None, None
    if FUNDING_FILTER and in_funding_window() and not funding_favors(stype, coin):
        if verbose:
            print(f"   {coin}: (struct) BOS {stype} skip — funding window aktif, gak searah")
        return None, None
    bos_ts = float(df_h1_live['ts'].iloc[bos_idx])
    idm_level = float(idm_chk['prot'])
    choch_str = f"{choch_level:.6g}" if choch_level else "—"
    logline = (f"\n🧱 {coin} | BOS(struct) {stype} | break:{swing_val:.6g} puncak:{_B:.6g} CHOCH:{choch_str} | "
               f"IDM(FVG-backed):{idm_level:.6g} ({idm_chk['n_trigger']} leg di pita) | FVG:{len(gaps)} "
               f"→ menunggu retrace M5 ke IDM")
    setup = {
        'type': stype, 'phase': 'WAIT_IDM_TOUCH',
        'bos_idx': bos_idx, 'swing_val': swing_val, 'choch_level': choch_level,
        'peak_val': _B, 'swing2': peak_val, 'bos_rng': bos_rng, 'bos_ts': bos_ts,
        'idm_level': idm_level, 'idm_n': idm_chk['n_trigger'],
        'created_ts': time.time(),
        # State fase M5 (diisi belakangan) — mekanisme rolling generik (break/ujung/terjauh)
        'm5_scan_from_ts': None,
        'm5_swing_val': None, 'm5_choch_level': None, 'm5_peak': None,
        'm5_idm': None,
        'm5_break_lvl': None, 'm5_ujung_lvl': None, 'm5_terjauh_lvl': None,
        'm5_break_dir': None, 'm5_break_since_ts': None,
    }
    return setup, logline


def _find_m5_rbs(df_seg, stype):
    """Cari RBS (Resistance Become Support, stype=Long) / SBR (Support Become Resistance, stype=Short)
    KECIL di dalam leg CHoCH M5, dicari dari UJUNG (titik ekstrem/karakter berbalik) ke UJUNG lain
    (sekarang) — persis seperti IDM H1 dicari dari titik choch ke titik puncak.
    Pola (contoh Long/RBS): candle1 SEARAH CHoCH (hijau) close, candle2 LAWAN (merah) open dekat situ
    lalu harga menjauh (turun) — WAJIB ada minimal 1 candle setelahnya yang TIDAK balik menyentuh
    zona [close1, open2] (ada 'space'/gap dulu, baru sah). Zona itu jadi resisten KECIL. Begitu ada
    candle yang CLOSE menembus balik ke atas zona itu (searah CHoCH) → RBS terkonfirmasi — TAPI kalau
    SETELAH break itu ada candle yang close balik menembus zona ke arah SEBALIKNYA (sweep balik),
    RBS ini batal/tidak valid.
    Ambil kandidat TERAKHIR (paling baru/dekat dgn sekarang) yang valid — sama seperti rantai BOS-m5
    & IDM H1 yang selalu pakai leg TERAKHIR, BUKAN yang pertama kali ketemu secara historis (zona lama
    yang lebih lebar/basi diabaikan kalau ada zona lebih baru yang juga valid).
    Return dict {zone_hi, zone_lo, c1_idx, c2_idx, break_idx} atau None."""
    n = len(df_seg)
    best = None
    for i in range(0, n - 2):
        c1_o, c1_c = float(df_seg['open'].iloc[i]), float(df_seg['close'].iloc[i])
        c2_o, c2_c = float(df_seg['open'].iloc[i + 1]), float(df_seg['close'].iloc[i + 1])
        if stype == 'Long':
            if not (c1_c > c1_o and c2_c < c2_o):   # c1 hijau (searah CHoCH), c2 merah (lawan)
                continue
        else:
            if not (c1_c < c1_o and c2_c > c2_o):   # c1 merah (searah CHoCH), c2 hijau (lawan)
                continue
        zone_hi = max(c1_c, c2_o); zone_lo = min(c1_c, c2_o)
        if zone_hi < zone_lo:
            continue   # (harusnya mustahil karena max/min, cuma jaga-jaga)
        space_found = False
        break_idx = None
        for j in range(i + 2, n):
            hi_j = float(df_seg['high'].iloc[j]); lo_j = float(df_seg['low'].iloc[j])
            cl_j = float(df_seg['close'].iloc[j])
            if not space_found:
                if stype == 'Long' and hi_j < zone_lo:
                    space_found = True
                elif stype == 'Short' and lo_j > zone_hi:
                    space_found = True
                elif not (hi_j < zone_lo or lo_j > zone_hi):
                    break   # overlap balik SEBELUM sempat ada space -> pola gagal, coba i berikutnya
                continue
            if break_idx is None:
                if stype == 'Long' and cl_j > zone_hi:
                    break_idx = j
                elif stype == 'Short' and cl_j < zone_lo:
                    break_idx = j
                continue
            # Sudah break -> cek SWEEP BALIK: kalau ada candle setelah break yang close-nya balik
            # menembus zona ke arah lawan, RBS/SBR ini batal (bukan kandidat valid).
            if stype == 'Long' and cl_j < zone_lo:
                break_idx = None; break
            if stype == 'Short' and cl_j > zone_hi:
                break_idx = None; break
        if break_idx is not None:
            best = {'zone_hi': zone_hi, 'zone_lo': zone_lo, 'c1_idx': i, 'c2_idx': i + 1, 'break_idx': break_idx}
    return best


def process_struct_setup(coin, setup, df_m5):
    """State machine: WAIT_IDM_TOUCH -> WAIT_M5_CHOCH (rantai BOS-m5 + CHoCH + RBS/SBR) -> WAIT_FILL -> fill.
    Return: 'remove' | 'keep' | 'lock' (WAIT_FILL) | 'fill'."""
    stype = setup['type']
    opp = 'Short' if stype == 'Long' else 'Long'
    if df_m5 is None or 'ts' not in df_m5.columns or len(df_m5) < (2 * SWING_BARS + 3):
        return 'keep'
    n = len(df_m5)
    closed_end = n - 1   # exclude candle M5 yang masih live/berjalan

    # ── WAIT_IDM_TOUCH: tunggu harga M5 retrace menyentuh level IDM ──
    if setup['phase'] == 'WAIT_IDM_TOUCH':
        idm_level = setup['idm_level']; bos_ts = setup['bos_ts']
        touched_idx = None
        for i in range(closed_end):
            if float(df_m5['ts'].iloc[i]) < bos_ts:
                continue   # abaikan candle SEBELUM BOS H1 terbentuk (anti stale-touch)
            lo = float(df_m5['low'].iloc[i]); hi = float(df_m5['high'].iloc[i])
            if (stype == 'Long' and lo <= idm_level) or (stype == 'Short' and hi >= idm_level):
                touched_idx = i; break
        if touched_idx is None:
            return 'keep'
        # ── Anti-basi: kalau PUNCAK H1 juga SUDAH tersentuh di histori SETELAH IDM tersentuh (bukan
        #    live, baru ketahuan sekarang krn baru dideteksi/redeploy atau tren masih jalan terus),
        #    berarti window IDM->puncak yang ADA SEKARANG sudah lewat & basi — jangan masuk
        #    WAIT_M5_CHOCH dulu (nanti cuma langsung "puncak kesentuh lagi" trivial krn peak_val
        #    dihitung dari data yg SAMA ini). TETAP di WAIT_IDM_TOUCH (jangan 'remove' — biar gak
        #    hancur-bikin-ulang tiap siklus & spam log): re-deteksi H1 normal di main loop akan
        #    otomatis update swing_val/peak_val begitu tren benar2 lanjut ke struktur yang lebih baru.
        peak_val = setup.get('peak_val')
        if peak_val is not None:
            touch_mask_peak = (df_m5['high'] >= peak_val) if stype == 'Long' else (df_m5['low'] <= peak_val)
            after_touch_peak = touch_mask_peak.iloc[touched_idx + 1:closed_end]
            if after_touch_peak.any():
                return 'keep'   # basi — nunggu swing_val/peak_val ke-update oleh re-deteksi H1 normal
        setup['m5_scan_from_ts'] = float(df_m5['ts'].iloc[touched_idx])
        setup['phase'] = 'WAIT_M5_CHOCH'
        choch_h1 = setup.get('choch_level'); pk_h1 = setup.get('peak_val')
        log_entry(f"🧱 {coin} {stype} (struct) BOS H1 → break:{setup.get('swing_val'):.6g} "
                  f"CHOCH:{choch_h1:.6g} puncak:{pk_h1:.6g} IDM:{idm_level:.6g} "
                  f"— mulai dimonitor M5")
        log_entry(f"👁️  {coin} {stype} (struct): IDM {idm_level:.6g} tersentuh M5 @ "
                  f"{_ts_wib(df_m5['ts'].iloc[touched_idx])} → mulai cari BOS M5 arah {opp}")
        return 'keep'

    # ── WAIT_M5_CHOCH: mekanisme ROLLING GENERIK — satu aturan yang sama diterapkan berulang.
    #    State: break_lvl (level yang jadi acuan sekarang), ujung_lvl (retrace lawan arah break_lvl),
    #    terjauh_lvl (titik terakhir break_lvl dikonfirmasi/diperbaharui), dan break_dir (arah struktur
    #    break_lvl SAAT INI: 'opp' kalau levelnya BOS/searah m5 yg dicari, 'stype' kalau levelnya CHoCH
    #    /searah H1 — arah ini TERTUKAR setiap kali geser).
    #    ATURAN TUNGGAL: setiap kali break_lvl tertembus WICK (searah break_dir) -> GESER:
    #      break_lvl_baru = ujung_lvl_lama, ujung_lvl_baru = terjauh_lvl_lama,
    #      terjauh_lvl_baru = titik-tembus-sekarang, break_dir dibalik (opp<->stype).
    #    KHUSUS kalau break_dir yang BARU (setelah geser) == stype (berarti level ini levelnya CHoCH,
    #    lawan dari BOS-m5 yang dicari) -> selain wick, HARUS dicek CLOSE BODY juga menembus level
    #    break_lvl (baru) itu untuk valid jadi entry (cari RBS/SBR). Kalau cuma wick (belum close) ->
    #    tunggu; kalau nanti malah wick balik lagi ke ujung_lvl (structur lanjut trend H1) -> itu event
    #    "break_lvl tertembus lagi" biasa -> geser lagi (aturan tunggal berlaku lagi, rekursif).
    #    IDM (n=1..20 fleksibel) HANYA dipakai SEKALI di awal (utk menentukan break_lvl PERTAMA, sejak
    #    IDM-H1 tersentuh) — sesudah itu TIDAK PERNAH dicari ulang; struktur murni geser mengikuti
    #    price action wick demi wick.
    if setup['phase'] == 'WAIT_M5_CHOCH':
        scan_ts = setup['m5_scan_from_ts']

        # ── Fase awal: break_lvl PERTAMA belum ada -> cari IDM-m5 (n=1..20), tunggu tersentuh ──
        if setup.get('m5_break_lvl') is None:
            idxs = df_m5.index[(df_m5['ts'] >= scan_ts) & (df_m5.index < closed_end)]
            if len(idxs) < 1:
                return 'keep'
            start_i = int(idxs[0])
            df_win = df_m5.iloc[start_i:closed_end].reset_index(drop=True)
            if len(df_win) < 2:
                return 'keep'
            idm_legs_m5 = _build_idm_legs_flex(df_win, opp, n_range=range(1, 21))
            if not idm_legs_m5:
                return 'keep'
            trig_val, trig_idx, ext_val, ext_idx = idm_legs_m5[-1]   # leg TERBARU yang dipakai (reset
                                                                       # otomatis tiap leg baru terbentuk,
                                                                       # krn _build_idm_legs_flex re-scan
                                                                       # dari awal tiap siklus)
            # IDM sendiri adalah mini-struktur (break=ext_val, ujung=trig_val) — 'terjauh' di-track
            # SEJAK leg IDM ini terbentuk (candle setelah trig_idx) sampai trig_val (IDM) tersentuh,
            # RESET tiap kali leg IDM berganti (karena idm_legs_m5[-1] otomatis ikut leg TERBARU).
            df_since_leg = df_win.iloc[trig_idx + 1:].reset_index(drop=True)
            if len(df_since_leg) < 1:
                return 'keep'   # leg baru saja terbentuk, belum ada candle susulan — tunggu
            touch_mask_idm = (df_since_leg['low'] <= trig_val) if opp == 'Long' else (df_since_leg['high'] >= trig_val)
            idm_touch_i = int(df_since_leg.index[touch_mask_idm][0]) if touch_mask_idm.any() else None
            seg_for_terjauh = df_since_leg.iloc[0:idm_touch_i + 1] if idm_touch_i is not None else df_since_leg
            terjauh_now = float(seg_for_terjauh['low'].min() if opp == 'Short' else seg_for_terjauh['high'].max())
            if idm_touch_i is None:
                setup['m5_idm'] = trig_val   # simpan kandidat terkini utk dashboard/log
                return 'keep'   # IDM belum tersentuh — 'terjauh' terus melebar, tunggu siklus berikutnya
            idm_touch_ts = float(df_since_leg['ts'].iloc[idm_touch_i])
            setup['m5_break_lvl'] = terjauh_now      # break awal utk fase rolling = TERJAUH (bukan ext_val)
            setup['m5_ujung_lvl'] = trig_val          # ujung mulai dari level IDM itu sendiri
            setup['m5_terjauh_lvl'] = terjauh_now
            setup['m5_break_dir'] = opp
            setup['m5_break_since_ts'] = idm_touch_ts
            setup['m5_idm'] = trig_val
            log_entry(f"👁️  {coin} {stype} (struct): IDM-m5 {trig_val:.6g} tersentuh @ "
                      f"{_ts_wib(idm_touch_ts)} — break={terjauh_now:.6g} (terjauh sejak IDM terbentuk), "
                      f"mantau break tertembus lagi")
            return 'keep'

        # ── Struktur sudah ada -> jalankan ATURAN TUNGGAL (geser rekursif per siklus) ──
        break_lvl = setup['m5_break_lvl']; ujung_lvl = setup['m5_ujung_lvl']
        terjauh_lvl = setup['m5_terjauh_lvl']; break_dir = setup['m5_break_dir']
        since_ts = setup['m5_break_since_ts']

        idxs = df_m5.index[(df_m5['ts'] > since_ts) & (df_m5.index < closed_end)]
        if len(idxs) == 0:
            return 'keep'
        start_i = int(idxs[0])
        df_after = df_m5.iloc[start_i:closed_end].reset_index(drop=True)
        if len(df_after) < 1:
            return 'keep'

        # ujung_lvl terus melebar (retrace terjauh berlawanan break_dir) selama break_lvl blm tertembus
        touch_mask_break = (df_after['high'] >= break_lvl) if break_dir == 'Long' else (df_after['low'] <= break_lvl)
        break_touch_i = int(df_after.index[touch_mask_break][0]) if touch_mask_break.any() else None
        seg_for_ujung = df_after.iloc[0:break_touch_i] if break_touch_i is not None else df_after
        if len(seg_for_ujung) > 0:
            ujung_now = float(seg_for_ujung['high'].max() if break_dir == 'Short' else seg_for_ujung['low'].min())
            if (break_dir == 'Short' and ujung_now > ujung_lvl) or (break_dir == 'Long' and ujung_now < ujung_lvl):
                setup['m5_ujung_lvl'] = ujung_now; ujung_lvl = ujung_now

        # Kalau level SEKARANG adalah CHoCH (break_dir == stype) -> cek CLOSE BODY jg (bukan cuma wick)
        if break_dir == stype:
            brk_close_mask = (df_after['close'] > break_lvl) if stype == 'Long' else (df_after['close'] < break_lvl)
            close_i = int(df_after.index[brk_close_mask][0]) if brk_close_mask.any() else None
            # CHoCH valid HANYA kalau close tembus terjadi DI CANDLE YANG SAMA/SEBELUM wick tembus jg
            # (keduanya sama² "break_lvl tertembus" — close body adalah syarat TAMBAHAN, bukan event
            # terpisah: begitu close_i ketemu, break_lvl otomatis juga sudah wick-tertembus di i yg sama)
            if close_i is not None and (break_touch_i is None or close_i <= break_touch_i):
                df_rbs_seg = df_after.iloc[0:close_i + 1].reset_index(drop=True)
                rbs = _find_m5_rbs(df_rbs_seg, stype) if len(df_rbs_seg) >= 3 else None
                if rbs is not None:
                    active_count = len(active_positions) + _count_slots()
                    if active_count >= MAX_CONCURRENT:
                        log_entry(f"⏸️  {coin} {stype} (struct): RBS/SBR ketemu tapi slot penuh "
                                  f"({active_count}/{MAX_CONCURRENT}) — tunda")
                        return 'keep'
                    entry_p = (rbs['zone_hi'] + rbs['zone_lo']) / 2.0
                    sl_p = ujung_lvl
                    side = 'Buy' if stype == 'Long' else 'Sell'
                    rbs_label = 'RBS' if stype == 'Long' else 'SBR'
                    log_entry(f"🔀 {coin} {stype} (struct): CHoCH {break_lvl:.6g} pecah (close body) — "
                              f"{rbs_label} @ [{rbs['zone_lo']:.6g}-{rbs['zone_hi']:.6g}] "
                              f"break @ {_ts_wib(df_rbs_seg['ts'].iloc[rbs['break_idx']])} "
                              f"→ limit entry@{entry_p:.6g} SL@{sl_p:.6g}")
                    oid = place_limit_order(coin, side, entry_p, sl_p)
                    if oid:
                        setup['entry'] = entry_p; setup['sl'] = sl_p; setup['order_id'] = oid
                        setup['phase'] = 'WAIT_FILL'
                        return 'lock'
                    log_entry(f"⚠️ {coin} {stype} (struct): place_limit_order gagal — dicoba lagi siklus berikutnya")
                    return 'keep'
                else:
                    log_entry(f"❌ {coin} {stype} (struct): CHoCH {break_lvl:.6g} pecah (close body) tapi "
                              f"TIDAK ADA RBS/SBR — dianggap umpan. Menunggu struktur bergeser lagi.")
                    # TIDAK return — biarkan lanjut ke logika geser di bawah kalau break_touch_i jg ada
                    # (biasanya sama, krn close tembus menyiratkan wick jg tembus)

        # ── ATURAN TUNGGAL: break_lvl tertembus WICK -> GESER struktur ──
        if break_touch_i is None:
            return 'keep'   # break_lvl belum tertembus sama sekali — tunggu terus (ujung terus melebar)

        titik_tembus = float(df_after['low'].iloc[break_touch_i] if break_dir == 'Long'
                              else df_after['high'].iloc[break_touch_i])
        new_break_dir = 'Long' if break_dir == 'Short' else 'Short'
        new_break_lvl = ujung_lvl
        new_ujung_lvl = terjauh_lvl
        new_terjauh_lvl = titik_tembus
        new_since_ts = float(df_after['ts'].iloc[break_touch_i])
        label = "CHoCH" if new_break_dir == stype else "BOS-m5"
        log_entry(f"🔄 {coin} {stype} (struct): level {break_lvl:.6g} tertembus (wick) @ "
                  f"{_ts_wib(new_since_ts)} — geser struktur jadi {label}: break={new_break_lvl:.6g} "
                  f"ujung={new_ujung_lvl:.6g} terjauh={new_terjauh_lvl:.6g}")
        setup['m5_break_lvl'] = new_break_lvl; setup['m5_ujung_lvl'] = new_ujung_lvl
        setup['m5_terjauh_lvl'] = new_terjauh_lvl; setup['m5_break_dir'] = new_break_dir
        setup['m5_break_since_ts'] = new_since_ts
        # Field log/dashboard (mempertahankan makna lama utk kompatibilitas tampilan)
        setup['m5_swing_val'] = new_break_lvl; setup['m5_choch_level'] = new_ujung_lvl
        setup['m5_peak'] = new_terjauh_lvl
        return 'keep'

    # ── WAIT_FILL: limit sudah terpasang, tunggu fill ──
    if setup['phase'] == 'WAIT_FILL':
        oid = setup.get('order_id')
        if oid and not _order_was_filled(coin, oid):
            if _order_exists(coin, oid):
                return 'lock'
            log_entry(f"⚠️ {coin} {stype} (struct): order {oid} hilang & bukan Filled — setup dibuang.")
            return 'remove'
        pos = get_open_position(coin, 'Buy' if stype == 'Long' else 'Sell')
        if pos:
            entry_p = setup['entry']; sl_p = setup['sl']
            side_order = 'Buy' if stype == 'Long' else 'Sell'
            actual_entry = float(pos.get('avgPrice', entry_p))
            actual_dist = abs(actual_entry - sl_p)
            min_dist = actual_entry * 0.002
            if actual_dist < min_dist:
                actual_dist = min_dist
                sl_p = actual_entry - actual_dist if side_order == 'Buy' else actual_entry + actual_dist
            trail_d = TRAIL_STOP * actual_dist
            info = get_instrument_info(coin); tick = info.get('tick_size', 0.0001)
            sl_r = round_price(sl_p, tick); trail_r = round_price(trail_d, tick)
            # Trail trigger (activePrice) dipasang di PUNCAK BOS H1 (setup['peak_val']), bukan lagi
            # formula TRAIL_ACT_R x dist — jadi otomatis menyesuaikan lebar tiap setup (puncak BOS
            # bisa jauh/dekat tergantung besar impulsnya). Fallback ke formula lama HANYA kalau
            # puncak ternyata di sisi yang salah dari entry (mis. data aneh / entry sudah lewat puncak).
            peak_h1 = setup.get('peak_val')
            if peak_h1 is not None and (
                (side_order == 'Buy' and peak_h1 > actual_entry) or
                (side_order == 'Sell' and peak_h1 < actual_entry)
            ):
                active_p = round_price(peak_h1, tick)
            else:
                active_p = round_price(actual_entry + TRAIL_ACT_R * actual_dist if side_order == 'Buy'
                                        else actual_entry - TRAIL_ACT_R * actual_dist, tick)
            trail_set_ok = False
            for _attempt in range(3):
                try:
                    if USE_TP:
                        tp_r = round_price(actual_entry + RR_TP * actual_dist if side_order == 'Buy' else actual_entry - RR_TP * actual_dist, tick)
                        res_ts = session.set_trading_stop(category=CATEGORY, symbol=coin, stopLoss=str(sl_r), takeProfit=str(tp_r), positionIdx=_pidx(side_order))
                    else:
                        res_ts = session.set_trading_stop(category=CATEGORY, symbol=coin, stopLoss=str(sl_r), trailingStop=str(trail_r), activePrice=str(active_p), positionIdx=_pidx(side_order))
                    if res_ts.get('retCode', -1) == 0:
                        trail_set_ok = True
                        print(f"🛡️  {coin}: SL={sl_r} " + (f"TP={tp_r} (1:{RR_TP})" if USE_TP else f"Trail={trail_r} act={active_p}"))
                        break
                    else:
                        print(f"⚠️ {coin}: set_trading_stop gagal: {res_ts.get('retMsg','')}"); time.sleep(2)
                except Exception as e:
                    print(f"⚠️ {coin}: set_trading_stop error: {e}"); time.sleep(2)
            if not trail_set_ok:
                print(f"⚠️ {coin}: Trail gagal — retry M5 berikutnya")
            active_positions[_akey(coin, stype)] = {
                'coin': coin, 'side': side_order, 'entry': actual_entry, 'sl': sl_p, 'dist': actual_dist,
                'trail_dist': trail_d, 'trail_engaged': False, 'trail_set': trail_set_ok,
                'last_price': actual_entry, 'entry_time': time.time(),
                'peak': actual_entry, 'peak_time': time.time(),
                'swing_val': setup.get('swing_val'), 'bos_type': stype, 'rev_count': 0,
                'orig_ocl': setup.get('entry'),
                'choch_level': setup.get('m5_choch_level'), 'peak_val': setup.get('m5_peak'),
                'swing2': setup.get('m5_swing_val'), 'kind': 'struct',
            }
            log_entry(f"════ STRUCT FILLED {stype} {coin} ════\n"
                      f"  entry:{actual_entry:.6f} SL:{sl_p:.6f} | H1 break:{setup.get('swing_val'):.6g} "
                      f"IDM:{setup.get('idm_level'):.6g} | BOS-m5 {opp}:{setup.get('m5_swing_val'):.6g} "
                      f"CHoCH-m5:{setup.get('m5_choch_level'):.6g}")
            print(f"✅ {coin} {stype} (struct): Limit filled! Entry:{actual_entry:.6f} SL:{sl_p:.6f}")
            return 'fill'
        else:
            oid = setup.get('order_id')
            if oid and not _order_exists(coin, oid):
                if _order_was_filled(coin, oid):
                    print(f"📭 {coin} {stype} (struct): Limit filled lalu tutup (SL) — selesai.")
                else:
                    log_entry(f"📤 {coin} {stype} (struct): Limit hilang (cancel) — setup dibuang.")
                return 'remove'
        return 'lock'
    return 'keep'


def check_idm_pending():
    """Cek limit IDM (Fib) yg menunggu: terisi -> active_positions; kadaluarsa -> batalkan."""
    for key in list(idm_pending.keys()):
        p = idm_pending[key]
        coin, side = p['coin'], p['side']
        # BUG FIX: sama seperti jalur FVG (process_setup/WAIT_FILL) — get_open_position(coin, side)
        # bisa menemukan posisi lain yang sudah ada duluan di sisi yang sama (misal dari limit FVG
        # yang sudah fill duluan di coin+arah yang sama), padahal order limit IDM ini ('order_id')
        # sendiri belum fill (atau bahkan belum dipasang sama sekali — masih monitoring engulfing).
        # Wajib verifikasi order_id benar2 Filled dulu sebelum sentuh SL/posisi.
        oid = p.get('order_id')
        if oid is None:
            continue   # belum ada limit terpasang sama sekali (masih monitoring engulfing) — skip
        if not _order_was_filled(coin, oid):
            if not _order_exists(coin, oid):
                print(f"⚠️ {coin} IDM: order {oid} sudah tidak ada & bukan Filled — dibuang.")
                del idm_pending[key]
            continue   # order masih pending (atau baru dibuang) — jangan sentuh apapun
        pos = get_open_position(coin, side)
        if pos is not None and float(pos.get('size', 0) or 0) > 0:
            entry = float(pos.get('avgPrice') or p['entry'])
            dist = abs(entry - p['sl'])
            # pasang trailing/TP LANGSUNG saat fill (sama seperti jalur FVG, andal)
            info = get_instrument_info(coin); tick = info.get('tick_size', 0.0001)
            trail_d = TRAIL_STOP * dist
            sl_r = round_price(p['sl'], tick); trail_r = round_price(trail_d, tick)
            active_p = round_price(entry + TRAIL_ACT_R * dist if side == "Buy"
                                   else entry - TRAIL_ACT_R * dist, tick)
            trail_set_ok = False
            for _attempt in range(3):
                try:
                    if USE_TP:
                        tp_r = round_price(entry + RR_TP * dist if side == "Buy" else entry - RR_TP * dist, tick)
                        res_ts = session.set_trading_stop(category=CATEGORY, symbol=coin, stopLoss=str(sl_r),
                                                          takeProfit=str(tp_r), positionIdx=_pidx(side))
                    else:
                        res_ts = session.set_trading_stop(category=CATEGORY, symbol=coin, stopLoss=str(sl_r),
                                                          trailingStop=str(trail_r), activePrice=str(active_p),
                                                          positionIdx=_pidx(side))
                    if res_ts.get('retCode', -1) == 0:
                        trail_set_ok = True
                        print(f"🛡️  {coin} IDM: SL={sl_r} " + (f"TP={tp_r}" if USE_TP else f"Trail={trail_r} act={active_p}"))
                        break
                    else:
                        print(f"⚠️ {coin} IDM: set_trading_stop gagal: {res_ts.get('retMsg','')}"); time.sleep(2)
                except Exception as e:
                    print(f"⚠️ {coin} IDM: set_trading_stop error: {e}"); time.sleep(2)
            active_positions[key] = {
                'coin': coin, 'side': side, 'entry': entry, 'sl': p['sl'],
                'dist': dist,
                'trail_dist': trail_d, 'trail_engaged': False, 'trail_set': trail_set_ok,
                'last_price': entry, 'entry_time': time.time(),
                'peak': entry, 'peak_time': time.time(),
                'swing_val': p['swing_val'], 'bos_type': p['e_stype'], 'rev_count': 0,
                'orig_ocl': entry, 'choch_level': p['choch_level'], 'peak_val': p['peak_val'],
                'swing2': p['peak_val'], 'kind': 'inducement',
            }
            print(f"✅ {coin}: LIMIT IDM {p['e_stype']} TERISI @ {entry:.6g}")
            log_entry(f"════ FILL INDUCEMENT {p['e_stype']} {coin} @ {entry:.6g} (limit {IDM_LIMIT_FIB*100:.0f}% candle H1) ════")
            del idm_pending[key]
            continue
        trig = p.get('trigger'); rng = p.get('rng')

        # ── IDM WAIT_FILL: limit sudah terpasang — cancel jika CHOCH atau PUNCAK tersentuh M5 (wick) ──
        if IDM_M5_ENGULF and p.get('phase') == 'WAIT_FILL' and p.get('order_id'):
            # Cross-check: trigger FVG (searah BOS) untuk BOS besar sama baru tersentuh SELAGI
            # limit IDM ini masih WAIT_FILL (belum terisi) → cancel limit-nya juga.
            if _fvg_trigger_touched_for_bos(coin, p.get('swing_val'), p.get('choch_level')):
                cancel_order(coin, p['order_id'])
                print(f"🚫 {coin}: IDM {p['e_stype']} limit dibatalkan — trigger FVG (searah BOS) "
                      f"tersentuh duluan untuk BOS besar ini")
                log_entry(f"🚫 {coin}: IDM {p['e_stype']} limit dibatalkan — trigger FVG tersentuh duluan (BOS besar sama)")
                del idm_pending[key]
                continue
            if p.get('peak_val') or p.get('choch_level'):
                df_m5_c = get_data(coin, "5", limit=100)
                if df_m5_c is not None and 'ts' in df_m5_c.columns:
                    df_m5_c = df_m5_c[df_m5_c['ts'] >= p.get('placed_ts', 0) * 1000]
                bos_stype = "Short" if p['e_stype'] == "Long" else "Long"
                invalid, why = struct_touch_invalidated(df_m5_c, bos_stype, p.get('choch_level'), p.get('peak_val'))
                if invalid:
                    cancel_order(coin, p['order_id'])
                    print(f"🚫 {coin}: IDM {p['e_stype']} limit dibatalkan — {why}")
                    log_entry(f"🚫 {coin}: IDM {p['e_stype']} limit dibatalkan — {why}")
                    del idm_pending[key]
                    continue
            continue

        # ── IDM M5 ENGULF MODE ──
        if IDM_M5_ENGULF and p.get('order_id') is None and not p.get('m5_hangus'):
            # Cross-check: kalau FVG (searah BOS) untuk BOS besar SAMA PERSIS baru tersentuh
            # trigger-nya SELAGI IDM ini masih monitoring engulfing → batalkan monitoring IDM ini.
            # Arah ini sudah "diambil" FVG, jangan dobel risk di coin+arah yang sama.
            if _fvg_trigger_touched_for_bos(coin, p.get('swing_val'), p.get('choch_level')):
                print(f"🚫 {coin}: IDM {p['e_stype']} dibatalkan — trigger FVG (searah BOS) tersentuh "
                      f"duluan untuk BOS besar ini, arah sudah diambil FVG")
                log_entry(f"🚫 {coin}: IDM {p['e_stype']} dibatalkan — trigger FVG tersentuh duluan (BOS besar sama)")
                del idm_pending[key]
                continue
            # Cek CHOCH atau puncak BOS besar tersentuh M5 (wick) → hangus permanen
            if p.get('peak_val') or p.get('choch_level'):
                df_m5_pk = get_data(coin, "5", limit=100)
                if df_m5_pk is not None and 'ts' in df_m5_pk.columns:
                    df_m5_pk = df_m5_pk[df_m5_pk['ts'] >= p.get('placed_ts', 0) * 1000]
                bos_stype = "Short" if p['e_stype'] == "Long" else "Long"
                invalid, why = struct_touch_invalidated(df_m5_pk, bos_stype, p.get('choch_level'), p.get('peak_val'))
                if invalid:
                    print(f"🚫 {coin}: IDM {p['e_stype']} hangus — {why}")
                    log_entry(f"🚫 {coin}: IDM {p['e_stype']} hangus — {why}")
                    _bos_h = bos_stype
                    inducement_done[(coin, _bos_h)] = (
                        _bos_h,
                        round(p.get('choch_level'), 10) if p.get('choch_level') is not None else None,
                        round(p.get('swing_val'), 10) if p.get('swing_val') is not None else None,
                    )
                    save_state()
                    del idm_pending[key]
                    continue
            df_m5_idm = get_data(coin, "5", limit=100)
            if df_m5_idm is None:
                continue
            e_stype_idm = p['e_stype']
            bos_rng_idm = rng or 1.0
            # Log status IDM trigger
            if trig:
                _curr_idm = float(df_m5_idm.iloc[-1]['close']) if len(df_m5_idm) > 0 else 0
                _pct_idm  = abs(_curr_idm - trig) / trig * 100 if trig and _curr_idm else 0
                if p.get('m5_triggered'):
                    print(f"👁️  IDM {coin} [{e_stype_idm}] | now:{_curr_idm:.6g} trigger:{trig:.6g} | monitor engulfing M5...")
                else:
                    print(f"👁️  IDM {coin} [{e_stype_idm}] | now:{_curr_idm:.6g} trigger:{trig:.6g} | "
                          f"menunggu sweep ({_pct_idm:.2f}% lagi)")


            # Build m5_setup untuk check_m5_engulfing
            m5_setup = {
                'type': e_stype_idm,
                'orig_ocl': trig,
                'm5_c1c_touched': p.get('m5_triggered', False),
                'm5_focus_hi': p.get('m5_focus_hi', 0.0),
                'm5_focus_lo': p.get('m5_focus_lo', 0.0),
                'm5_focus_idx': p.get('m5_focus_idx', 0),
                'm5_focus_initialized': p.get('m5_focus_initialized', False),
                'peak_val': trig + bos_rng_idm,
                'choch_level': trig - bos_rng_idm,
                'created_ts': p.get('placed_ts', 0),
                'h1_ema_resolved': p.get('h1_ema_resolved', False),
                'h1_ema_dir': p.get('h1_ema_dir'),
                'h1_ema_trigger_ts': p.get('h1_ema_trigger_ts'),
                'h1_decision_ts_close': p.get('h1_decision_ts_close'),
                'bos_swing_val': p.get('swing_val'),      # BOS besar ASLI (bukan trig±bos_rng) — utk cross-check
                'bos_choch_level': p.get('choch_level'),  # BOS besar ASLI — utk cross-check
                'is_idm': True,
            }
            # H1 cuma perlu di-fetch kalau gate EMA20 H1 belum selesai (hemat API call)
            df_h1_idm = get_data(coin, "60", limit=50) if not m5_setup['h1_ema_resolved'] else None
            engulf = check_m5_engulfing(coin, m5_setup, df_m5_idm, bos_rng_idm, df_h1=df_h1_idm)
            # Simpan state kembali ke idm_pending
            p['m5_triggered']         = m5_setup['m5_c1c_touched']
            p['m5_focus_hi']          = m5_setup['m5_focus_hi']
            p['m5_focus_lo']          = m5_setup['m5_focus_lo']
            p['m5_focus_idx']         = m5_setup['m5_focus_idx']
            p['m5_focus_initialized'] = m5_setup['m5_focus_initialized']
            p['h1_ema_resolved']      = m5_setup['h1_ema_resolved']
            p['h1_ema_dir']           = m5_setup['h1_ema_dir']
            p['h1_ema_trigger_ts']    = m5_setup['h1_ema_trigger_ts']
            p['h1_decision_ts_close'] = m5_setup['h1_decision_ts_close']
            if engulf and engulf.get('cancelled'):
                pass   # cross-check hanya berlaku utk FVG (is_idm=True di sini) — seharusnya tak pernah terjadi
            elif engulf:
                # Pasang LIMIT ORDER di ujung candle fokus M5 (sama seperti FVG)
                limit_entry = engulf['entry']
                limit_sl    = engulf['sl']
                final_dir   = p.get('h1_ema_dir') or e_stype_idm
                side_final  = engulf['side']   # 'Buy'/'Sell' — SUDAH pakai arah hasil gate EMA20 H1
                oid = place_limit_order(coin, side_final, limit_entry, limit_sl)
                if oid:
                    p['order_id'] = oid
                    p['entry']    = limit_entry
                    p['sl']       = limit_sl
                    p['phase']    = 'WAIT_FILL'   # tandai sudah punya limit
                    p['e_stype']  = final_dir     # arah final (bisa dibalik "mode EMA") dipakai seterusnya
                    p['side']     = side_final    # dipakai check fill posisi (get_open_position) di atas
                    _mode_lbl = "" if final_dir == e_stype_idm else " [MODE EMA — arah dibalik]"
                    print(f"\U0001f4cd {coin}: IDM M5 ENGULF {final_dir} → LIMIT @ {limit_entry:.6g} "
                          f"SL:{limit_sl:.6g}{_mode_lbl}")
                    _eo = engulf.get('engulf_ohlc', {})
                    log_entry(
                        f"════ IDM M5 ENGULF LIMIT {final_dir} {coin} @ {limit_entry:.6g}{_mode_lbl} ════\n"
                        f"  Candle ENGULFING: open={_eo.get('open',0):.6g} high={_eo.get('high',0):.6g} "
                        f"low={_eo.get('low',0):.6g} close={_eo.get('close',0):.6g} "
                        f"→ entry = {limit_entry:.6g} (high/low candle sebelum engulfing)\n"
                        f"  SL: entry={limit_entry:.6g} fixed ± buffer({SL_ENGULF_PCT*100:.0f}% range BOS)="
                        f"{engulf.get('sl_buffer',0):.6g} → SL final={limit_sl:.6g}"
                    )
            continue   # M5 engulf path selesai, lanjut ke key berikutnya

        # ── INVALIDASI PERGERAKAN (mode limit lama) ──
        if trig is not None and rng and not IDM_M5_ENGULF:
            thr = trig - IDM_CANCEL_MOVE_PCT * rng if p['e_stype'] == "Short" else trig + IDM_CANCEL_MOVE_PCT * rng
            df_m5 = get_data(coin, "5", limit=30)
            if df_m5 is not None and len(df_m5) > 0:
                seg = df_m5[df_m5['ts'] >= p['placed_ts'] * 1000]
                if len(seg) > 0:
                    moved = (float(seg['low'].min()) <= thr) if p['e_stype'] == "Short" \
                            else (float(seg['high'].max()) >= thr)
                    if moved:
                        if p.get('order_id'):
                            cancel_order(coin, p['order_id'])
                        print(f"🚫 {coin}: LIMIT IDM {p['e_stype']} batal — harga bergerak "
                              f">{IDM_CANCEL_MOVE_PCT*100:.0f}% range dari trigger {trig:.6g} (lewat {thr:.6g}).")
                        del idm_pending[key]


def run_bot():
    global bot_start_ts
    bot_start_ts = time.time()   # timestamp saat bot mulai jalan
    load_state()   # muat inducement_done dari file (bertahan lewat redeploy/restart)
    print("SMC INTI BOT — " + ("BOS H1 -> IDM touch -> BOS M5 lawan arah -> CHoCH balik -> entry 50%/SL 100% (STRUCT_MODE)"
          if STRUCT_MODE else "BOS H1 -> FVG -> Limit @ C1.close -> TP 1:2"))
    print(f"CONFIG v9.22 | swing {SWING_BARS}-{SWING_BARS}/sub {SUBLEG_BARS}-{SUBLEG_BARS} | FVG biasa (warna bebas) | "
          f"zona C1 {ENTRY_ZONE_LO*100:.1f}%-{ENTRY_ZONE_HI*100:.0f}%{'(dinamis)' if ZONE_FROM_RETRACE else ''} | "
          f"gap {('<=%.2f%%' % (MAX_GAP_PCT*100)) if MAX_GAP_PCT > 0 else 'bebas'} | "
          f"SL {('FIXED %.0f%% range' % (SL_CAP_RANGE*100)) if SL_FIXED_RANGE else (('C1, cap %.0f%% range' % (SL_CAP_RANGE*100)) if SL_CAP_RANGE > 0 else 'C1')} | "
          f"monitor 2-arah | fresh-C1 {'ON' if REQUIRE_FRESH_C1 else 'off'} | "
          f"FVG butuh IDM {'ON' if REQUIRE_IDM_FOR_FVG else 'off'} | "
          f"risk {RISK_PCT*100:.0f}%/trade | lev {LEVERAGE}x | "
          f"TP {'1:'+str(RR_TP) if USE_TP else 'trailing'} | "
          f"HEDGE {'ON (IDM+FVG barengan)' if ALLOW_HEDGE else 'off (one-way)'} | "
          f"induce {('ON %s rantai-mini-BOS %.0f-%.0f%% [%s]' % (INDUCEMENT_TF, INDUCEMENT_ZONE_LO*100, INDUCEMENT_ZONE_HI*100, ('LIMIT Fib%.1f%%' % (IDM_LIMIT_FIB*100)) if IDM_LIMIT_ENTRY else 'MARKET')) if INDUCEMENT_ENTRY else 'off'} | bump order >=${ORDER_BUMP_FLOOR:.0f}")
    if not test_connection():
        print("⛔ Tidak bisa konek ke Bybit.")
        return
    if ALLOW_HEDGE:
        try:
            r = session.switch_position_mode(category=CATEGORY, coin="USDT", mode=3)
            rc = r.get('retCode', -1)
            if rc == 0:
                print("🔀 Hedge mode AKTIF (switch_position_mode mode=3, semua USDT-perp).")
            elif rc == 110025:
                print("🔀 Hedge mode sudah aktif (tak berubah).")
            else:
                print(f"⚠️ switch_position_mode: {r.get('retMsg','')} (code:{rc}) — "
                      f"set Hedge Mode manual di app & TUTUP semua posisi dulu kalau perlu.")
        except Exception as e:
            print(f"⚠️ switch_position_mode error: {e} — set Hedge Mode manual di app dulu.")

    while True:
        now = time.time()
        sec = now % 300
        wait_sec = 300 - sec + 2
        if wait_sec > 300:
            wait_sec = 2
        print(f"⏱️  Tunggu candle M5 close: {wait_sec:.0f} detik...")
        time.sleep(wait_sec)

        for _k in list(active_positions.keys()):
            try:
                check_trailing_sl(_k)
            except Exception as e:
                print(f"⚠️ Trailing SL {coin}: {e}")

        try:
            check_idm_pending()
        except Exception as e:
            print(f"⚠️ IDM pending: {e}")

        n_active   = len(active_positions)
        n_waitfill = _count_slots()
        n_approach = sum(1 for d in pending.values() for s in d.values() if s.get('phase') == 'WAIT_APPROACH')
        n_struct_wait_idm = sum(1 for dirs in struct_pending.values() for s in dirs.values()
                                 if s.get('phase') == 'WAIT_IDM_TOUCH')
        n_struct_watch = sum(1 for dirs in struct_pending.values() for s in dirs.values()
                              if s.get('phase') == 'WAIT_M5_CHOCH')
        n_struct_waitfill = sum(1 for dirs in struct_pending.values() for s in dirs.values()
                                 if s.get('phase') == 'WAIT_FILL')
        slots_used = n_active + n_waitfill + n_struct_waitfill
        print(f"\n{'='*55}")
        print(f"📊 SLOT: {slots_used}/{MAX_CONCURRENT} terpakai (posisi:{n_active} | limit:{n_waitfill} | "
              f"watch:{n_approach} | struct-wait-idm:{n_struct_wait_idm} | struct-watch-m5:{n_struct_watch} | "
              f"struct-limit:{n_struct_waitfill})")
        if EXPERIMENTAL_MODE and EXPERIMENTAL_H1_BIAS_FILTER and h1_bias_state:
            bias_str = ", ".join(f"{c}:{st.get('bias') or '—'}" for c, st in h1_bias_state.items())
            print(f"🧭 BIAS H1: {bias_str}")
        if active_positions:
            for c, p in active_positions.items():
                c = p.get('coin', c)
                bk = p.get('swing_val'); pk = p.get('peak_val'); ch = p.get('choch_level')
                bk = f"{bk:.6g}" if bk else "—"; pk = f"{pk:.6g}" if pk else "—"; ch = f"{ch:.6g}" if ch else "—"
                print(f"   POSISI {c} {p.get('bos_type','?')} @ {p.get('entry',0):.6g} SL:{p.get('sl',0):.6g} | "
                      f"break:{bk} puncak:{pk} CHOCH:{ch}")
        if pending:
            for c, dirs in pending.items():
                for d, st in dirs.items():
                    bk = st.get('swing_val'); pk = st.get('peak_val'); ch = st.get('choch_level')
                    bk = f"{bk:.6g}" if bk else "—"; pk = f"{pk:.6g}" if pk else "—"; ch = f"{ch:.6g}" if ch else "—"
                    print(f"   {c} [{d}]: {st.get('phase','?')} @ {st.get('entry',0):.6g} | "
                          f"break:{bk} puncak:{pk} CHOCH:{ch}")
        if struct_pending:
            for c, dirs in struct_pending.items():
                for d, st in dirs.items():
                    bk = st.get('swing_val'); idm = st.get('idm_level')
                    ch = st.get('choch_level'); pk = st.get('peak_val')
                    mbrk = st.get('m5_break_lvl'); muj = st.get('m5_ujung_lvl'); mtj = st.get('m5_terjauh_lvl')
                    mid = st.get('m5_idm'); mdir = st.get('m5_break_dir')
                    bk  = f"{bk:.6g}" if bk else "—"; idm = f"{idm:.6g}" if idm else "—"
                    ch  = f"{ch:.6g}" if ch else "—"; pk  = f"{pk:.6g}" if pk else "—"
                    mbrk = f"{mbrk:.6g}" if mbrk else "—"; muj = f"{muj:.6g}" if muj else "—"
                    mtj = f"{mtj:.6g}" if mtj else "—"; mid = f"{mid:.6g}" if mid else "—"
                    ent = st.get('entry')
                    ent = f"{ent:.6g}" if ent else "—"
                    watch_tag = " [WATCH-M5]" if st.get('phase') == 'WAIT_M5_CHOCH' else ""
                    if st.get('phase') == 'WAIT_M5_CHOCH':
                        if mdir is None:
                            sub_status = f"cari IDM-m5 (kandidat:{mid})"
                        else:
                            label = "CHoCH" if mdir == d else "BOS-m5"
                            sub_status = f"{label} break={mbrk} ujung={muj} terjauh={mtj}"
                        print(f"   {c} [{d}] (struct){watch_tag}: {sub_status} @ {ent} | "
                              f"H1 break:{bk} CHOCH:{ch} puncak:{pk} IDM:{idm}")
                    else:
                        print(f"   {c} [{d}] (struct): {st.get('phase','?')} @ {ent} | "
                              f"H1 break:{bk} CHOCH:{ch} puncak:{pk} IDM:{idm}")
        if idm_pending:
            for k, p in idm_pending.items():
                trig_p = p.get('trigger', 0); e_st = p.get('e_stype','?')
                bk = p.get('swing_val'); pk = p.get('peak_val'); ch = p.get('choch_level')
                bk = f"{bk:.6g}" if bk else "—"; pk = f"{pk:.6g}" if pk else "—"; ch = f"{ch:.6g}" if ch else "—"
                phase_lbl = "WAIT_FILL" if p.get('order_id') else "WAIT_TRIGGER"
                print(f"   IDM {p.get('coin','?')} [{e_st}]: {phase_lbl} trigger={trig_p:.6g} | "
                      f"break:{bk} puncak:{pk} CHOCH:{ch}")
        print(f"{'='*55}")

        for coin in SYMBOLS:
            try:
                time.sleep(3)

                # ── MODE STRUKTURAL: BOS H1 -> IDM touch -> BOS M5 lawan arah -> CHoCH balik -> entry ──
                # Independen total dari jalur EMA/FVG/IDM lama di bawah — tidak menyentuh pending/idm_pending.
                if STRUCT_MODE:
                    if FUNDING_FILTER and in_funding_window():
                        cancel_unfavorable_limits(coin)
                    if (not ALLOW_HEDGE) and coin in active_positions:
                        continue
                    df_h1_live = get_data(coin, "60", limit=100)
                    if df_h1_live is None:
                        continue
                    sh_h1, sl_h1 = find_last_swing_bos(df_h1_live)
                    df_m5_live = get_data(coin, "5", limit=300)

                    if coin in struct_pending:
                        dirs = struct_pending[coin]
                        filled = False
                        for d in list(dirs.keys()):
                            action = process_struct_setup(coin, dirs[d], df_m5_live)
                            if action == 'remove':
                                if dirs[d].get('order_id'):
                                    cancel_order(coin, dirs[d]['order_id'])
                                del dirs[d]
                            elif action == 'fill':
                                filled = True
                                for d2 in list(dirs.keys()):
                                    if d2 != d and dirs[d2].get('order_id'):
                                        cancel_order(coin, dirs[d2]['order_id'])
                                        print(f"🚫 {coin} {d2} (struct): order lawan dibatalkan (arah {d} terisi).")
                                break
                        if filled:
                            struct_pending.pop(coin, None)
                            continue
                        # Re-deteksi BOS H1 dua arah — HANYA kalau setup masih WAIT_IDM_TOUCH (belum
                        # mulai monitoring M5 sama sekali). Sekali masuk WAIT_M5_CHOCH atau WAIT_FILL,
                        # JANGAN diganggu re-deteksi biasa — kalau tidak, peak_val/swing_val terus
                        # "menyesuaikan" tren yang jalan (self-fulfilling: puncak baru selalu baru saja
                        # kesentuh), bikin setup di-reset & IDM ke-touch ulang terus tiap siklus tanpa
                        # pernah sempat memantau M5 dgn benar. Satu-satunya cara keluar dari WAIT_M5_CHOCH
                        # adalah lewat process_struct_setup sendiri (CHoCH valid, puncak beneran
                        # kesentuh SETELAH idm, atau limit fill/hilang).
                        for d in ('Long', 'Short'):
                            if ALLOW_HEDGE and _akey(coin, d) in active_positions:
                                continue
                            cur = dirs.get(d)
                            if cur is not None and cur.get('phase') in ('WAIT_M5_CHOCH', 'WAIT_FILL'):
                                continue
                            cand, cand_log = build_h1_struct_setup(coin, df_h1_live, sh_h1, sl_h1, verbose=False, force_dir=d)
                            if not cand:
                                continue
                            if cur is None:
                                print(f"➕ {coin} {d} (struct): BOS H1 {d} terdeteksi — tambah pantauan")
                                print(cand_log); dirs[d] = cand
                            elif cand['swing_val'] != cur.get('swing_val'):
                                print(f"🔁 {coin} {d} (struct): BOS H1 lebih baru — ganti (break {cur.get('swing_val')} → {cand['swing_val']:.6g})")
                                print(cand_log); dirs[d] = cand
                        if not dirs:
                            struct_pending.pop(coin, None)
                        continue

                    dirs_new = {}
                    for d in ('Long', 'Short'):
                        if ALLOW_HEDGE and _akey(coin, d) in active_positions:
                            continue
                        cand, cand_log = build_h1_struct_setup(coin, df_h1_live, sh_h1, sl_h1, verbose=False, force_dir=d)
                        if cand:
                            print(cand_log); dirs_new[d] = cand
                    if dirs_new:
                        struct_pending[coin] = dirs_new
                    continue

                # ── MODE EKSPERIMENTAL: independen total dari jalur IDM/FVG di bawah ──
                if EXPERIMENTAL_MODE:
                    # Bias H1 (EMA3/EMA20) selalu di-update, terlepas dari status posisi/hedge —
                    # supaya cross H1 tidak pernah terlewat walau slot lagi penuh.
                    if EXPERIMENTAL_H1_BIAS_FILTER:
                        df_h1_exp = get_data(coin, "60", limit=100)
                        update_h1_bias(coin, df_h1_exp)
                    if ALLOW_HEDGE or coin not in active_positions:
                        df_m5_exp = get_data(coin, "5", limit=300)
                        check_experimental_engulf(coin, df_m5_exp)
                    continue

                # Funding window: batalkan limit yg gak searah sebelum settlement
                if FUNDING_FILTER and in_funding_window():
                    cancel_unfavorable_limits(coin)
                if (not ALLOW_HEDGE) and coin in active_positions:
                    continue
                df_h1_live = get_data(coin, "60", limit=100)
                if df_h1_live is None:
                    continue
                sh_h1, sl_h1 = find_last_swing_bos(df_h1_live)
                closed_h1 = df_h1_live.iloc[-2]
                curr_h1   = df_h1_live.iloc[-1]
                # Fetch M5 hanya jika ada setup C1 close yang sedang monitor engulfing
                _need_m5 = M5_ENGULF_FILTER and coin in pending
                df_m5_live = get_data(coin, "5", limit=300) if _need_m5 else None

                # ── INDUCEMENT ENTRY: selalu dicek, terlepas dari pending FVG ──
                if INDUCEMENT_ENTRY:
                    check_inducement_entry(coin, df_h1_live, sh_h1, sl_h1)

                # ── PROSES SETUP PENDING (per arah) ──────────────────
                if coin in pending:
                    dirs = pending[coin]
                    filled = False
                    for d in list(dirs.keys()):
                        action = process_setup(coin, dirs[d], df_h1_live, curr_h1, df_m5=df_m5_live)
                        if action == 'remove':
                            if dirs[d].get('order_id'):
                                cancel_order(coin, dirs[d]['order_id'])
                            del dirs[d]
                        elif action == 'fill':
                            filled = True
                            for d2 in list(dirs.keys()):
                                if d2 != d and dirs[d2].get('order_id'):
                                    cancel_order(coin, dirs[d2]['order_id'])
                                    print(f"🚫 {coin} {d2}: order lawan dibatalkan (arah {d} terisi).")
                            break
                    if filled:
                        pending.pop(coin, None)
                        continue
                    # Re-deteksi DUA ARAH: tambah/ganti di arah yg masih WAIT_APPROACH atau belum ada.
                    # WAIT_FILL TIDAK lagi di-skip total — tetap dicek BOS baru, dan limit yg sudah
                    # terpasang dibatalkan HANYA kalau BOS-nya memang sudah berganti (swing_val beda),
                    # bukan lagi karena harga lari jauh (>2R) dari entry.
                    for d in ('Long', 'Short'):
                        if ALLOW_HEDGE and _akey(coin, d) in active_positions:
                            continue   # hedge: arah ini posisinya sudah terbuka -> jangan pasang limit lagi
                        cur = dirs.get(d)
                        cand, cand_log = build_setup_from_bos(coin, df_h1_live, sh_h1, sl_h1, closed_h1, verbose=False, force_dir=d)
                        if not cand:
                            continue
                        if cur is None:
                            print(f"➕ {coin} {d}: BOS {d} terdeteksi — tambah pantauan")
                            print(cand_log); dirs[d] = cand
                        elif cand['swing_val'] != cur.get('swing_val'):
                            if cur.get('phase') == 'WAIT_FILL' and cur.get('order_id'):
                                cancel_order(coin, cur['order_id'])
                                print(f"🚫 {coin} {d}: limit dibatalkan — BOS baru terdeteksi di H1 "
                                      f"(break {cur.get('swing_val')} → {cand['swing_val']:.6g})")
                            else:
                                print(f"🔁 {coin} {d}: BOS lebih baru — ganti (break {cur.get('swing_val')} → {cand['swing_val']:.6g})")
                            print(cand_log); dirs[d] = cand
                        elif cur.get('phase') != 'WAIT_FILL':
                            # arah & swing sama, belum terkunci -> segarkan swing2/FVG (tanpa log)
                            cur['swing2'] = cand.get('swing2'); cur['peak_val'] = cand.get('peak_val')
                            cur['choch_level'] = cand.get('choch_level'); cur['fvg_list'] = cand.get('fvg_list', cur.get('fvg_list'))
                        # kalau WAIT_FILL dan swing_val SAMA (BOS belum berganti) -> biarkan, jangan diutak-atik
                    if not dirs:
                        pending.pop(coin, None)
                    continue

                # ── SCAN SETUP BARU: deteksi DUA ARAH sekaligus ──
                dirs_new = {}
                for d in ('Long', 'Short'):
                    if ALLOW_HEDGE and _akey(coin, d) in active_positions:
                        continue   # hedge: arah ini posisinya sudah terbuka
                    cand, cand_log = build_setup_from_bos(coin, df_h1_live, sh_h1, sl_h1, closed_h1, verbose=False, force_dir=d)
                    if cand:
                        print(cand_log); dirs_new[d] = cand
                if dirs_new:
                    pending[coin] = dirs_new
                else:
                    # diagnostik DUA ARAH: kenapa tak ada setup (BOS? FVG? stale? invalid?)
                    build_setup_from_bos(coin, df_h1_live, sh_h1, sl_h1, closed_h1, verbose=True, force_dir='Long')
                    build_setup_from_bos(coin, df_h1_live, sh_h1, sl_h1, closed_h1, verbose=True, force_dir='Short')

            except Exception as e:
                print(f"⚠️ Error {coin}: {e}"); continue


if __name__ == "__main__":
    run_bot()
