"""
Калькулятор фьючерсов MOEX — Strict Mobile Version + мониторинг уровней и сводка по зонам.
Автоматически подгружает справочные данные из Google Sheets (Тикер, Тикер TW, A, B, Уровень).
Зависимости: streamlit, pandas, requests.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from io import StringIO

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter, Retry

# ─────────────────────────────────────────────────────────────
# КОНСТАНТЫ
# ─────────────────────────────────────────────────────────────
ISS = "https://iss.moex.com/iss/engines/futures/markets/forts"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1cHQnG1M7gQI5BpL9W0ijWgK-AI38NleQW_DOxccLDm0/export?format=csv"

MARGIN_LEVELS = ["НАЧ. ГО", "КНУР", "КСУР", "КПУР", "КОУР"]
MARGIN_HINTS = {
    "НАЧ. ГО": "НАЧ. ГО — базовое гарантийное обеспечение",
    "КНУР":    "КНУР — начальный уровень риска (LRC)",
    "КСУР":    "КСУР — стандартный уровень риска (MRC)",
    "КПУР":    "КПУР — повышенный уровень риска (HRC)",
    "КОУР":    "КОУР — особый уровень риска (ERC)",
}

IMS_MARGIN_MAP = {
    "НАЧ. ГО": ("IM_BUY",      "IM_SELL"),
    "КНУР":    ("IM_BUY_LRC",  "IM_SELL_LRC"),
    "КСУР":    ("IM_BUY_MRC",  "IM_SELL_MRC"),
    "КПУР":    ("IM_BUY_HRC",  "IM_SELL_HRC"),
    "КОУР":    ("IM_BUY_ERC",  "IM_SELL_ERC"),
}

TRADE_STATE_KEYS = [
    "sl_input", "tp_input",
    "entry_p_0", "entry_q_0",
    "entry_p_1", "entry_q_1",
    "entry_p_2", "entry_q_2",
    "point_a_input", "point_b_input",
]

# Маркеры сводки
DIR_EMOJI = {"LONG": "🟩", "SHORT": "🟥"}
ZONE_EMOJI = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣"}
ZONE_TXT = {1: "Зона 1 (идеальная)", 2: "Зона 2 (повышенный риск)", 3: "Зона 3 (высокий риск)"}
BELL = "🔔"


# ─────────────────────────────────────────────────────────────
# ПАРСИНГ И ФОРМАТИРОВАНИЕ
# ─────────────────────────────────────────────────────────────
def parse_number(val) -> float | None:
    """Понимает 'р.69 000,00', '1 234,56', '105.25', узкие пробелы Google Sheets."""
    if val is None:
        return None
    if isinstance(val, float) and math.isnan(val):
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


def fmt_n(v, decimals: int = 0) -> str:
    """Деньги и количества — без лишних нулей."""
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


def fmt_px(v, decimals: int = 2) -> str:
    """Цены — ровно столько знаков, сколько требует спецификация ISS (DECIMALS)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(v):
        return "—"
    d = max(0, min(int(decimals), 8))
    return f"{v:.{d}f}".replace(".", ",")


def fmt_date(s) -> str:
    try:
        s = str(s).strip()
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return str(s) if s else "—"


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


# ─────────────────────────────────────────────────────────────
# ЗОНЫ A-B (зона 1 всегда у точки A, зона 3 всегда у точки B)
# ─────────────────────────────────────────────────────────────
def determine_zone(price: float | None, a: float | None, b: float | None) -> tuple[int | None, tuple | None]:
    if price is None or a is None or b is None or a == b:
        return None, None

    step = (b - a) / 3.0
    p1 = a + step
    p2 = a + 2.0 * step

    z1 = (min(a, p1), max(a, p1))
    z2 = (min(p1, p2), max(p1, p2))
    z3 = (min(p2, b), max(p2, b))
    boundaries = (z1[0], z1[1], z2[0], z2[1], z3[0], z3[1])

    lo, hi = min(a, b), max(a, b)
    if price < lo or price > hi:
        return None, boundaries

    if a < b:                      # LONG: A снизу
        z = 1 if price <= p1 else (2 if price <= p2 else 3)
    else:                          # SHORT: A сверху
        z = 1 if price >= p1 else (2 if price >= p2 else 3)
    return z, boundaries


def level_state(price: float | None, setup: dict) -> str:
    """
    none    — данных нет
    reached — LONG: цена <= уровня; SHORT: цена >= уровня
    expired — цена ушла за точку A (LONG: ниже A, SHORT: выше A) → уровень снят
    armed   — уровень ждёт отработки
    """
    lvl = setup.get("level")
    a = setup.get("a")
    d = setup.get("dir")
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
# ЗАГРУЗКА ДАННЫХ ИЗ GOOGLE SHEETS
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def load_sheet_data() -> tuple[float, dict]:
    default_deposit = 100000.0
    ticker_map: dict[str, list[dict]] = {}

    try:
        df = pd.read_csv(SHEET_URL, header=None, dtype=str, keep_default_na=False)
    except Exception as e:
        st.warning(f"Не удалось загрузить данные из Google Sheets: {e}")
        return default_deposit, ticker_map

    if df.empty:
        return default_deposit, ticker_map

    # Депозит — ячейка B1
    if df.shape[1] > 1:
        val = parse_number(df.iloc[0, 1])
        if val is not None and val > 0:
            default_deposit = val

    # Резерв: слово «Депозит» в любой из первых колонок
    if default_deposit == 100000.0:
        for col_idx in range(min(4, df.shape[1])):
            hit = df[df[col_idx].map(_norm).str.contains("депозит", na=False)]
            if not hit.empty and col_idx + 1 < df.shape[1]:
                val = parse_number(df.iloc[hit.index[0], col_idx + 1])
                if val is not None and val > 0:
                    default_deposit = val
                    break

    # Строка заголовков
    hdr = None
    for i in range(min(30, len(df))):
        if _norm(df.iloc[i, 0]) in {"тикер", "ticker", "secid"}:
            hdr = i
            break
    if hdr is None:
        return default_deposit, ticker_map

    names = [_norm(x) for x in df.iloc[hdr].tolist()]

    def find_col(pred, fallback):
        for i, n in enumerate(names):
            if pred(n):
                return i
        return fallback

    c_t = find_col(lambda n: n in {"тикер", "ticker", "secid"}, 0)
    c_tw = find_col(lambda n: "tw" in n or "tradingview" in n, 1)
    c_a = find_col(lambda n: n in {"a", "а", "точка a", "точка а"}, 2)
    c_b = find_col(lambda n: n in {"b", "в", "точка b", "точка в"}, 3)
    c_l = find_col(lambda n: "уровень" in n or "level" in n or n in {"ур", "ур.", "lvl"}, None)

    def cell(row, idx):
        if idx is None or idx >= len(row):
            return None
        return row.iloc[idx]

    for i in range(hdr + 1, len(df)):
        row = df.iloc[i]
        t = str(cell(row, c_t) or "").strip().upper()
        if not t or _norm(t) in {"тикер", "ticker", "nan", "итого"}:
            continue

        tw_val = str(cell(row, c_tw) or "").strip()
        pt_a = parse_number(cell(row, c_a))
        pt_b = parse_number(cell(row, c_b))
        pt_lvl = parse_number(cell(row, c_l))

        direction = None
        if pt_a is not None and pt_b is not None and pt_a != pt_b:
            direction = "LONG" if pt_a < pt_b else "SHORT"

        setup = {"tw": tw_val, "a": pt_a, "b": pt_b, "dir": direction, "level": pt_lvl}
        bucket = ticker_map.setdefault(t, [])
        if setup not in bucket:
            bucket.append(setup)

    return default_deposit, ticker_map


# ─────────────────────────────────────────────────────────────
# HTTP-СЕССИЯ И MOEX ISS
# ─────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "moex-futures-calc/8.0"})
    s.mount(
        "https://",
        HTTPAdapter(
            max_retries=Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504)),
            pool_maxsize=8,
        ),
    )
    return s


@st.cache_data(ttl=300, show_spinner=False)
def fetch_futures() -> pd.DataFrame:
    try:
        r = _session().get(f"{ISS}/securities.json", params={"iss.meta": "off", "iss.only": "securities,marketdata"}, timeout=30)
        r.raise_for_status()
        js = r.json()
        sec = pd.DataFrame(js["securities"]["data"], columns=js["securities"]["columns"])
        mkt = pd.DataFrame(js["marketdata"]["data"], columns=js["marketdata"]["columns"])
    except Exception as e:
        st.error(f"MOEX ISS недоступен: {e}")
        return pd.DataFrame()

    sec = sec.drop_duplicates("SECID", keep="first")
    mkt = mkt.drop_duplicates("SECID", keep="first")
    df = sec.merge(mkt, on="SECID", how="left")

    for col in ("MINSTEP", "STEPPRICE", "INITIALMARGIN", "LAST", "PREVSETTLEPRICE", "DECIMALS"):
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    df = df[(df["MINSTEP"] > 0) & (df["STEPPRICE"] > 0)].copy()
    df["PRICE"] = df["LAST"].where(df["LAST"] > 0, df["PREVSETTLEPRICE"])
    return df


@st.cache_data(ttl=900, show_spinner=False)
def fetch_ims() -> pd.DataFrame:
    try:
        r = _session().get(f"{ISS}/ims.json", params={"iss.meta": "off"}, timeout=15)
        r.raise_for_status()
        js = r.json()
        block = js.get("ims") or {}
        df = pd.DataFrame(block.get("data") or [], columns=block.get("columns") or [])
        if not df.empty:
            df.columns = [str(c).upper().strip() for c in df.columns]
            if "BOARDID" in df.columns:
                df = df[df["BOARDID"] == "RFUD"].copy()
            if "SECID" in df.columns:
                df = df.drop_duplicates("SECID", keep="first").copy()
            for c in df.columns:
                if c.startswith("IM_") and c != "IM_TIME":
                    df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
            return df
    except Exception:
        pass

    try:
        r = _session().get(f"{ISS}/ims", params={"iss.meta": "off"}, timeout=15)
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text), sep="|")
        if not df.empty:
            df.columns = [str(c).upper().strip() for c in df.columns]
            if "BOARDID" in df.columns:
                df = df[df["BOARDID"] == "RFUD"].copy()
            if "SECID" in df.columns:
                df = df.drop_duplicates("SECID", keep="first").copy()
            for c in df.columns:
                if c.startswith("IM_") and c != "IM_TIME":
                    df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
            return df
    except Exception:
        st.warning("Не удалось загрузить точные ГО. Используются базовые значения.")
        return pd.DataFrame()


def merge_margins(futures: pd.DataFrame, ims: pd.DataFrame, margin_type: str) -> pd.DataFrame:
    df = futures.copy()
    base_margin = df["INITIALMARGIN"].fillna(0.0)
    df["GO_LONG"] = base_margin
    df["GO_SHORT"] = base_margin

    cols = IMS_MARGIN_MAP.get(margin_type)
    if cols and not ims.empty and "SECID" in ims.columns:
        col_b, col_s = cols
        if col_b in ims.columns and col_s in ims.columns:
            margin_map = ims.set_index("SECID")
            df["GO_LONG"] = df["SECID"].map(margin_map[col_b]).fillna(base_margin)
            df["GO_SHORT"] = df["SECID"].map(margin_map[col_s]).fillna(base_margin)

    df["GO_LONG"] = df["GO_LONG"].where(df["GO_LONG"] > 0, base_margin)
    df["GO_SHORT"] = df["GO_SHORT"].where(df["GO_SHORT"] > 0, base_margin)
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


def _decimals(step: float) -> int:
    if step <= 0:
        return 2
    return max(0, min(6, int(math.ceil(-math.log10(step) - 1e-9))))


def _to_instrument(row: pd.Series) -> Instrument:
    d = int(row.get("DECIMALS", 0) or 0)
    if d <= 0:
        d = _decimals(float(row["MINSTEP"]))
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
# СВОДКА ПО АКТИВАМ ТАБЛИЦЫ
# ─────────────────────────────────────────────────────────────
def build_summary(instruments: dict, ticker_map: dict) -> list[dict]:
    out: list[dict] = []
    for t, setups in ticker_map.items():
        ins = instruments.get(t)
        if ins is None:
            continue
        for s in setups:
            if s.get("dir") is None:
                continue
            z, _ = determine_zone(ins.price, s["a"], s["b"])
            out.append({
                "ticker": t,
                "tw": s.get("tw", ""),
                "dir": s["dir"],
                "zone": z,
                "price": ins.price,
                "decimals": ins.decimals,
                "a": s["a"],
                "b": s["b"],
                "level": s.get("level"),
                "state": level_state(ins.price, s),
            })
    return out


def sort_summary(items: list[dict], mode: str) -> list[dict]:
    rank = {"reached": 0, "armed": 1, "none": 2, "expired": 3}
    if mode == "По тикеру":
        return sorted(items, key=lambda x: (x["ticker"], x["dir"]))
    if mode == "По зоне":
        return sorted(items, key=lambda x: (x["zone"] is None, x["zone"] or 9, x["ticker"], x["dir"]))
    return sorted(items, key=lambda x: (rank.get(x["state"], 9), x["zone"] is None, x["zone"] or 9, x["ticker"], x["dir"]))


# ─────────────────────────────────────────────────────────────
# СТРАНИЦА И CSS
# ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Калькулятор фьючерсов MOEX",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    @media (max-width: 640px) {
        .block-container { padding-top: 0.5rem !important; padding-bottom: 0.5rem !important; }
        .stApp > header { display: none; }
        .mobile-col-3 > div { width: 33.33% !important; flex: 0 0 33.33% !important; padding: 0 2px !important; }
        .mobile-col-2 > div { width: 50% !important; flex: 0 0 50% !important; padding: 0 2px !important; }
        .stNumberInput { min-height: 2.2rem !important; }
        .stNumberInput label { display: none !important; }
        .stNumberInput input { height: 2.2rem !important; font-size: 14px !important; }
        .stRadio > div { flex-direction: row !important; gap: 8px !important; }
        .stRadio label { font-size: 13px !important; padding: 4px 8px !important; }
        .streamlit-expanderHeader { font-size: 13px !important; padding: 0.3rem 0 !important; }
        .stButton > button { padding: 0.3rem 0.5rem !important; font-size: 13px !important; }
        .stSidebar h3 { font-size: 1rem !important; margin-bottom: 0.5rem !important; margin-top: 0.5rem !important; }
        .stSidebar p { font-size: 0.8rem !important; color: #666 !important; margin-bottom: 0.8rem !important; }
    }

    .stRadio div[role="radiogroup"] > label:has(> input[value="LONG"]) {
        background-color: #e6f4ea !important; border: 2px solid #34a853 !important;
        color: #137333 !important; font-weight: 600 !important; border-radius: 6px !important; padding: 8px 16px !important;
    }
    .stRadio div[role="radiogroup"] > label:has(> input[value="SHORT"]) {
        background-color: #fce8e6 !important; border: 2px solid #ea4335 !important;
        color: #c5221f !important; font-weight: 600 !important; border-radius: 6px !important; padding: 8px 16px !important;
    }
    .stRadio div[role="radiogroup"] > label:has(> input[value="LONG"]:checked) {
        background-color: #34a853 !important; color: white !important;
    }
    .stRadio div[role="radiogroup"] > label:has(> input[value="SHORT"]:checked) {
        background-color: #ea4335 !important; color: white !important;
    }

    .compact-metrics {
        display: flex; justify-content: space-between; align-items: center;
        background: #f0f2f6; padding: 8px 12px; border-radius: 8px;
        font-size: 0.85rem; margin-bottom: 8px; border: 1px solid #e0e0e0;
    }
    .compact-metrics .item { text-align: center; flex: 1; }
    .compact-metrics .item b { display: block; font-size: 0.7rem; color: #666; text-transform: uppercase; }
    .compact-metrics .item span { font-size: 0.95rem; font-weight: 600; color: #0e1117;
        font-variant-numeric: tabular-nums; }

    .zone-indicator {
        padding: 8px 12px; border-radius: 6px; margin: 8px 0;
        font-size: 0.85rem; font-weight: 600;
    }
    .zone-1 { background: #e6f4ea; border: 1px solid #34a853; color: #137333; }
    .zone-2 { background: #fef7e0; border: 1px solid #f9ab00; color: #b06000; }
    .zone-3 { background: #fce8e6; border: 1px solid #ea4335; color: #c5221f; }
    .zone-0 { background: #f1f3f5; border: 1px solid #ced4da; color: #495057; }

    .sidebar-ref {
        background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 4px;
        padding: 6px 8px; margin-top: 4px; margin-bottom: 8px;
    }
    .sidebar-ref-item {
        font-size: 0.75rem; color: #495057; padding: 2px 0;
        border-bottom: 1px dashed #dee2e6; font-variant-numeric: tabular-nums;
    }
    .sidebar-ref-item:last-child { border-bottom: none; }
    .sidebar-ref-dir { font-weight: 700; }
    .dir-long { color: #137333; }
    .dir-short { color: #c5221f; }
    .ref-lvl { color: #8a5308; font-weight: 600; }

    /* ── сводка по активам ── */
    .sum-head { display: flex; gap: 5px; align-items: center; flex-wrap: wrap; margin: 2px 0 5px; }
    .sum-badge {
        font-size: 0.65rem; font-weight: 700; border-radius: 999px; padding: 2px 8px;
        background: #e9ecef; color: #343a40; border: 1px solid #dee2e6;
        font-variant-numeric: tabular-nums;
    }
    .sum-badge.hot { background: #fef7e0; border-color: #f0c36d; color: #8a5308; }

    .summary-list {
        background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 6px;
        padding: 3px 5px; margin: 4px 0 8px; max-height: 260px; overflow-y: auto;
        overscroll-behavior: contain;
        scrollbar-width: thin; scrollbar-color: #adb5bd #e9ecef;
    }
    .summary-list::-webkit-scrollbar { width: 8px; }
    .summary-list::-webkit-scrollbar-track { background: #e9ecef; border-radius: 8px; }
    .summary-list::-webkit-scrollbar-thumb {
        border-radius: 8px; background: linear-gradient(180deg, #4c6ef5, #34a853);
    }
    .summary-list::-webkit-scrollbar-thumb:hover { background: #4c6ef5; }

    .summary-item {
        display: flex; align-items: center; gap: 5px; padding: 4px 3px;
        border-bottom: 1px dashed #dee2e6; font-size: 0.72rem; line-height: 1.15;
    }
    .summary-item:last-child { border-bottom: none; }
    .summary-item.hot {
        background: linear-gradient(96deg, #fef7e0, rgba(254,247,224,0));
        border-radius: 4px; border-bottom-color: #f0c36d;
    }
    .summary-item .em { font-size: 0.74rem; }
    .summary-item .tk { font-weight: 700; color: #212529; letter-spacing: .2px; }
    .summary-item .tw {
        font-size: 0.62rem; color: #868e96; flex: 1; min-width: 0;
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .summary-item .zn { font-size: 0.74rem; }
    .summary-item .bell {
        font-size: 0.6rem; font-weight: 700; color: #8a5308; background: #fdecc8;
        border: 1px solid #f0c36d; border-radius: 4px; padding: 0 4px; white-space: nowrap;
        font-variant-numeric: tabular-nums;
    }
    .summary-item .off {
        font-size: 0.58rem; color: #868e96; background: #e9ecef;
        border-radius: 4px; padding: 0 4px;
    }
    .sum-empty { font-size: 0.7rem; color: #868e96; padding: 6px 4px; }

    /* ── строки уровня в калькуляторе ── */
    .lvl-line {
        font-size: 0.74rem; border-radius: 6px; padding: 5px 9px; margin: 4px 0;
        border: 1px solid #e0e0e0; background: #f8f9fa; color: #343a40;
        font-variant-numeric: tabular-nums;
    }
    .lvl-line.hit {
        background: #fef7e0; border: 1px solid #f0c36d; border-left: 4px solid #f9ab00;
        color: #8a5308; font-weight: 700;
    }
    .lvl-line.off { color: #868e96; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ─────────────────────────────────────────────────────────────
# ИНИЦИАЛИЗАЦИЯ SESSION STATE
# ─────────────────────────────────────────────────────────────
sheet_deposit, sheet_ticker_map = load_sheet_data()

if "deposit_initialized" not in st.session_state:
    st.session_state.deposit_input = sheet_deposit
    st.session_state.deposit_initialized = True

if "margin_type" not in st.session_state:
    st.session_state.margin_type = "КСУР"


# ─────────────────────────────────────────────────────────────
# ЗАГРУЗКА ДАННЫХ MOEX (ДО САЙДБАРА)
# ─────────────────────────────────────────────────────────────
st.markdown("<h4 style='margin: 0 0 8px 0;'>Калькулятор фьючерсов MOEX</h4>", unsafe_allow_html=True)

with st.spinner("Загрузка данных..."):
    futures = fetch_futures()
    ims = fetch_ims()

if futures.empty:
    st.error("Не удалось загрузить список фьючерсов с MOEX ISS.")
    st.stop()

margin_type = st.session_state.margin_type
futures = merge_margins(futures, ims, margin_type)
futures = futures[(futures["PRICE"] > 0) & ((futures["GO_LONG"] > 0) | (futures["GO_SHORT"] > 0))].copy()

if futures.empty:
    st.warning("Нет контрактов с валидными ценой и ГО.")
    st.stop()

futures = futures.sort_values("SECID").reset_index(drop=True)
instruments: dict[str, Instrument] = {row["SECID"]: _to_instrument(row) for _, row in futures.iterrows()}

# Сводка и множество тикеров со сработавшим уровнем
summary_all = build_summary(instruments, sheet_ticker_map)
hot_tickers = {x["ticker"] for x in summary_all if x["state"] == "reached"}
hot_count = sum(1 for x in summary_all if x["state"] == "reached")
has_levels = any(s.get("level") is not None for ss in sheet_ticker_map.values() for s in ss)


def format_ticker(t: str) -> str:
    setups = sheet_ticker_map.get(t, [])
    tw = ""
    for s in setups:
        if s.get("tw"):
            tw = s["tw"]
            break
    base = f"{t} ({tw})" if tw else t
    return f"{base} {BELL}" if t in hot_tickers else base


ticker = st.selectbox(
    "Фьючерс", list(instruments.keys()),
    format_func=format_ticker,
    label_visibility="collapsed",
    key="ticker_select"
)

if "last_ticker" not in st.session_state:
    st.session_state.last_ticker = ticker
elif st.session_state.last_ticker != ticker:
    for key in TRADE_STATE_KEYS:
        if key in st.session_state:
            del st.session_state[key]
    st.session_state.last_ticker = ticker

inst = instruments[ticker]
setups = sheet_ticker_map.get(ticker, [])


# ─────────────────────────────────────────────────────────────
# САЙДБАР (ПОСЛЕ ЗАГРУЗКИ inst)
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Депозит и риск на сделку")
    st.markdown("Основные параметры для расчёта объёма позиции.")

    deposit = st.number_input(
        "Депозит, руб.", min_value=0.0,
        step=1000.0, format="%.0f", key="deposit_input"
    )

    st.markdown(
        f"<div class='sidebar-ref'><div class='sidebar-ref-item'>Из таблицы (B1): <b>{fmt_n(sheet_deposit, 0)} руб.</b></div></div>",
        unsafe_allow_html=True
    )

    risk_pct = st.number_input("Риск на сделку, %", min_value=0.1, max_value=100.0, value=1.0, step=0.1)

    st.divider()

    st.header("Лимиты загрузки и RR")
    st.markdown("Контроль нагрузки на депозит и минимальное соотношение риск/прибыль.")

    margin_util = st.number_input("Макс. загрузка ГО, %", min_value=10, max_value=100, value=100, step=10)
    required_rr = st.number_input("Требуемый RR", min_value=0.5, max_value=20.0, value=3.0, step=0.5)

    margin_type = st.selectbox(
        "Категория риска (ГО)",
        MARGIN_LEVELS,
        index=MARGIN_LEVELS.index(st.session_state.margin_type),
        format_func=lambda x: MARGIN_HINTS.get(x, x),
        key="margin_type"
    )

    st.divider()

    # ── НОВОЕ: СВОДКА ПО АКТИВАМ ТАБЛИЦЫ ──
    st.header("Сводка по активам")
    st.markdown("Зона текущей цены по A/B из таблицы. 🟩 LONG · 🟥 SHORT · 1️⃣2️⃣3️⃣ — номер зоны.")

    st.markdown(
        f"<div class='sum-head'>"
        f"<span class='sum-badge'>{len({x['ticker'] for x in summary_all})} активов</span>"
        f"<span class='sum-badge{' hot' if hot_count else ''}'>{BELL} {hot_count} сработало</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    fc, sc = st.columns([1, 1], gap="small")
    only_hot = fc.checkbox("Только уровни", key="sum_only_hot")
    sort_mode = sc.selectbox(
        "Сортировка сводки", ["По сигналу", "По зоне", "По тикеру"],
        key="sum_sort", label_visibility="collapsed",
    )

    rows_data = [x for x in summary_all if x["state"] == "reached"] if only_hot else summary_all
    rows_data = sort_summary(rows_data, sort_mode)

    if rows_data:
        rows_html = []
        for x in rows_data:
            zn = ZONE_EMOJI.get(x["zone"], "▫️")
            tw = f"<span class='tw'>{x['tw']}</span>" if x["tw"]
