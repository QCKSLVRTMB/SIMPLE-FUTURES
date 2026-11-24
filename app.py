# app.py — Калькулятор фьючерсов MOEX · монитор уровней и зон A/B · с учётом комиссий · Захват ликвидности
from __future__ import annotations

import math
import re
import time
import urllib3
from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter, Retry

# ─────────────────────────────────────────────────────────────
# КОНСТАНТЫ
# ─────────────────────────────────────────────────────────────
ISS = "https://iss.moex.com/iss/engines/futures/markets/forts"
SHEET_ID = "1cHQnG1M7gQI5BpL9W0ijWgK-AI38NleQW_DOxccLDm0"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
COMMISSIONS_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq"
    f"?tqx=out:csv&sheet={quote('Комиссии')}"
)

MARGIN_LEVELS = ["НАЧ. ГО", "КНУР", "КСУР", "КПУР", "КОУР"]
MARGIN_HINTS = {
    "НАЧ. ГО": "НАЧ. ГО — базовое",
    "КНУР": "КНУР — начальный (LRC)",
    "КСУР": "КСУР — стандартный (MRC)",
    "КПУР": "КПУР — повышенный (HRC)",
    "КОУР": "КОУР — особый (ERC)",
}
IMS_MARGIN_MAP = {
    "НАЧ. ГО": ("IM_BUY", "IM_SELL"),
    "КНУР": ("IM_BUY_LRC", "IM_SELL_LRC"),
    "КСУР": ("IM_BUY_MRC", "IM_SELL_MRC"),
    "КПУР": ("IM_BUY_HRC", "IM_SELL_HRC"),
    "КОУР": ("IM_BUY_ERC", "IM_SELL_ERC"),
}

TRADE_STATE_KEYS = [
    "sl_input", "tp_input",
    "entry_p_0", "entry_q_0",
    "entry_p_1", "entry_q_1",
    "entry_p_2", "entry_q_2",
    "point_a_input", "point_b_input",
    "zl_input", "use_zl_mode",
]

DIR_EMOJI = {"LONG": "🟩", "SHORT": "🟥"}
ZONE_EMOJI = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣"}
BELL = "🔔"

ZONE_LABEL = {
    1: ("Зона 1", "оптимальная"),
    2: ("Зона 2", "повышенный риск"),
    3: ("Зона 3", "высокий риск"),
}

MOVE_CLASSES = {
    "Зигзаг":    "move-zigzag",
    "Тенденция": "move-trend",
    "Волна":     "move-wave",
}

MOVE_RANK = {"Зигзаг": 0, "Тенденция": 1, "Волна": 2}

DEFAULT_COMMISSION_PCT = 0.05
DEFAULT_MIN_COMMISSION = 2.0

# Прогрессия усреднения: каждая следующая точка = сумма предыдущих × 2
# 1 точка: [1]
# 2 точки: [1, 2]
# 3 точки: [1, 2, 6]
VOLUME_RATIO = {1: [1], 2: [1, 2], 3: [1, 2, 6]}


# ─────────────────────────────────────────────────────────────
# ЧИСЛА И ФОРМАТЫ
# ─────────────────────────────────────────────────────────────
def parse_number(val) -> float | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in {"nan", "none", "—", "-"}:
        return None
    s = re.sub(r"[^\d,.\-+]", "", s)
    if not s or s in {"-", "+", ".", ","}:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def normalize_move(val) -> str | None:
    if val is None:
        return None
    s = str(val).strip().lower()
    if not s or s in {"nan", "none", "—", "-", "нет"}:
        return None
    if "зигзаг" in s or "zigzag" in s:
        return "Зигзаг"
    if "тенденц" in s or "trend" in s:
        return "Тенденция"
    if "волн" in s or "wave" in s:
        return "Волна"
    return None


def fmt_px(v, decimals: int = 2) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    d = max(0, min(int(decimals), 8))
    return f"{v:.{d}f}".replace(".", ",")


def fmt_n(v, decimals: int = 0) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    d = max(0, min(int(decimals), 6))
    s = f"{v:.{d}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",")


def fmt_date(s) -> str:
    try:
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return str(s) if s else "—"


def fmt_clock(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def move_tag_html(move: str | None) -> str:
    if not move or move not in MOVE_CLASSES:
        return ""
    cls = MOVE_CLASSES[move]
    return f'<span class="move-tag {cls}">{move}</span>'


# ─────────────────────────────────────────────────────────────
# ЗОНЫ A/B И УРОВНИ
# ─────────────────────────────────────────────────────────────
def zone_bands(a: float, b: float) -> tuple[float, float]:
    p1 = a + (b - a) / 3.0
    p2 = a + 2.0 * (b - a) / 3.0
    return p1, p2


def determine_zone(price, a, b) -> tuple[int | None, dict | None]:
    if price is None or a is None or b is None or a == b:
        return None, None
    p1, p2 = zone_bands(a, b)
    lo, hi = min(a, b), max(a, b)
    info = {"a": a, "b": b, "p1": p1, "p2": p2, "lo": lo, "hi": hi,
            "dir": "LONG" if a < b else "SHORT"}
    if price < lo or price > hi:
        info["outside"] = "below" if price < lo else "above"
        return None, info
    info["outside"] = None
    if a < b:
        z = 1 if price <= p1 else (2 if price <= p2 else 3)
    else:
        z = 1 if price >= p1 else (2 if price >= p2 else 3)
    return z, info


def level_state(price, setup: dict) -> str:
    lvl, a, d = setup.get("level"), setup.get("a"), setup.get("dir")
    if price is None or lvl is None or a is None or d is None:
        return "none"
    if d == "LONG":
        if price < a:
            return "expired"
        return "reached" if price <= lvl else "armed"
    if price > a:
        return "expired"
    return "reached" if price >= lvl else "armed"


# ─────────────────────────────────────────────────────────────
# GOOGLE SHEETS: ОСНОВНОЙ ЛИСТ
# ─────────────────────────────────────────────────────────────
def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


@st.cache_data(ttl=900, show_spinner=False)
def load_sheet_data() -> tuple[float, dict, dict, str]:
    deposit = 100_000.0
    tmap: dict[str, list[dict]] = {}
    meta = {"ok": False, "error": None, "rows": 0, "ts": time.time(),
            "level_col": False, "move_col": False,
            "entry_col": False, "sl_col": False, "tp_col": False}
    tariff_name = ""

    try:
        raw = pd.read_csv(SHEET_URL, header=None, dtype=str, keep_default_na=False)
    except Exception as e:
        meta["error"] = f"{type(e).__name__}: {e}"
        return deposit, tmap, meta, tariff_name

    if raw.empty:
        meta["error"] = "таблица пуста"
        return deposit, tmap, meta, tariff_name

    if raw.shape[1] > 1:
        v = parse_number(raw.iloc[0, 1])
        if v is not None and v > 0:
            deposit = v

    if deposit == 100_000.0:
        for c in range(min(4, raw.shape[1])):
            hit = raw[raw[c].map(_norm).str.contains("депозит", na=False)]
            if not hit.empty and c + 1 < raw.shape[1]:
                v = parse_number(raw.iloc[hit.index[0], c + 1])
                if v is not None and v > 0:
                    deposit = v
                    break

    if raw.shape[1] > 4:
        tariff_name = str(raw.iloc[0, 4]).strip()
        if tariff_name.lower() in {"nan", "none", ""}:
            tariff_name = ""

    hdr = None
    for i in range(min(30, len(raw))):
        if _norm(raw.iloc[i, 0]) in {"тикер", "ticker", "secid"}:
            hdr = i
            break
    if hdr is None:
        meta["error"] = "не найдена строка заголовков «Тикер»"
        meta["ok"] = True
        return deposit, tmap, meta, tariff_name

    names = [_norm(x) for x in raw.iloc[hdr].tolist()]

    def find(pred, default):
        for i, n in enumerate(names):
            if pred(n):
                return i
        return default

    c_t = find(lambda n: n in {"тикер", "ticker", "secid"}, 0)
    c_tw = find(lambda n: "tw" in n or "tradingview" in n, 1)
    c_a = find(lambda n: n in {"a", "точка a", "а"}, 2)
    c_b = find(lambda n: n in {"b", "точка b", "в"}, 3)
    c_l = find(lambda n: "уровень" in n or "level" in n or n in {"ур", "ур.", "lvl"}, None)
    c_m = find(lambda n: ("движен" in n or "movement" in n or "move" in n
                          or n in {"тип", "type", "движ", "движ."}), None)
    c_entry = find(lambda n: ("твх" in n or "вход" in n or "entry" in n
                              or n in {"цена входа", "цена", "price"}), None)
    c_sl = find(lambda n: ("sl" in n or "стоп" in n or "stop" in n
                           or n in {"стоп-лосс", "stop-loss", "стоп лосс"}), None)
    c_tp = find(lambda n: ("tp" in n or "тейк" in n or "take" in n
                           or n in {"тейк-профит", "take-profit", "тейк профит"}), None)
    meta["level_col"] = c_l is not None
    meta["move_col"] = c_m is not None
    meta["entry_col"] = c_entry is not None
    meta["sl_col"] = c_sl is not None
    meta["tp_col"] = c_tp is not None

    def cell(row, idx):
        if idx is None or idx >= len(row):
            return None
        return row.iloc[idx]

    for i in range(hdr + 1, len(raw)):
        row = raw.iloc[i]
        t = str(cell(row, c_t) or "").strip().upper()
        if not t or _norm(t) in {"тикер", "ticker", "nan", "итого"}:
            continue
        tw = str(cell(row, c_tw) or "").strip()
        a = parse_number(cell(row, c_a))
        b = parse_number(cell(row, c_b))
        lvl = parse_number(cell(row, c_l))
        move = normalize_move(cell(row, c_m))
        entry = parse_number(cell(row, c_entry))
        sl = parse_number(cell(row, c_sl))
        tp = parse_number(cell(row, c_tp))
        d = None
        if a is not None and b is not None and a != b:
            d = "LONG" if a < b else "SHORT"
        setup = {
            "tw": tw, "a": a, "b": b, "level": lvl, "dir": d, "move": move,
            "entry": entry, "sl": sl, "tp": tp,
        }
        bucket = tmap.setdefault(t, [])
        if setup not in bucket:
            bucket.append(setup)
        meta["rows"] += 1

    meta["ok"] = True
    return deposit, tmap, meta, tariff_name


# ─────────────────────────────────────────────────────────────
# GOOGLE SHEETS: ЛИСТ "КОМИССИИ"
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def load_commissions() -> tuple[dict, dict]:
    book: dict[str, dict] = {}
    meta = {"ok": False, "error": None, "ts": time.time()}

    try:
        raw = pd.read_csv(COMMISSIONS_URL, header=0, dtype=str, keep_default_na=False)
    except Exception as e:
        meta["error"] = f"{type(e).__name__}: {e}"
        return book, meta

    if raw.empty or raw.shape[1] < 3:
        meta["error"] = "лист «Комиссии» пуст или имеет меньше 3 колонок"
        return book, meta

    cols = [str(c).strip().lower() for c in raw.columns]

    def find_col(*variants):
        for v in variants:
            for i, c in enumerate(cols):
                if v in c:
                    return i
        return None

    c_name = find_col("название", "тариф", "name") or 0
    c_broker = find_col("брокер", "broker") or 1
    c_pct = find_col("комиссия", "%", "pct", "rate")
    c_min = find_col("мин", "min")

    if c_pct is None:
        c_pct = 2 if raw.shape[1] > 2 else None
    if c_min is None and raw.shape[1] > 3:
        c_min = 3

    for _, row in raw.iterrows():
        name = str(row.iloc[c_name]).strip()
        if not name or name.lower() in {"nan", "none", ""}:
            continue
        broker = str(row.iloc[c_broker]).strip() if c_broker is not None else ""
        pct = parse_number(row.iloc[c_pct]) if c_pct is not None else None
        mn = parse_number(row.iloc[c_min]) if c_min is not None else None
        if pct is None:
            continue
        book[name] = {
            "broker": broker,
            "pct": pct,
            "min": mn if mn is not None else 0.0,
        }

    meta["ok"] = True
    return book, meta


def resolve_commission(tariff_name: str, book: dict) -> tuple[float, float, str, bool]:
    if not tariff_name:
        return DEFAULT_COMMISSION_PCT, DEFAULT_MIN_COMMISSION, "", False
    if tariff_name in book:
        rec = book[tariff_name]
        return rec["pct"], rec["min"], tariff_name, True
    key = tariff_name.lower()
    for name, rec in book.items():
        if name.lower() == key:
            return rec["pct"], rec["min"], name, True
    return DEFAULT_COMMISSION_PCT, DEFAULT_MIN_COMMISSION, tariff_name, False


def calc_commission(turnover: float, pct: float, min_comm: float) -> float:
    if turnover <= 0:
        return 0.0
    return max(turnover * pct / 100.0, min_comm)


# ─────────────────────────────────────────────────────────────
# MOEX ISS
# ─────────────────────────────────────────────────────────────
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

@st.cache_resource(show_spinner=False)
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "moex-futures-desk/15.0"})
    s.verify = False
    s.mount(
        "https://",
        HTTPAdapter(
            max_retries=Retry(total=3, backoff_factor=0.5,
                              status_forcelist=(429, 500, 502, 503, 504)),
            pool_maxsize=8,
        ),
    )
    return s


@st.cache_data(ttl=300, show_spinner=False)
def fetch_futures() -> tuple[pd.DataFrame, float, str | None]:
    ts = time.time()
    try:
        r = _session().get(
            f"{ISS}/securities.json",
            params={"iss.meta": "off", "iss.only": "securities,marketdata"},
            timeout=30,
        )
        r.raise_for_status()
        js = r.json()
        sec = pd.DataFrame(js["securities"]["data"], columns=js["securities"]["columns"])
        mkt = pd.DataFrame(js["marketdata"]["data"], columns=js["marketdata"]["columns"])
    except Exception as e:
        return pd.DataFrame(), ts, f"{type(e).__name__}: {e}"

    sec = sec.drop_duplicates("SECID", keep="first")
    mkt = mkt.drop_duplicates("SECID", keep="first")
    df = sec.merge(mkt, on="SECID", how="left", suffixes=("", "_m"))

    for col in ("MINSTEP", "STEPPRICE", "INITIALMARGIN", "LAST",
                "PREVSETTLEPRICE", "DECIMALS"):
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    df = df[(df["MINSTEP"] > 0) & (df["STEPPRICE"] > 0)].copy()
    df["PRICE"] = df["LAST"].where(df["LAST"] > 0, df["PREVSETTLEPRICE"])
    return df, ts, None


@st.cache_data(ttl=900, show_spinner=False)
def fetch_ims() -> pd.DataFrame:
    def normalize(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df.columns = [str(c).upper().strip() for c in df.columns]
        if "BOARDID" in df.columns:
            f = df[df["BOARDID"] == "RFUD"]
            if not f.empty:
                df = f
        if "SECID" in df.columns:
            df = df.drop_duplicates("SECID", keep="first")
        df = df.copy()
        for c in df.columns:
            if c.startswith("IM_") and c != "IM_TIME":
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
        return df

    try:
        r = _session().get(f"{ISS}/ims.json", params={"iss.meta": "off"}, timeout=15)
        r.raise_for_status()
        block = r.json().get("ims") or {}
        df = pd.DataFrame(block.get("data") or [], columns=block.get("columns") or [])
        if not df.empty:
            return normalize(df)
    except Exception:
        pass

    try:
        r = _session().get(f"{ISS}/ims", params={"iss.meta": "off"}, timeout=15)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text), sep="|")
        return normalize(df)
    except Exception:
        return pd.DataFrame()


def merge_margins(futures: pd.DataFrame, ims: pd.DataFrame, margin_type: str) -> pd.DataFrame:
    df = futures.copy()
    base = df["INITIALMARGIN"].fillna(0.0)
    df["GO_LONG"] = base
    df["GO_SHORT"] = base
    cols = IMS_MARGIN_MAP.get(margin_type)
    if cols and not ims.empty and "SECID" in ims.columns:
        cb, cs = cols
        if cb in ims.columns and cs in ims.columns:
            m = ims.set_index("SECID")
            df["GO_LONG"] = df["SECID"].map(m[cb]).fillna(base)
            df["GO_SHORT"] = df["SECID"].map(m[cs]).fillna(base)
    df["GO_LONG"] = df["GO_LONG"].where(df["GO_LONG"] > 0, base)
    df["GO_SHORT"] = df["GO_SHORT"].where(df["GO_SHORT"] > 0, base)
    return df


# ─────────────────────────────────────────────────────────────
# INSTRUMENT
# ─────────────────────────────────────────────────────────────
@dataclass(slots=True)
class Instrument:
    ticker: str
    name: str
    min_step: float
    step_price: float
    price: float
    go_long: float
    go_short: float
    expiry: str
    decimals: int

    @property
    def point_value(self) -> float:
        return (self.step_price / self.min_step) if self.min_step else 1.0

    def margin(self, direction: str) -> float:
        return self.go_long if direction == "LONG" else self.go_short

    def round_price(self, p: float) -> float:
        if not self.min_step:
            return round(p, self.decimals)
        return round(round(p / self.min_step) * self.min_step, self.decimals)


def _decimals_from_step(step: float) -> int:
    if step <= 0:
        return 2
    return max(0, min(8, int(math.ceil(-math.log10(step) - 1e-9))))


def _to_instrument(row: pd.Series) -> Instrument:
    d = int(row.get("DECIMALS", 0) or 0)
    if d <= 0:
        d = _decimals_from_step(float(row["MINSTEP"]))
    return Instrument(
        ticker=str(row["SECID"]),
        name=str(row.get("SHORTNAME") or row["SECID"]),
        min_step=float(row["MINSTEP"]),
        step_price=float(row["STEPPRICE"]),
        price=float(row["PRICE"]),
        go_long=float(row["GO_LONG"]),
        go_short=float(row["GO_SHORT"]),
        expiry=str(row.get("LASTTRADEDATE") or "—"),
        decimals=max(0, min(8, d)),
    )


# ─────────────────────────────────────────────────────────────
# СВОДКА
# ─────────────────────────────────────────────────────────────
def build_summary(instruments: dict, tmap: dict) -> list[dict]:
    out = []
    for t, setups in tmap.items():
        inst = instruments.get(t)
        if inst is None:
            continue
        for s in setups:
            if s.get("dir") is None:
                continue
            z, info = determine_zone(inst.price, s["a"], s["b"])
            out.append({
                "ticker": t,
                "tw": s.get("tw", ""),
                "dir": s["dir"],
                "zone": z,
                "outside": (info or {}).get("outside"),
                "price": inst.price,
                "decimals": inst.decimals,
                "level": s.get("level"),
                "state": level_state(inst.price, s),
                "a": s["a"],
                "b": s["b"],
                "move": s.get("move"),
                "entry": s.get("entry"),
                "sl": s.get("sl"),
                "tp": s.get("tp"),
            })
    return out


def sort_summary(items: list[dict], mode: str) -> list[dict]:
    rank = {"reached": 0, "armed": 1, "none": 2, "expired": 3}

    if mode == "По тикеру":
        return sorted(items, key=lambda x: (x["ticker"], x["dir"]))

    if mode == "По зоне":
        return sorted(items, key=lambda x: (x["zone"] is None, x["zone"] or 9,
                                            x["ticker"], x["dir"]))

    if mode == "По уровню":
        def _lvl_key(x):
            if x["state"] == "reached":
                return (0, 0.0, x["ticker"])
            if x["state"] == "armed" and x.get("level") is not None:
                return (1, abs(x["price"] - x["level"]), x["ticker"])
            return (2, float("inf"), x["ticker"])
        return sorted(items, key=_lvl_key)

    if mode == "По движению":
        def _move_key(x):
            m = x.get("move")
            m_rank = MOVE_RANK.get(m, 99) if m else 99
            return (m_rank, x["ticker"], x["dir"])
        return sorted(items, key=_move_key)

    return sorted(items, key=lambda x: (rank.get(x["state"], 9),
                                        x["zone"] is None, x["zone"] or 9,
                                        x["ticker"], x["dir"]))


# ─────────────────────────────────────────────────────────────
# ВИЗУАЛИЗАЦИЯ: ЛЕСТНИЦА ЗОН
# ─────────────────────────────────────────────────────────────
def zone_ladder_html(info: dict, price: float, level: float | None,
                     lvl_state: str, decimals: int) -> str:
    lo, hi = info["lo"], info["hi"]
    span = hi - lo
    if span <= 0:
        return ""

    def y(v: float) -> float:
        return max(0.0, min(100.0, (hi - v) / span * 100.0))

    a, b, p1, p2 = info["a"], info["b"], info["p1"], info["p2"]
    long = a < b

    def band(v_from, v_to, z):
        top = y(max(v_from, v_to))
        bot = y(min(v_from, v_to))
        return (f'<div class="lad-band z{z}" style="top:{top:.3f}%;'
                f'height:{max(bot - top, 0):.3f}%"><i>{z}</i></div>')

    bands = band(a, p1, 1) + band(p1, p2, 2) + band(p2, b, 3)

    oor = info.get("outside") is not None
    price_line = (
        f'<div class="lad-price{" oor" if oor else ""}" style="top:{y(price):.3f}%">'
        f'<span class="dot"></span></div>'
    )

    level_line = ""
    if level is not None and lo <= level <= hi:
        cls = "hit" if lvl_state == "reached" else ("off" if lvl_state == "expired" else "")
        level_line = (
            f'<div class="lad-level {cls}" style="top:{y(level):.3f}%">'
            f'<span>{BELL if lvl_state == "reached" else ""}</span></div>'
        )

    top_label = "B" if long else "A"
    bot_label = "A" if long else "B"
    mid_hi_v, mid_lo_v = (p2, p1) if long else (p1, p2)

    axis = (
        f'<div class="lad-tick end" style="top:{y(hi):.3f}%">'
        f'<b>{top_label}</b> {fmt_px(hi, decimals)}</div>'
        f'<div class="lad-tick" style="top:{y(mid_hi_v):.3f}%">{fmt_px(mid_hi_v, decimals)}</div>'
        f'<div class="lad-tick" style="top:{y(mid_lo_v):.3f}%">{fmt_px(mid_lo_v, decimals)}</div>'
        f'<div class="lad-tick end" style="top:{y(lo):.3f}%">'
        f'<b>{bot_label}</b> {fmt_px(lo, decimals)}</div>'
    )

    return (
        f'<div class="ladder{" is-short" if not long else ""}">'
        f'<div class="lad-bar">{bands}{level_line}{price_line}</div>'
        f'<div class="lad-axis">{axis}</div>'
        f'</div>'
    )


# ─────────────────────────────────────────────────────────────
# СТРАНИЦА
# ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Калькулятор фьючерсов QCKSLVR TRADING",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
:root{
  --ink:#0B1220; --ink2:#16233A; --muted:#5A6577; --warm:#6B6459;
  --paper:#F2F5FA; --surface:#FFFFFF; --surface2:#E9EEF6; --line:#D3DCEA;
  --long:#0E9F6E; --long-deep:#046C4E; --long-soft:#E1F5EE;
  --short:#E02424; --short-deep:#B01B1B; --short-soft:#FDE7E7;
  --warn:#C2740A; --warn-soft:#FDF1D8;
  --info:#1D4ED8; --info-soft:#E3E9FE;
  --zl:#7C3AED; --zl-soft:#F3E8FF;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
}
html,body,[class*="css"]{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;
}
html, body { scroll-padding-top: 1.5rem; }
.stApp{background:
  radial-gradient(1100px 420px at 12% -8%, #E6EDFB 0%, rgba(230,237,251,0) 62%),
  radial-gradient(760px 380px at 104% 4%, #E4F5EE 0%, rgba(228,245,238,0) 58%),
  var(--paper);}
.block-container{padding-top:1.4rem;padding-bottom:1.1rem;max-width:760px;}
#MainMenu,footer{visibility:hidden;}

.app-title{
  display:flex;align-items:center;justify-content:space-between;gap:10px;
  background:linear-gradient(104deg,var(--ink) 0%,var(--ink2) 46%,#22357A 100%);
  color:#fff;border-radius:10px;padding:8px 12px;margin-bottom:9px;
  box-shadow:0 6px 18px -10px rgba(11,18,32,.75);
}
.app-title-main{flex:1;min-width:0;}
.app-title h1{font-size:.95rem;font-weight:700;margin:0;letter-spacing:.2px;}
.app-title .sub{font-size:.63rem;color:#9FB2D8;font-family:var(--mono);letter-spacing:.4px;}
.app-title .pulse{
  font-size:.63rem;font-family:var(--mono);background:rgba(255,255,255,.10);
  border:1px solid rgba(255,255,255,.20);border-radius:999px;padding:2px 8px;color:#DCE6FF;
  white-space:nowrap;flex-shrink:0;
}
.app-title .pulse b{color:#FFD27A;}

div[data-testid="stCheckbox"]{min-height:0;}
div[data-testid="stCheckbox"] label{align-items:center;gap:7px;}
div[data-testid="stCheckbox"] label p{
  font-size:.72rem!important;font-weight:700;color:var(--ink2);letter-spacing:.2px;}
div[data-testid="stCheckbox"] input[type="checkbox"]{accent-color:var(--info);}
div[data-testid="stCheckbox"] label span:first-of-type{border-color:#B7C3D8!important;}
div[data-testid="stCheckbox"] label:has(input:checked) span:first-of-type{
  background:linear-gradient(140deg,var(--info),#3B6BF0)!important;
  border-color:var(--info)!important;}
.filter-chips{display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end;align-items:center;
  height:100%;padding-top:2px;}

.strip{
  display:flex;background:var(--surface);border:1px solid var(--line);
  border-radius:9px;overflow:hidden;margin-bottom:8px;
  box-shadow:0 1px 0 rgba(11,18,32,.03);
}
.strip .cell{flex:1;padding:6px 8px;text-align:center;border-left:1px solid var(--line);min-width:0;}
.strip .cell:first-child{border-left:none;}
.strip .k{display:block;font-size:.58rem;letter-spacing:.7px;text-transform:uppercase;color:var(--muted);}
.strip .v{display:block;font-family:var(--mono);font-size:.86rem;font-weight:600;
  color:var(--ink);font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.strip.tint{background:linear-gradient(96deg,#EEF3FF 0%,#F4FAF7 100%);}

.desk{display:grid;grid-template-columns:146px 1fr;gap:10px;align-items:stretch;margin-bottom:8px;}
.ladder{display:flex;gap:6px;height:126px;background:var(--surface);
  border:1px solid var(--line);border-radius:9px;padding:8px 8px 8px 9px;}
.lad-bar{position:relative;width:34px;flex:0 0 34px;border-radius:5px;overflow:hidden;
  background:var(--surface2);box-shadow:inset 0 0 0 1px rgba(11,18,32,.07);}
.lad-band{position:absolute;left:0;right:0;display:flex;align-items:center;justify-content:center;}
.lad-band i{font-style:normal;font-size:.58rem;font-weight:700;font-family:var(--mono);opacity:.72;}
.lad-band.z1{background:linear-gradient(180deg,#BFE9DA,#8FD9BF);color:var(--long-deep);}
.lad-band.z2{background:linear-gradient(180deg,#FBE3B4,#F7D08A);color:#8A5308;}
.lad-band.z3{background:linear-gradient(180deg,#F6BDBD,#EFA0A0);color:var(--short-deep);}
.lad-price{position:absolute;left:-3px;right:-3px;height:0;border-top:2px solid var(--ink);
  transform:translateY(-1px);z-index:4;filter:drop-shadow(0 0 3px rgba(11,18,32,.45));}
.lad-price .dot{position:absolute;left:-1px;top:-4px;width:7px;height:7px;background:var(--ink);
  transform:rotate(45deg);border-radius:1px;}
.lad-price.oor{border-top-style:dashed;opacity:.55;}
.lad-level{position:absolute;left:0;right:0;height:0;border-top:2px dashed var(--warn);
  transform:translateY(-1px);z-index:3;}
.lad-level span{position:absolute;right:-2px;top:-8px;font-size:.55rem;line-height:1;}
.lad-level.hit{border-top-color:#8A5308;border-top-style:solid;}
.lad-level.off{opacity:.32;}
.lad-axis{position:relative;flex:1;min-width:0;}
.lad-tick{position:absolute;left:0;transform:translateY(-50%);font-family:var(--mono);
  font-size:.6rem;color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums;}
.lad-tick.end{color:var(--ink);font-weight:600;}
.lad-tick.end b{color:var(--info);}
.ladder.is-short .lad-tick.end b{color:var(--short-deep);}

.desk-side{display:flex;flex-direction:column;gap:6px;min-width:0;}
.src-tag{font-size:.58rem;letter-spacing:.5px;text-transform:uppercase;color:var(--warm);
  font-family:var(--mono);}
.zone-card{border-radius:9px;padding:7px 10px;border:1px solid var(--line);background:var(--surface);flex:1;
  display:flex;flex-direction:column;justify-content:center;gap:2px;}
.zone-card .zn{font-size:1.02rem;font-weight:700;font-family:var(--mono);line-height:1.1;}
.zone-card .zl{font-size:.68rem;color:var(--muted);}
.zone-card.z1{background:linear-gradient(100deg,var(--long-soft),#F6FCF9);border-color:#A9DFCB;}
.zone-card.z1 .zn{color:var(--long-deep);}
.zone-card.z2{background:linear-gradient(100deg,var(--warn-soft),#FFFBF2);border-color:#EBC98A;}
.zone-card.z2 .zn{color:#8A5308;}
.zone-card.z3{background:linear-gradient(100deg,var(--short-soft),#FFF7F7);border-color:#EFB3B3;}
.zone-card.z3 .zn{color:var(--short-deep);}
.zone-card.z0 .zn{color:var(--muted);}
.lvl-line{font-size:.68rem;font-family:var(--mono);color:var(--ink);
  background:var(--surface);border:1px solid var(--line);border-radius:7px;padding:4px 8px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.lvl-line.hit{background:var(--warn-soft);border-color:#E3B96B;color:#7A4B06;font-weight:700;}
.lvl-line.off{color:var(--muted);}

.hit-banner{
  display:flex;align-items:center;gap:8px;background:linear-gradient(98deg,#FDF1D8,#FFF9EC);
  border:1px solid #E3B96B;border-left:4px solid var(--warn);border-radius:8px;
  padding:6px 10px;margin-bottom:8px;font-size:.74rem;color:#7A4B06;font-weight:600;
  font-family:var(--mono);
}
.hit-banner .b{font-size:.9rem;}

.setup-apply{
  background:var(--surface);border:1px solid var(--line);border-radius:9px;
  padding:8px 10px;margin-bottom:6px;
  display:flex;flex-direction:column;gap:6px;
}
.setup-apply-head{
  display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;
}
.setup-apply-title{
  font-size:.62rem;font-weight:700;letter-spacing:.7px;text-transform:uppercase;
  color:var(--muted);font-family:var(--mono);
}
.setup-values{
  display:flex;gap:6px;flex-wrap:wrap;font-family:var(--mono);font-size:.68rem;
}
.setup-val{
  background:var(--surface2);border:1px solid var(--line);border-radius:5px;
  padding:3px 7px;color:var(--ink2);font-variant-numeric:tabular-nums;
  white-space:nowrap;
}
.setup-val b{color:var(--ink);font-weight:700;margin-right:3px;}
.setup-val.empty{color:var(--muted);font-style:italic;background:transparent;border-style:dashed;}

.lbl{font-size:.6rem;font-weight:700;letter-spacing:.7px;text-transform:uppercase;
  color:var(--muted);margin:6px 0 3px;}
div[data-testid="stNumberInput"] input{font-family:var(--mono);font-variant-numeric:tabular-nums;}
.stSelectbox div[data-baseweb="select"]>div{border-radius:8px;}
.stSelectbox span{font-family:var(--mono);font-size:.82rem;}

.stRadio div[role="radiogroup"]{gap:8px;}
.stRadio div[role="radiogroup"]>label{
  border-radius:8px;padding:6px 18px!important;font-weight:700;letter-spacing:.4px;
  transition:transform .12s ease, box-shadow .12s ease;
}
.stRadio div[role="radiogroup"]>label:has(input[value="LONG"]){
  background:var(--long-soft);border:2px solid var(--long);color:var(--long-deep);}
.stRadio div[role="radiogroup"]>label:has(input[value="SHORT"]){
  background:var(--short-soft);border:2px solid var(--short);color:var(--short-deep);}
.stRadio div[role="radiogroup"]>label:has(input[value="LONG"]:checked){
  background:linear-gradient(100deg,var(--long),#12B981);color:#fff;
  box-shadow:0 5px 14px -7px rgba(14,159,110,.9);transform:translateY(-1px);}
.stRadio div[role="radiogroup"]>label:has(input[value="SHORT"]:checked){
  background:linear-gradient(100deg,var(--short),#F05252);color:#fff;
  box-shadow:0 5px 14px -7px rgba(224,36,36,.9);transform:translateY(-1px);}
.stRadio div[role="radiogroup"]>label p{font-size:.8rem;font-weight:700;}

.chips{display:flex;gap:6px;flex-wrap:wrap;margin:2px 0 7px;}
.chip{font-family:var(--mono);font-size:.7rem;font-weight:700;border-radius:999px;
  padding:3px 10px;border:1px solid var(--line);background:var(--surface);color:var(--ink);
  font-variant-numeric:tabular-nums;}
.chip.ok{background:var(--long-soft);border-color:#A9DFCB;color:var(--long-deep);}
.chip.bad{background:var(--short-soft);border-color:#EFB3B3;color:var(--short-deep);}
.chip.warn{background:var(--warn-soft);border-color:#EBC98A;color:#8A5308;}
.chip.info{background:var(--info-soft);border-color:#B9C6F7;color:#1E3A8A;}
.chip.zl{background:var(--zl-soft);border-color:#C4B5FD;color:#5B21B6;}
.chip.mini{font-size:.62rem;padding:2px 8px;}

section[data-testid="stSidebar"]{background:linear-gradient(180deg,#FFFFFF 0%,#F5F8FD 100%);
  border-right:1px solid var(--line);}
section[data-testid="stSidebar"] h2{font-size:.86rem!important;font-weight:700;color:var(--ink);
  margin:.2rem 0 .1rem!important;padding:0!important;}
section[data-testid="stSidebar"] h2::before{content:"";display:inline-block;width:3px;height:12px;
  background:linear-gradient(180deg,var(--info),var(--long));border-radius:2px;margin-right:7px;
  vertical-align:-1px;}
section[data-testid="stSidebar"] p{font-size:.7rem;color:var(--muted);margin-bottom:.45rem;}
section[data-testid="stSidebar"] hr{margin:.55rem 0;border-color:var(--line);}

.ref{background:var(--surface);border:1px solid var(--line);border-radius:7px;padding:5px 8px;margin:4px 0 6px;}
.ref-row{font-family:var(--mono);font-size:.68rem;color:var(--ink2);padding:2px 0;
  border-bottom:1px dashed var(--line);font-variant-numeric:tabular-nums;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.ref-row:last-child{border-bottom:none;}
.ref-row b.L{color:var(--long-deep);} .ref-row b.S{color:var(--short-deep);}
.ref-row .lv{color:#8A5308;}
.ref-row .trd{color:var(--info);}

.move-tag{
  display:inline-block;font-family:var(--mono);font-size:.58rem;font-weight:700;
  letter-spacing:.3px;padding:1px 5px;border-radius:3px;margin-left:3px;
  vertical-align:middle;white-space:nowrap;
}
.move-zigzag{background:var(--short-soft);color:var(--short-deep);border:1px solid #EFB3B3;}
.move-trend{background:var(--long-soft);color:var(--long-deep);border:1px solid #A9DFCB;}
.move-wave{background:var(--info-soft);color:#1E3A8A;border:1px solid #B9C6F7;}

.sum-head{display:flex;gap:5px;align-items:center;margin:2px 0 4px;flex-wrap:wrap;}
.badge{font-family:var(--mono);font-size:.62rem;font-weight:700;border-radius:999px;padding:2px 8px;
  background:var(--surface2);color:var(--ink2);border:1px solid var(--line);}
.badge.hot{background:var(--warn-soft);border-color:#E3B96B;color:#7A4B06;}

.sum{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:3px 5px;
  max-height:264px;overflow-y:auto;overscroll-behavior:contain;
  scrollbar-width:thin;scrollbar-color:#9AA9C4 var(--surface2);}
.sum::-webkit-scrollbar{width:8px;}
.sum::-webkit-scrollbar-track{background:var(--surface2);border-radius:8px;}
.sum::-webkit-scrollbar-thumb{border-radius:8px;background:linear-gradient(180deg,var(--info),var(--long));}
.sum::-webkit-scrollbar-thumb:hover{background:var(--info);}
.sum-row{display:flex;align-items:center;gap:5px;padding:4px 4px;border-bottom:1px dashed var(--line);
  font-size:.7rem;line-height:1.15;}
.sum-row:last-child{border-bottom:none;}
.sum-row.hot{background:linear-gradient(96deg,var(--warn-soft),rgba(253,241,216,0));
  border-radius:5px;border-bottom-color:#EBC98A;}
.sum-row .em{font-size:.72rem;line-height:1;}
.sum-row .tk{font-family:var(--mono);font-weight:700;color:var(--ink);letter-spacing:.2px;}
.sum-row .tw{font-family:var(--mono);font-size:.6rem;color:var(--muted);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0;}
.sum-row .zn{font-size:.72rem;}
.sum-row .bell{font-family:var(--mono);font-size:.6rem;font-weight:700;color:#7A4B06;
  background:#FBE3B4;border:1px solid #E3B96B;border-radius:4px;padding:0 4px;white-space:nowrap;}
.sum-row .off{font-family:var(--mono);font-size:.58rem;color:var(--muted);
  background:var(--surface2);border-radius:4px;padding:0 4px;}
.sum-empty{font-size:.68rem;color:var(--muted);padding:6px 4px;font-family:var(--mono);}

.meta-note{font-family:var(--mono);font-size:.6rem;color:var(--warm);margin-top:4px;}
.stButton>button{border-radius:8px;font-size:.72rem;font-weight:600;padding:.28rem .5rem;
  border:1px solid var(--line);transition:transform .1s ease,border-color .1s ease;}
.stButton>button:hover{border-color:var(--info);transform:translateY(-1px);}
.streamlit-expanderHeader{font-size:.74rem;font-weight:600;}

@media (max-width:640px){
  .block-container{padding-top:1.2rem!important;padding-bottom:.6rem!important;}
  .stApp>header{display:none;}
  .app-title{
    flex-direction:column;align-items:stretch;gap:4px;
    padding:6px 10px;margin-bottom:7px;
  }
  .app-title-main{width:100%;}
  .app-title h1{font-size:.78rem;line-height:1.2;}
  .app-title .sub{font-size:.55rem;}
  .app-title .pulse{align-self:flex-start;font-size:.58rem;padding:2px 7px;}
  div[data-testid="stNumberInput"] label{display:none!important;}
  div[data-testid="stNumberInput"] input{height:2.15rem!important;font-size:14px!important;}
  div[data-testid="stNumberInput"]{min-height:2.15rem!important;}
  div[data-testid="stNumberInput"] button{display:none!important;}
  div[data-testid="stCheckbox"] label p{font-size:.68rem!important;}
  .stRadio>div{flex-direction:row!important;}
  .stButton>button{padding:.24rem .4rem!important;font-size:.68rem!important;}
  .streamlit-expanderHeader{font-size:.7rem!important;padding:.25rem 0!important;}
  .desk{grid-template-columns:132px 1fr;gap:8px;}
  .ladder{height:118px;padding:7px;}
  .lad-bar{width:30px;flex:0 0 30px;}
  .lad-tick{font-size:.56rem;}
  .strip .v{font-size:.78rem;}
  .chip.mini{font-size:.58rem;padding:1px 6px;}
  .move-tag{font-size:.52rem;padding:1px 4px;}
  .setup-apply{padding:6px 8px;}
  .setup-val{font-size:.62rem;padding:2px 5px;}
}
@media (max-width:360px){
  .desk{grid-template-columns:118px 1fr;}
  .lad-tick{font-size:.52rem;}
  .app-title h1{font-size:.72rem;}
  .app-title .sub{font-size:.5rem;}
  .app-title .pulse{font-size:.52rem;padding:1px 6px;}
}
</style>
""",
    unsafe_allow_html=True,
)

# ─────────────────────────────────────────────────────────────
# ОТЛОЖЕННОЕ ПРИМЕНЕНИЕ СЕТАПА
# ─────────────────────────────────────────────────────────────
_pending = st.session_state.pop("_pending_setup", None)
if _pending:
    if _pending.get("a") is not None:
        st.session_state["point_a_input"] = float(_pending["a"])
    if _pending.get("b") is not None:
        st.session_state["point_b_input"] = float(_pending["b"])
    if _pending.get("dir") is not None:
        st.session_state["_dir_idx"] = 1 if _pending["dir"] == "SHORT" else 0
        st.session_state.pop("direction_radio", None)
    if _pending.get("entry") is not None:
        st.session_state["entry_p_0"] = float(_pending["entry"])
    if _pending.get("sl") is not None:
        st.session_state["sl_input"] = float(_pending["sl"])
    if _pending.get("tp") is not None:
        st.session_state["tp_input"] = float(_pending["tp"])

# ─────────────────────────────────────────────────────────────
# ДАННЫЕ
# ─────────────────────────────────────────────────────────────
sheet_deposit, sheet_map, sheet_meta, tariff_name_raw = load_sheet_data()
commission_book, commission_meta = load_commissions()

comm_pct, comm_min, resolved_tariff, tariff_found = resolve_commission(
    tariff_name_raw, commission_book
)

level_cells = sum(1 for ss in sheet_map.values() for s in ss if s.get("level") is not None)
st.markdown(
    f"""
<div class="app-title">
  <div class="app-title-main">
    <h1>Калькулятор фьючерсов QCKSLVR TRADING</h1>
    <div class="sub">FORTS · RISK DESK · ZONES A/B</div>
  </div>
  <div class="pulse">уровни <b>{level_cells}</b></div>
</div>
""",
    unsafe_allow_html=True,
)

with st.spinner("Загрузка MOEX ISS…"):
    futures, fut_ts, fut_err = fetch_futures()
    ims = fetch_ims()

if fut_err:
    st.error(f"MOEX ISS недоступен: {fut_err}")
    st.stop()
if futures.empty:
    st.error("Не удалось загрузить список фьючерсов с MOEX ISS.")
    st.stop()

margin_type = st.session_state.get("margin_type_sel", "КСУР")
futures = merge_margins(futures, ims, margin_type)
futures = futures[(futures["PRICE"] > 0) &
                  ((futures["GO_LONG"] > 0) | (futures["GO_SHORT"] > 0))].copy()
if futures.empty:
    st.warning("Нет контрактов с валидными ценой и ГО.")
    st.stop()

futures = futures.sort_values("SECID").reset_index(drop=True)
instruments: dict[str, Instrument] = {
    row["SECID"]: _to_instrument(row) for _, row in futures.iterrows()
}

summary_all = build_summary(instruments, sheet_map)
hot_tickers = {x["ticker"] for x in summary_all if x["state"] == "reached"}
hot_count = sum(1 for x in summary_all if x["state"] == "reached")

all_tickers = list(instruments.keys())
sheet_tickers = [t for t in all_tickers if t in sheet_map]
missing_tickers = [t for t in sheet_map if t not in instruments]


# ─────────────────────────────────────────────────────────────
# ФИЛЬТР СПИСКА + ВЫБОР ИНСТРУМЕНТА
# ─────────────────────────────────────────────────────────────
fcol, ccol = st.columns([1.18, 1], gap="small")
only_sheet = fcol.checkbox(
    "Только тикеры из таблицы",
    value=bool(st.session_state.get("only_sheet_tickers", False)),
    key="only_sheet_tickers",
    help="Оставить в списке только фьючерсы, присутствующие в Google Sheets",
)

filter_active = only_sheet and bool(sheet_tickers)
options = sheet_tickers if filter_active else all_tickers

chips = [f'<span class="chip mini info">в списке {len(options)}</span>']
if sheet_tickers:
    chips.append(f'<span class="chip mini">таблица {len(sheet_tickers)}</span>')
if hot_count:
    chips.append(f'<span class="chip mini warn">{BELL} {hot_count}</span>')
ccol.markdown(f'<div class="filter-chips">{"".join(chips)}</div>', unsafe_allow_html=True)

if only_sheet and not sheet_tickers:
    st.warning("В таблице нет тикеров, доступных в текущем списке MOEX — показан полный список.")
elif filter_active and missing_tickers:
    st.caption(
        f"Не найдены в ISS (истёкшие или иные коды): {', '.join(sorted(missing_tickers)[:8])}"
        + (" …" if len(missing_tickers) > 8 else "")
    )


def format_ticker(t: str) -> str:
    tw = ""
    for s in sheet_map.get(t, []):
        if s.get("tw"):
            tw = s["tw"]
            break
    base = f"{t} ({tw})" if tw else t
    return f"{base} {BELL}" if t in hot_tickers else base


prev_sel = st.session_state.get("ticker_select")
if prev_sel in options:
    sel_index = options.index(prev_sel)
else:
    st.session_state.pop("ticker_select", None)
    preferred = next((t for t in options if t in hot_tickers), options[0])
    sel_index = options.index(preferred)

ticker = st.selectbox(
    "Фьючерс",
    options,
    index=sel_index,
    format_func=format_ticker,
    label_visibility="collapsed",
    key="ticker_select",
)

if st.session_state.get("last_ticker") != ticker:
    for k in TRADE_STATE_KEYS:
        st.session_state.pop(k, None)
    st.session_state.pop("direction_radio", None)
    st.session_state.pop("_setup_msg", None)
    dirs = {s["dir"] for s in sheet_map.get(ticker, []) if s.get("dir")}
    st.session_state["_dir_idx"] = 1 if dirs == {"SHORT"} else 0
    st.session_state["last_ticker"] = ticker

inst = instruments[ticker]
setups = sheet_map.get(ticker, [])
dec = inst.decimals
fmt_in = f"%.{dec}f"
step_ui = float(inst.min_step) if inst.min_step > 0 else 0.01


# ─────────────────────────────────────────────────────────────
# САЙДБАР
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Депозит и риск на сделку")
    st.markdown("База для расчёта объёма позиции и денежного риска.")

    deposit = st.number_input(
        "Депозит, руб.", min_value=0.0, value=float(sheet_deposit),
        step=1000.0, format="%.2f", key="deposit_input",
    )
    st.markdown(
        f'<div class="ref"><div class="ref-row">Таблица&nbsp;B1: <b>{fmt_n(sheet_deposit, 2)}</b> руб.</div></div>',
        unsafe_allow_html=True,
    )
    risk_pct = st.number_input(
        "Риск на сделку, %", min_value=0.1, max_value=100.0,
        value=1.0, step=0.1, key="risk_pct_input",
    )

    st.divider()
    st.header("Лимиты загрузки и RR")
    st.markdown("Контроль нагрузки на депозит и минимальное риск/прибыль.")

    margin_util = st.number_input(
        "Макс. загрузка ГО, %", min_value=10, max_value=100,
        value=100, step=10, key="margin_util_input",
    )
    required_rr = st.number_input(
        "Требуемый RR", min_value=0.5, max_value=20.0,
        value=3.0, step=0.5, key="required_rr_input",
    )
    margin_type = st.selectbox(
        "Категория риска (ГО)", MARGIN_LEVELS,
        index=MARGIN_LEVELS.index("КСУР"),
        format_func=lambda x: MARGIN_HINTS.get(x, x),
        key="margin_type_sel",
    )

    # ── БЛОК ТАРИФА ──
    st.divider()
    st.header("Торговый тариф")
    if tariff_found:
        st.markdown(
            f'<div class="ref">'
            f'<div class="ref-row"><b>{resolved_tariff}</b></div>'
            f'<div class="ref-row">комиссия: {fmt_n(comm_pct, 3)}% · мин. {fmt_n(comm_min, 2)} ₽</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    elif tariff_name_raw:
        st.markdown(
            f'<div class="ref">'
            f'<div class="ref-row" style="color:#B01B1B;">Тариф «{tariff_name_raw}» не найден</div>'
            f'<div class="ref-row">используется дефолт: {fmt_n(DEFAULT_COMMISSION_PCT, 3)}% · мин. {fmt_n(DEFAULT_MIN_COMMISSION, 2)} ₽</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div class="ref">'
            f'<div class="ref-row" style="color:var(--muted);">E1 не заполнен — дефолт</div>'
            f'<div class="ref-row">комиссия: {fmt_n(DEFAULT_COMMISSION_PCT, 3)}% · мин. {fmt_n(DEFAULT_MIN_COMMISSION, 2)} ₽</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

    st.divider()

    st.header("Сводка по активам")
    st.markdown("Зона текущей цены по A/B из таблицы. 🟩 LONG · 🟥 SHORT · 1️⃣2️⃣3️⃣ зоны.")

    st.markdown(
        f'<div class="sum-head">'
        f'<span class="badge">{len({x["ticker"] for x in summary_all})} активов</span>'
        f'<span class="badge{" hot" if hot_count else ""}">{BELL} {hot_count} сработало</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    fcol2, scol2 = st.columns([1, 1], gap="small")
    only_hot = fcol2.checkbox("Только уровни", key="sum_only_hot")
    sort_mode = scol2.selectbox(
        "Сортировка", ["По сигналу", "По уровню", "По движению", "По зоне", "По тикеру"],
        key="sum_sort", label_visibility="collapsed",
    )

    items = [x for x in summary_all if x["state"] == "reached"] if only_hot else summary_all
    items = sort_summary(items, sort_mode)

    if items:
        rows = []
        for x in items:
            zn = ZONE_EMOJI.get(x["zone"], "▫️")
            tw = f'<span class="tw">{x["tw"]}</span>' if x["tw"] else '<span class="tw"></span>'
            mark = ""
            if x["state"] == "reached":
                mark = f'<span class="bell">{BELL} {fmt_px(x["level"], x["decimals"])}</span>'
            elif x["state"] == "expired":
                mark = '<span class="off">снят</span>'
            move_html = move_tag_html(x.get("move"))
            hint = (f'{x["ticker"]} · {x["dir"]} · цена {fmt_px(x["price"], x["decimals"])} · '
                    f'A {fmt_px(x["a"], x["decimals"])} / B {fmt_px(x["b"], x["decimals"])}'
                    + (f' · {x["move"]}' if x.get("move") else ""))
            rows.append(
                f'<div class="sum-row{" hot" if x["state"] == "reached" else ""}" title="{hint}">'
                f'<span class="em">{DIR_EMOJI.get(x["dir"], "")}</span>'
                f'<span class="tk">{x["ticker"]}</span>'
                f'{tw}'
                f'{move_html}'
                f'<span class="zn">{zn}</span>'
                f'{mark}'
                f'</div>'
            )
        st.markdown(f'<div class="sum">{"".join(rows)}</div>', unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="sum"><div class="sum-empty">'
            + ("Нет сработавших уровней" if only_hot else "Нет данных таблицы в списке MOEX")
            + "</div></div>",
            unsafe_allow_html=True,
        )

    st.divider()

    st.header("Зоны риска A-B")
    st.markdown("Зона 1 у A — вход, зона 2 — объём ×0.5, зона 3 у B — запрет.")

    if setups:
        st.markdown('<div class="lbl">Загрузить сетап (A/B)</div>', unsafe_allow_html=True)
        acols = st.columns(min(3, len(setups)), gap="small")
        for i, s in enumerate(setups[:3]):
            label = f"{s['dir'] or '—'} {fmt_px(s['a'], dec)}"
            if acols[i].button(label, key=f"apply_ab_{ticker}_{i}", use_container_width=True):
                st.session_state["_pending_setup"] = {"a": s.get("a"), "b": s.get("b"),
                                                      "dir": s.get("dir"),
                                                      "entry": s.get("entry"),
                                                      "sl": s.get("sl"),
                                                      "tp": s.get("tp")}
                st.rerun()

    point_a = st.number_input(
        "Точка A", value=None, step=step_ui, format=fmt_in,
        placeholder="Точка A", key="point_a_input",
    )
    point_b = st.number_input(
        "Точка B", value=None, step=step_ui, format=fmt_in,
        placeholder="Точка B", key="point_b_input",
    )

    # ── РЕЖИМ ЗАХВАТА ЛИКВИДНОСТИ ──
    st.divider()
    use_zl_mode = st.checkbox(
        "🎯 Режим: Захват ликвидности (ЗЛ)",
        key="use_zl_mode",
        help="Цена ныряет за точку А и возвращается. Усреднение с увеличением объёма.",
    )

    zl_price = None
    if use_zl_mode:
        zl_price = st.number_input(
            "Уровень ЗЛ (Захвата ликвидности)",
            value=None, step=step_ui, format=fmt_in,
            placeholder="Цена захвата ликвидности",
            key="zl_input",
        )
        st.caption(
            "Для LONG: ЗЛ ниже точки А, SL ниже ЗЛ.\n"
            "Для SHORT: ЗЛ выше точки А, SL выше ЗЛ."
        )

    st.markdown('<div class="lbl">Справка из таблицы</div>', unsafe_allow_html=True)
    if setups:
        ref = []
        for s in setups:
            cls = "L" if s["dir"] == "LONG" else "S"
            a_t = fmt_px(s["a"], dec) if s["a"] is not None else "—"
            b_t = fmt_px(s["b"], dec) if s["b"] is not None else "—"
            lv = (f' · <span class="lv">ур {fmt_px(s["level"], dec)}</span>'
                  if s.get("level") is not None else "")
            mv = move_tag_html(s.get("move"))
            entry_t = fmt_px(s["entry"], dec) if s.get("entry") is not None else "—"
            sl_t = fmt_px(s["sl"], dec) if s.get("sl") is not None else "—"
            tp_t = fmt_px(s["tp"], dec) if s.get("tp") is not None else "—"
            trd = f' · <span class="trd">Твх {entry_t} / SL {sl_t} / TP {tp_t}</span>'
            ref.append(
                f'<div class="ref-row"><b class="{cls}">{s["dir"] or "—"}</b> '
                f'A {a_t} · B {b_t}{lv}{trd}{mv}</div>'
            )
        st.markdown(f'<div class="ref">{"".join(ref)}</div>', unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="ref"><div class="ref-row">Тикера нет в таблице</div></div>',
            unsafe_allow_html=True,
        )

    st.caption("LONG: A снизу, B сверху. SHORT: A сверху, B снизу.")

    st.divider()
    if st.button("Обновить котировки и таблицу", use_container_width=True):
        fetch_futures.clear()
        fetch_ims.clear()
        load_sheet_data.clear()
        load_commissions.clear()
        st.rerun()

    ims_note = "IMS подключён" if not ims.empty else "IMS недоступен — базовое ГО"
    sheet_note = "таблица OK" if sheet_meta["ok"] else "таблица недоступна"
    comm_note = ("тарифы OK" if commission_meta["ok"]
                 else f"тарифы: {commission_meta.get('error') or 'недоступны'}")
    lvl_note = "«уровень»" if sheet_meta["level_col"] else "нет «уровня»"
    move_note = "«движение»" if sheet_meta["move_col"] else "нет «движения»"
    trade_note = ("«Твх/SL/TP»" if (sheet_meta["entry_col"] or sheet_meta["sl_col"] or sheet_meta["tp_col"])
                  else "нет «Твх/SL/TP»")
    scope_note = "список: таблица" if filter_active else "список: все фьючерсы"
    st.markdown(
        f'<div class="meta-note">котировки {fmt_clock(fut_ts)} · таблица {fmt_clock(sheet_meta["ts"])}<br>'
        f'{len(instruments)} контрактов · {scope_note}<br>{ims_note}<br>'
        f'{sheet_note} · {comm_note}<br>{lvl_note} · {move_note} · {trade_note}</div>',
        unsafe_allow_html=True,
    )

if sheet_meta["error"]:
    st.caption(f"Таблица: {sheet_meta['error']}")


# ─────────────────────────────────────────────────────────────
# ЦЕНА · ГО · ЛЕСТНИЦА ЗОН
# ─────────────────────────────────────────────────────────────
st.markdown(
    f"""
<div class="strip">
  <div class="cell"><span class="k">Цена</span><span class="v">{fmt_px(inst.price, dec)}</span></div>
  <div class="cell"><span class="k">Шаг / ст.</span><span class="v">{fmt_px(inst.min_step, dec)} / {fmt_n(inst.step_price, 2)}₽</span></div>
  <div class="cell"><span class="k">ГО {margin_type} L/S</span><span class="v">{fmt_n(inst.go_long, 0)} / {fmt_n(inst.go_short, 0)}</span></div>
</div>
""",
    unsafe_allow_html=True,
)

manual_ab = point_a is not None and point_b is not None and point_a != point_b
active_setup = None
if manual_ab:
    src_a, src_b = float(point_a), float(point_b)
    src_label = "По введённым A/B"
else:
    for s in setups:
        if s.get("dir"):
            active_setup = s
            break
    if active_setup:
        src_a, src_b = active_setup["a"], active_setup["b"]
        src_label = f"Таблица · {active_setup['dir']}"
    else:
        src_a = src_b = None
        src_label = "A/B не заданы"

cur_zone, cur_info = (determine_zone(inst.price, src_a, src_b)
                      if src_a is not None else (None, None))

cur_level, cur_lvl_state = None, "none"
if active_setup is not None:
    cur_level = active_setup.get("level")
    cur_lvl_state = level_state(inst.price, active_setup)
elif manual_ab:
    d = "LONG" if src_a < src_b else "SHORT"
    for s in setups:
        if s.get("dir") == d and s.get("level") is not None:
            cur_level = s["level"]
            cur_lvl_state = level_state(inst.price, {"level": cur_level, "a": src_a, "dir": d})
            break

ladder = (zone_ladder_html(cur_info, inst.price, cur_level, cur_lvl_state, dec)
          if cur_info else
          '<div class="ladder" style="align-items:center;justify-content:center;">'
          '<span style="font-size:.62rem;color:#5A6577;font-family:var(--mono);text-align:center;">'
          'Задайте A и B<br>в боковом меню</span></div>')

if cur_zone:
    zt, zl_text = ZONE_LABEL[cur_zone]
    zcard = (f'<div class="zone-card z{cur_zone}"><span class="zn">{ZONE_EMOJI[cur_zone]} {zt}</span>'
             f'<span class="zl">{zl_text} · цена {fmt_px(inst.price, dec)}</span></div>')
elif cur_info:
    if cur_info.get("outside") == "below":
        side = "ниже A" if cur_info["dir"] == "LONG" else "выше A"
    else:
        side = "выше B" if cur_info["dir"] == "LONG" else "ниже B"
    zcard = (f'<div class="zone-card z0"><span class="zn">▫️ Вне диапазона</span>'
             f'<span class="zl">цена {side} · {fmt_px(inst.price, dec)}</span></div>')
else:
    zcard = ('<div class="zone-card z0"><span class="zn">—</span>'
             '<span class="zl">зона не определена</span></div>')

if cur_level is not None:
    eff_dir = (active_setup or {}).get("dir") or ("LONG" if src_a < src_b else "SHORT")
    if cur_lvl_state == "reached":
        cmp = "≤" if eff_dir == "LONG" else "≥"
        lvl_html = (f'<div class="lvl-line hit">{BELL} уровень {fmt_px(cur_level, dec)} '
                    f'отработан ({cmp})</div>')
    elif cur_lvl_state == "expired":
        lvl_html = f'<div class="lvl-line off">уровень {fmt_px(cur_level, dec)} снят — цена за A</div>'
    else:
        lvl_html = (f'<div class="lvl-line">уровень {fmt_px(cur_level, dec)} · '
                    f'ждать {fmt_px(abs(inst.price - cur_level), dec)}</div>')
else:
    lvl_html = (f'<div class="lvl-line off">1 пункт = {fmt_n(inst.point_value, 2)} ₽ · '
                f'эксп. {fmt_date(inst.expiry)}</div>')

st.markdown(
    f'<div class="desk">{ladder}'
    f'<div class="desk-side"><span class="src-tag">{src_label}</span>{zcard}{lvl_html}</div>'
    f'</div>',
    unsafe_allow_html=True,
)

for s in setups:
    if level_state(inst.price, s) == "reached":
        cmp = "≤" if s["dir"] == "LONG" else "≥"
        move_html = move_tag_html(s.get("move"))
        st.markdown(
            f'<div class="hit-banner"><span class="b">{BELL}</span>'
            f'<span>{s["dir"]}: цена {fmt_px(inst.price, dec)} {cmp} уровень '
            f'{fmt_px(s["level"], dec)}{move_html} — сигнал на вход</span></div>',
            unsafe_allow_html=True,
        )


# ─────────────────────────────────────────────────────────────
# БЛОК ПРИМЕНЕНИЯ СЕТАПА СДЕЛКИ ИЗ ТАБЛИЦЫ
# ─────────────────────────────────────────────────────────────
trade_setups = [s for s in setups if any(s.get(k) is not None for k in ("entry", "sl", "tp"))]

if trade_setups:
    ts_idx_key = f"ts_idx_{ticker}"
    cur_ts_idx = st.session_state.get(ts_idx_key, 0)
    if cur_ts_idx >= len(trade_setups):
        cur_ts_idx = 0
        st.session_state[ts_idx_key] = 0
    cur_ts = trade_setups[cur_ts_idx]

    def _val_chip(label: str, v, d: int) -> str:
        if v is None:
            return f'<span class="setup-val empty"><b>{label}</b>—</span>'
        return f'<span class="setup-val"><b>{label}</b>{fmt_px(v, d)}</span>'

    dir_chip = f'<span class="setup-val"><b>{DIR_EMOJI.get(cur_ts["dir"], "")}</b>{cur_ts["dir"]}</span>'
    move_chip = move_tag_html(cur_ts.get("move"))

    chips_html = (
        f'<div class="setup-values">'
        f'{dir_chip}'
        f'{_val_chip("A", cur_ts.get("a"), dec)}'
        f'{_val_chip("B", cur_ts.get("b"), dec)}'
        f'{_val_chip("Твх", cur_ts.get("entry"), dec)}'
        f'{_val_chip("SL", cur_ts.get("sl"), dec)}'
        f'{_val_chip("TP", cur_ts.get("tp"), dec)}'
        f'{move_chip}'
        f'</div>'
    )

    nav_label = ""
    if len(trade_setups) > 1:
        nav_label = (
            f'<span class="setup-apply-title" style="margin-right:4px;">'
            f'сетап {cur_ts_idx + 1} / {len(trade_setups)}</span>'
        )

    st.markdown(
        f'<div class="setup-apply">'
        f'<div class="setup-apply-head">'
        f'<div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">'
        f'<span class="setup-apply-title">Сделка из таблицы</span>'
        f'{nav_label}'
        f'</div>'
        f'</div>{chips_html}</div>',
        unsafe_allow_html=True,
    )

    if len(trade_setups) > 1:
        nav_c1, nav_c2, nav_c3 = st.columns([1, 2, 1], gap="small")
        with nav_c1:
            btn_prev = st.button("◀ Пред.", key=f"ts_prev_{ticker}", use_container_width=True)
        with nav_c2:
            btn_apply = st.button("ПРИМЕНИТЬ СЕТАП", key=f"ts_apply_{ticker}",
                                  type="primary", use_container_width=True)
        with nav_c3:
            btn_next = st.button("След. ▶", key=f"ts_next_{ticker}", use_container_width=True)
    else:
        btn_prev, btn_next = False, False
        btn_apply = st.button("ПРИМЕНИТЬ СЕТАП", key=f"ts_apply_{ticker}",
                              type="primary", use_container_width=True)

    if btn_prev:
        st.session_state[ts_idx_key] = (cur_ts_idx - 1) % len(trade_setups)
        st.rerun()
    elif btn_next:
        st.session_state[ts_idx_key] = (cur_ts_idx + 1) % len(trade_setups)
        st.rerun()
    elif btn_apply:
        ts = trade_setups[st.session_state.get(ts_idx_key, 0)]
        st.session_state["_pending_setup"] = ts
        st.session_state["_setup_msg"] = (
            f"Сетап применён: {ts['dir']} · "
            f"Твх {fmt_px(ts.get('entry'), dec) if ts.get('entry') is not None else '—'} · "
            f"SL {fmt_px(ts.get('sl'), dec) if ts.get('sl') is not None else '—'} · "
            f"TP {fmt_px(ts.get('tp'), dec) if ts.get('tp') is not None else '—'}"
        )
        st.rerun()

_setup_msg = st.session_state.pop("_setup_msg", None)
if _setup_msg:
    st.success(_setup_msg)


# ─────────────────────────────────────────────────────────────
# СДЕЛКА
# ─────────────────────────────────────────────────────────────
direction = st.radio(
    "Направление", ["LONG", "SHORT"], horizontal=True,
    index=st.session_state.get("_dir_idx", 0),
    label_visibility="collapsed", key="direction_radio",
)
sign = 1 if direction == "LONG" else -1

st.markdown('<div class="lbl">Стоп-лосс / тейк-профит</div>', unsafe_allow_html=True)
c1, c2 = st.columns(2, gap="small")
sl = c1.number_input("SL", value=None, step=step_ui, format=fmt_in,
                     placeholder="Стоп-лосс", label_visibility="collapsed", key="sl_input")
tp = c2.number_input("TP", value=None, step=step_ui, format=fmt_in,
                     placeholder="Тейк-профит", label_visibility="collapsed", key="tp_input")

st.markdown('<div class="lbl">Вход · цена и контракты</div>', unsafe_allow_html=True)
ec1, ec2 = st.columns([3, 2], gap="small")
p1 = ec1.number_input("Цена 1", value=None, step=step_ui, format=fmt_in,
                      placeholder="Цена входа", label_visibility="collapsed", key="entry_p_0")
q1 = ec2.number_input("Контр. 1", min_value=0, value=None, step=1, format="%d",
                      placeholder="Кол-во", label_visibility="collapsed", key="entry_q_0")

entries = []
if p1 is not None:
    entries.append({"price": p1, "qty": q1 or 0})

with st.expander("Усреднение — ещё две точки входа", expanded=False):
    for i in range(1, 3):
        a1, a2 = st.columns([3, 2], gap="small")
        p = a1.number_input(f"Цена {i+1}", value=None, step=step_ui, format=fmt_in,
                            placeholder=f"Цена входа {i+1}", label_visibility="collapsed",
                            key=f"entry_p_{i}")
        q = a2.number_input(f"Контр. {i+1}", min_value=0, value=None, step=1, format="%d",
                            placeholder=f"Кол-во {i+1}", label_visibility="collapsed",
                            key=f"entry_q_{i}")
        if p is not None:
            entries.append({"price": p, "qty": q or 0})

with_qty = [e for e in entries if e["qty"] > 0]
if with_qty:
    qty_sum = sum(e["qty"] for e in with_qty)
    avg_entry = sum(e["price"] * e["qty"] for e in with_qty) / qty_sum
elif entries:
    avg_entry = entries[0]["price"]
    qty_sum = 0
else:
    avg_entry = None
    qty_sum = 0
effective_qty = qty_sum

if avg_entry is not None and sl is not None and avg_entry != sl:
    risk_pts = abs(avg_entry - sl)
    min_tp = inst.round_price(avg_entry + sign * risk_pts * required_rr)
    st.markdown(
        f'<div class="chips"><span class="chip info">мин. TP для RR {fmt_n(required_rr, 2)} → '
        f'{fmt_px(min_tp, dec)}</span>'
        f'<span class="chip">риск {fmt_px(risk_pts, dec)} пт</span></div>',
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────
# ВАЛИДАЦИЯ
# ─────────────────────────────────────────────────────────────
have_prices = avg_entry is not None and sl is not None and tp is not None
have_qty = effective_qty > 0
warnings: list[str] = []

if have_prices:
    if direction == "LONG":
        if sl >= avg_entry:
            warnings.append("LONG: стоп-лосс должен быть ниже средней цены входа.")
        if tp <= avg_entry:
            warnings.append("LONG: тейк-профит должен быть выше средней цены входа.")
    else:
        if sl <= avg_entry:
            warnings.append("SHORT: стоп-лосс должен быть выше средней цены входа.")
        if tp >= avg_entry:
            warnings.append("SHORT: тейк-профит должен быть ниже средней цены входа.")

# Валидация Захвата Ликвидности (ЗЛ)
zl_valid = True
if use_zl_mode and zl_price is not None and point_a is not None:
    if direction == "LONG":
        if zl_price >= point_a:
            zl_valid = False
            warnings.append("ЗЛ: для LONG уровень захвата ликвидности должен быть НИЖЕ точки A.")
        elif sl is not None and sl >= zl_price:
            zl_valid = False
            warnings.append("ЗЛ: для LONG стоп-лосс (SL) должен быть НИЖЕ уровня захвата ликвидности.")
    else:  # SHORT
        if zl_price <= point_a:
            zl_valid = False
            warnings.append("ЗЛ: для SHORT уровень захвата ликвидности должен быть ВЫШЕ точки A.")
        elif sl is not None and sl <= zl_price:
            zl_valid = False
            warnings.append("ЗЛ: для SHORT стоп-лосс (SL) должен быть ВЫШЕ уровня захвата ликвидности.")

entry_zone, entry_info = (determine_zone(avg_entry, point_a, point_b) if manual_ab else (None, None))
zone_blocked, zone_reduction, zone_message = False, 1.0, None

# ИСПРАВЛЕННАЯ ЛОГИКА ЗОН:
# Зона 1 (у точки A) — оптимальна для обоих направлений
# Зона 2 (середина) — повышенный риск, сокращение на 50%
# Зона 3 (у точки B) — запрещена для обоих направлений
if manual_ab and entry_zone is not None:
    if entry_zone == 3:
        zone_blocked = True
        zone_message = f"ЗАПРЕЩЕНО: вход в зоне 3 (у точки B) для {direction}. Высокий риск."
    elif entry_zone == 2:
        zone_reduction = 0.5
        zone_message = "Зона 2: повышенный риск — рекомендуемый объём сокращён на 50%."
    else:
        zone_message = f"Зона {entry_zone}: оптимальная для {direction}."

for w in warnings:
    st.warning(w)

if zone_message:
    cls = "bad" if zone_blocked else ("warn" if zone_reduction < 1 else "ok")
    st.markdown(
        f'<div class="chips"><span class="chip {cls}">вход · {ZONE_EMOJI.get(entry_zone, "▫️")} '
        f'{zone_message}</span></div>',
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────
# РАСЧЁТ КОМИССИЙ
# ─────────────────────────────────────────────────────────────
turnover_one_way = (avg_entry or 0.0) * inst.point_value * effective_qty
commission_one_way = calc_commission(turnover_one_way, comm_pct, comm_min)
total_commission = commission_one_way * 2.0


# ─────────────────────────────────────────────────────────────
# РЕЗУЛЬТАТ
# ─────────────────────────────────────────────────────────────
if zone_blocked:
    st.error(zone_message)
elif not have_prices:
    st.markdown(
        '<div class="chips"><span class="chip">Заполните вход, SL и TP для расчёта</span></div>',
        unsafe_allow_html=True,
    )
elif not have_qty:
    sl_dist = abs(avg_entry - sl)
    tp_dist = abs(tp - avg_entry)
    rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0
    st.markdown(
        f'<div class="strip tint"><div class="cell"><span class="k">Расчётный RR</span>'
        f'<span class="v">{fmt_n(rr, 2)}</span></div>'
        f'<div class="cell"><span class="k">Средний вход</span>'
        f'<span class="v">{fmt_px(avg_entry, dec)}</span></div></div>',
        unsafe_allow_html=True,
    )
    st.caption("Укажите количество контрактов — появится сайзинг и проверка MM.")
else:
    sl_dist = abs(avg_entry - sl)
    tp_dist = abs(tp - avg_entry)
    sl_steps = sl_dist / inst.min_step
    tp_steps = tp_dist / inst.min_step
    risk_per_ct = sl_steps * inst.step_price
    profit_per_ct = tp_steps * inst.step_price

    margin = inst.margin(direction)
    risk_budget = deposit * risk_pct / 100
    qty_by_risk = int(risk_budget // risk_per_ct) if risk_per_ct > 0 else 0
    qty_by_margin = int((deposit * margin_util / 100) // margin) if margin > 0 else 0
    qty_base = min(qty_by_risk, qty_by_margin)
    qty_rec = int(qty_base * zone_reduction)

    gross_risk = risk_per_ct * effective_qty
    gross_reward = profit_per_ct * effective_qty

    net_risk = gross_risk + total_commission
    net_reward = max(gross_reward - total_commission, 0.0)
    net_rr = (net_reward / net_risk) if net_risk > 0 else 0.0

    actual_risk_pct = (net_risk / deposit * 100) if deposit else 0.0

    rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0
    rr_ok = rr >= required_rr and sl_dist > 0 and tp_dist > 0
    mm_ok = effective_qty <= qty_rec and not warnings and effective_qty > 0
    risk_exceeded = actual_risk_pct > risk_pct + 1e-9

    # ─────────────────────────────────────────────────────────────
    # УНИВЕРСАЛЬНАЯ ЛОГИКА РАСПРЕДЕЛЕНИЯ ОБЪЁМА (прогрессия 1-2-6)
    # ─────────────────────────────────────────────────────────────
    # Определяем количество активных точек входа (с введённой ценой)
    active_entry_points = sum(1 for e in entries if e["price"] is not None)
    active_entry_points = max(1, min(3, active_entry_points))

    # Выбираем прогрессию в зависимости от количества точек
    ratio = VOLUME_RATIO[active_entry_points]
    ratio_sum = sum(ratio)

    # Рассчитываем рекомендуемое распределение от qty_rec
    base_lots = max(1, qty_rec // ratio_sum)
    rec_qtys = [base_lots * r for r in ratio]
    # Дополняем до 3 элементов для единообразия отображения
    while len(rec_qtys) < 3:
        rec_qtys.append(0)

    rec_total = sum(rec_qtys)

    # В режиме ЗЛ автоподставляем цену ЗЛ во вторую точку, если она пуста
    if use_zl_mode and zl_valid and zl_price is not None:
        if st.session_state.get("entry_p_1") is None:
            st.session_state["entry_p_1"] = float(zl_price)

    # Вывод рекомендации по распределению объёма
    if qty_rec > 0:
        entry_labels = ["Твх 1 (у точки А)"]
        if active_entry_points >= 2:
            entry_labels.append("Твх 2 (на уровне ЗЛ)" if use_zl_mode else "Твх 2 (усреднение)")
        if active_entry_points >= 3:
            entry_labels.append("Твх 3 (глубокое усреднение)")

        chips_parts = []
        for i, (label, qty) in enumerate(zip(entry_labels, rec_qtys[:active_entry_points])):
            chips_parts.append(
                f'<span style="background: #fff; padding: 4px 8px; border-radius: 4px; '
                f'border: 1px solid #EBC98A; white-space: nowrap;">'
                f'<b>{label}:</b> {qty} конт.'
                f'</span>'
            )
        chips_parts.append(
            f'<span style="background: #E3E9FE; padding: 4px 8px; border-radius: 4px; '
            f'border: 1px solid #B9C6F7; color: #1E3A8A; white-space: nowrap;">'
            f'<b>Итого в серии:</b> {rec_total} из {qty_rec} допустимых'
            f'</span>'
        )

        mode_label = "🎯 ЗАХВАТ ЛИКВИДНОСТИ" if use_zl_mode else "📊 УСРЕДНЕНИЕ"
        ratio_str = " / ".join(str(r) for r in ratio)

        st.markdown(
            f"""
            <div style="background: var(--zl-soft); border: 1px solid #C4B5FD; border-radius: 8px; padding: 10px; margin: 10px 0;">
                <div style="font-size: 0.75rem; font-weight: 700; color: #5B21B6; margin-bottom: 6px; font-family: var(--mono);">
                    {mode_label} · прогрессия [{ratio_str}]
                </div>
                <div style="display: flex; gap: 8px; flex-wrap: wrap; font-family: var(--mono); font-size: 0.8rem;">
                    {"".join(chips_parts)}
                </div>
                <div style="font-size: 0.65rem; color: #7C3AED; margin-top: 6px; font-style: italic;">
                    * Вы можете вручную изменить количество контрактов в полях выше. Калькулятор пересчитает среднюю цену и риск.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    st.markdown(
        f"""
<div class="strip tint">
  <div class="cell"><span class="k">RR (грязный)</span><span class="v" style="color:{'#046C4E' if rr_ok else '#B01B1B'}">{fmt_n(rr, 2)}</span></div>
  <div class="cell"><span class="k">RR (чистый)</span><span class="v" style="color:{'#046C4E' if net_rr >= required_rr else '#B01B1B'}">{fmt_n(net_rr, 2)}</span></div>
  <div class="cell"><span class="k">Реком. объём</span><span class="v" style="color:#1E3A8A">{fmt_n(qty_rec, 0)}</span></div>
</div>
<div class="chips">
  <span class="chip info">комиссия за круг: {fmt_n(total_commission, 2)} ₽</span>
  <span class="chip {'ok' if rr_ok else 'bad'}">грязный RR {fmt_n(rr, 2)} {'≥' if rr_ok else '&lt;'} {fmt_n(required_rr, 2)}</span>
  <span class="chip {'ok' if net_rr >= required_rr else 'bad'}">чистый RR {fmt_n(net_rr, 2)} {'≥' if net_rr >= required_rr else '&lt;'} {fmt_n(required_rr, 2)}</span>
  <span class="chip {'ok' if mm_ok else 'bad'}">MM {fmt_n(effective_qty, 0)} {'≤' if mm_ok else '&gt;'} {fmt_n(qty_rec, 0)}</span>
</div>
""",
        unsafe_allow_html=True,
    )

    if warnings:
        st.error("Некорректные цены сделки — исправьте уровни выше.")
    elif not mm_ok:
        st.error(f"Объём {fmt_n(effective_qty, 0)} конт. превышает рекомендуемые {fmt_n(qty_rec, 0)}.")
    elif not rr_ok:
        ratio_rr = rr / required_rr if required_rr else 0
        safe_qty = int(math.floor(qty_rec * ratio_rr))
        if safe_qty < 1:
            st.warning("RR слишком низкий — сделку рекомендуется пропустить.")
        elif effective_qty <= safe_qty:
            st.info(f"RR ниже плана, но объём {fmt_n(effective_qty, 0)} ≤ безопасного {fmt_n(safe_qty, 0)}.")
        else:
            st.warning(
                f"Сократите позицию до {fmt_n(safe_qty, 0)} конт. — "
                f"риск снизится до {fmt_n(risk_per_ct * safe_qty, 2)} ₽."
            )

    with st.expander("Детали расчёта", expanded=False):
        risk_style = 'style="color:#B01B1B;font-weight:700"' if risk_exceeded else ""
        reward_style = 'style="color:#046C4E;font-weight:700"'
        zone_block = ""
        if manual_ab and entry_info:
            z1 = sorted([point_a, entry_info["p1"]])
            z2 = sorted([entry_info["p1"], entry_info["p2"]])
            z3 = sorted([entry_info["p2"], point_b])
            zone_block = f"""
**Зоны A-B (введённые значения)**
- Зона 1 · у A: `{fmt_px(z1[0], dec)}` — `{fmt_px(z1[1], dec)}`
- Зона 2 · середина: `{fmt_px(z2[0], dec)}` — `{fmt_px(z2[1], dec)}`
- Зона 3 · у B: `{fmt_px(z3[0], dec)}` — `{fmt_px(z3[1], dec)}`
- Вход в зоне: **{entry_zone if entry_zone else 'вне диапазона'}** · коэффициент объёма **×{fmt_n(zone_reduction, 2)}**
"""
        level_block = ""
        for s in setups:
            stt = level_state(inst.price, s)
            if stt == "none":
                continue
            tag = {"reached": "ОТРАБОТАН", "armed": "ожидание", "expired": "снят (цена за A)"}[stt]
            move_text = f" · движение: {s['move']}" if s.get("move") else ""
            level_block += (
                f"- {s['dir']}: уровень `{fmt_px(s['level'], dec)}` — **{tag}**{move_text} "
                f"(цена `{fmt_px(inst.price, dec)}`)\n"
            )
        if level_block:
            level_block = "**Уровни из таблицы**\n" + level_block

        tariff_display = resolved_tariff if tariff_found else (
            f"{tariff_name_raw} (дефолт {fmt_n(DEFAULT_COMMISSION_PCT, 3)}%)"
            if tariff_name_raw else f"дефолт {fmt_n(DEFAULT_COMMISSION_PCT, 3)}%"
        )

        zl_block = ""
        if use_zl_mode and zl_price is not None:
            zl_block = f"**Захват ликвидности:** уровень ЗЛ = `{fmt_px(zl_price, dec)}`\n"

        st.markdown(
            f"""
**Вход:** `{fmt_px(avg_entry, dec)}` · **Контрактов:** `{fmt_n(effective_qty, 0)}` · **Направление:** `{direction}`

**SL:** `{fmt_px(sl_dist, dec)}` пт = `{fmt_n(sl_steps, 2)}` шагов × `{fmt_n(inst.step_price, 2)}` ₽ = **{fmt_n(risk_per_ct, 2)} ₽ / конт.**
**TP:** `{fmt_px(tp_dist, dec)}` пт = `{fmt_n(tp_steps, 2)}` шагов × `{fmt_n(inst.step_price, 2)}` ₽ = **{fmt_n(profit_per_ct, 2)} ₽ / конт.**

**Бюджет риска:** `{fmt_n(deposit, 2)} ₽ × {fmt_n(risk_pct, 2)}%` = **{fmt_n(risk_budget, 2)} ₽**
**Qty по риску:** `{fmt_n(qty_by_risk, 0)}` · **Qty по ГО ({margin_type}):** `{fmt_n(qty_by_margin, 0)}` · **База:** `{fmt_n(qty_base, 0)}`
**Рекомендуемый объём:** `{fmt_n(qty_base, 0)} × {fmt_n(zone_reduction, 2)}` = **{fmt_n(qty_rec, 0)} конт.**

**Комиссия (тариф: {tariff_display})**
- Оборот одной стороны: `{fmt_n(turnover_one_way, 0)} ₽` = `{fmt_px(avg_entry, dec)}` × `{fmt_n(inst.point_value, 2)}` × `{fmt_n(effective_qty, 0)}`
- Комиссия за сторону: `max({fmt_n(turnover_one_way, 0)} × {fmt_n(comm_pct, 3)}%, {fmt_n(comm_min, 2)})` = **{fmt_n(commission_one_way, 2)} ₽**
- **Комиссия за круг (вход + выход):** **{fmt_n(total_commission, 2)} ₽**

**Итог на {fmt_n(effective_qty, 0)} конт.**
- Грязная прибыль при TP: <span {reward_style}>+{fmt_n(gross_reward, 2)} ₽</span>
- Грязный убыток при SL: <span {risk_style}>−{fmt_n(gross_risk, 2)} ₽</span>
- **Чистая прибыль при TP: <span {reward_style}>+{fmt_n(net_reward, 2)} ₽</span>**
- **Чистый убыток при SL: <span {risk_style}>−{fmt_n(net_risk, 2)} ₽</span>**
- Чистый RR: **{fmt_n(net_rr, 2)}**
- Фактический риск (чистый): <span {risk_style}>{fmt_n(actual_risk_pct, 2)}%</span> от депозита (лимит {fmt_n(risk_pct, 2)}%){' — **ПРЕВЫШЕНИЕ ЛИМИТА**' if risk_exceeded else ''}
- Загрузка ГО: `{fmt_n(margin * effective_qty, 0)} ₽` ({fmt_n((margin * effective_qty / deposit * 100) if deposit else 0, 2)}% депозита)

{zl_block}
{zone_block}
{level_block}
*ГО: таблица клиринга MOEX ISS, категория `{margin_type}`. Точность цены: {dec} знак(ов) — поле DECIMALS спецификации ISS.*
""",
            unsafe_allow_html=True,
        )
