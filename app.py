# app.py — Калькулятор фьючерсов MOEX · монитор уровней и зон A/B · с учётом комиссий
from __future__ import annotations

import math
import re
import time
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
# Отдельный URL для листа "Комиссии" (gviz/tq позволяет указать имя листа)
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

# Дефолтная комиссия (если тариф не найден в таблице)
DEFAULT_COMMISSION_PCT = 0.05
DEFAULT_MIN_COMMISSION = 2.0


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
    """Возвращает: (депозит, маппинг тикеров, мета, название тарифа из E1)."""
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

    # Депозит — B1
    if raw.shape[1] > 1:
        v = parse_number(raw.iloc[0, 1])
        if v is not None and v > 0:
            deposit = v

    # Резерв: слово «Депозит»
    if deposit == 100_000.0:
        for c in range(min(4, raw.shape[1])):
            hit = raw[raw[c].map(_norm).str.contains("депозит", na=False)]
            if not hit.empty and c + 1 < raw.shape[1]:
                v = parse_number(raw.iloc[hit.index[0], c + 1])
                if v is not None and v > 0:
                    deposit = v
                    break

    # Тариф из E1 (строка 0, столбец 4)
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
    """
    Возвращает:
      - tariff_book: dict[name] = {"broker": str, "pct": float, "min": float}
      - meta: {"ok": bool, "error": str|None, "ts": float}
    """
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

    # Нормализуем названия колонок
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
    """
    Возвращает (pct, min_commission, resolved_name, found).
    Если тариф не найден — возвращает дефолтные значения.
    """
    if not tariff_name:
        return DEFAULT_COMMISSION_PCT, DEFAULT_MIN_COMMISSION, "", False
    # Точное совпадение
    if tariff_name in book:
        rec = book[tariff_name]
        return rec["pct"], rec["min"], tariff_name, True
    # Нечувствительное к регистру
    key = tariff_name.lower()
    for name, rec in book.items():
        if name.lower() == key:
            return rec["pct"], rec["min"], name, True
    return DEFAULT_COMMISSION_PCT, DEFAULT_MIN_COMMISSION, tariff_name, False


def calc_commission(turnover: float, pct: float, min_comm: float) -> float:
    """Комиссия за одну сторону сделки."""
    if turnover <= 0:
        return 0.0
    return max(turnover * pct / 100.0, min_comm)


# ─────────────────────────────────────────────────────────────
# MOEX ISS
# ─────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "moex-futures-desk/15.0"})
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
  background:linear-gradient(100deg
