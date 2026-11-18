"""
Калькулятор фьючерсов MOEX — Strict Mobile Version (Fixed Deposit Parsing).
Автоматически подгружает справочные данные из Google Sheets.
Зависимости: streamlit, pandas, requests.
"""
from __future__ import annotations

import math
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


# ─────────────────────────────────────────────────────────────
# ПАРСИНГ ЧИСЕЛ
# ─────────────────────────────────────────────────────────────
def parse_number(val) -> float | None:
    """Универсальный парсинг чисел из ячеек таблицы (учитывает пробелы, запятые, валюту)."""
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return None
    # Убираем валютные символы, пробелы, меняем запятую на точку
    s = s.replace("р.", "").replace("руб.", "").replace("₽", "").replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────
# ЗАГРУЗКА ДАННЫХ ИЗ GOOGLE SHEETS
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def load_sheet_data() -> tuple[float, dict]:
    """Загружает депозит из B1 и список всех сетапов (A, B) для тикеров."""
    default_deposit = 100000.0
    ticker_map = {}
    
    try:
        df = pd.read_csv(SHEET_URL, header=None)
        
        # 1. ДЕПОЗИТ: сначала пробуем напрямую из B1 (строка 0, столбец 1)
        if len(df) > 0 and len(df.columns) > 1:
            val = parse_number(df.iloc[0, 1])
            if val is not None and val > 0:
                default_deposit = val
        
        # 2. Fallback: ищем слово "Депозит" в любом столбце и берем значение справа
        if default_deposit == 100000.0:
            for col_idx in range(min(4, len(df.columns))):
                deposit_rows = df[df[col_idx].astype(str).str.contains("Депозит", na=False, case=False)]
                if not deposit_rows.empty:
                    row_idx = deposit_rows.index[0]
                    if col_idx + 1 < len(df.columns):
                        val = parse_number(df.iloc[row_idx, col_idx + 1])
                        if val is not None and val > 0:
                            default_deposit = val
                            break
        
        # 3. Поиск таблицы тикеров
        header_idx = df[df[0].astype(str).str.contains("Тикер", na=False, case=False)].index
        if not header_idx.empty:
            idx = header_idx[0]
            data_df = df.iloc[idx+1:].dropna(how='all')
            data_df.columns = ["Тикер", "Тикер TW", "A", "B"]
            
            for _, row in data_df.iterrows():
                t = str(row["Тикер"]).strip()
                if t and t.lower() != "nan":
                    tw_val = str(row["Тикер TW"]).strip() if pd.notna(row["Тикер TW"]) else ""
                    pt_a = parse_number(row["A"])
                    pt_b = parse_number(row["B"])

                    direction = None
                    if pt_a is not None and pt_b is not None:
                        direction = "LONG" if pt_a < pt_b else "SHORT"

                    setup = {"tw": tw_val, "a": pt_a, "b": pt_b, "dir": direction}
                    
                    if t not in ticker_map:
                        ticker_map[t] = []
                    ticker_map[t].append(setup)
                    
    except Exception as e:
        st.warning(f"Не удалось загрузить данные из Google Sheets: {e}")
        
    return default_deposit, ticker_map


# ─────────────────────────────────────────────────────────────
# ФОРМАТИРОВАНИЕ И ЛОГИКА ЗОН
# ─────────────────────────────────────────────────────────────
def fmt_n(v, decimals: int = 0) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    d = max(0, min(int(decimals), 2))
    if d == 0:
        s = f"{v:.0f}"
    else:
        s = f"{v:.{d}f}"
        if "." in s:
            s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",")


def fmt_date(s) -> str:
    try:
        s = str(s).strip()
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except Exception:
        return str(s) if s else "—"


def determine_zone(price: float | None, a: float | None, b: float | None) -> tuple[int | None, tuple | None]:
    if price is None or a is None or b is None or a == b:
        return None, None
    
    step = (b - a) / 3
    
    z1_low, z1_high = min(a, a + step), max(a, a + step)
    z2_low, z2_high = min(a + step, a + 2 * step), max(a + step, a + 2 * step)
    z3_low, z3_high = min(a + 2 * step, b), max(a + 2 * step, b)
    
    boundaries = (z1_low, z1_high, z2_low, z2_high, z3_low, z3_high)
    
    if z1_low <= price <= z1_high:
        return 1, boundaries
    elif z2_low <= price <= z2_high:
        return 2, boundaries
    elif z3_low <= price <= z3_high:
        return 3, boundaries
    return None, boundaries


# ─────────────────────────────────────────────────────────────
# HTTP-СЕССИЯ И MOEX ISS
# ─────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "moex-futures-calc/5.0"})
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

    for col in ("MINSTEP", "STEPPRICE", "INITIALMARGIN", "LAST", "PREVSETTLEPRICE"):
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
    return max(0, min(2, int(math.ceil(-math.log10(step)))))


def _to_instrument(row: pd.Series) -> Instrument:
    return Instrument(
        ticker=str(row["SECID"]),
        name=str(row.get("SHORTNAME") or row["SECID"]),
        min_step=float(row["MINSTEP"]),
        step_price=float(row["STEPPRICE"]),
        price=float(row["PRICE"]),
        go_long=float(row["GO_LONG"]),
        go_short=float(row["GO_SHORT"]),
        expiry=str(row.get("LASTTRADEDATE") or "—"),
        decimals=_decimals(float(row["MINSTEP"])),
    )


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
    .compact-metrics .item span { font-size: 0.95rem; font-weight: 600; color: #0e1117; }
    
    .zone-indicator {
        padding: 8px 12px; border-radius: 6px; margin: 8px 0;
        font-size: 0.85rem; font-weight: 600;
    }
    .zone-1 { background: #e6f4ea; border: 1px solid #34a853; color: #137333; }
    .zone-2 { background: #fef7e0; border: 1px solid #f9ab00; color: #b06000; }
    .zone-3 { background: #fce8e6; border: 1px solid #ea4335; color: #c5221f; }
    
    .sidebar-ref {
        background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 4px;
        padding: 6px 8px; margin-top: 4px; margin-bottom: 8px;
    }
    .sidebar-ref-item {
        font-size: 0.75rem; color: #495057; padding: 2px 0;
        border-bottom: 1px dashed #dee2e6;
    }
    .sidebar-ref-item:last-child { border-bottom: none; }
    .sidebar-ref-dir { font-weight: 700; }
    .dir-long { color: #137333; }
    .dir-short { color: #c5221f; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ─────────────────────────────────────────────────────────────
# ЗАГРУЗКА ДАННЫХ ИЗ ТАБЛИЦЫ
# ─────────────────────────────────────────────────────────────
sheet_deposit, sheet_ticker_map = load_sheet_data()

# Инициализация депозита из таблицы при первом запуске сессии
if "deposit_initialized" not in st.session_state:
    st.session_state.deposit_input = sheet_deposit
    st.session_state.deposit_initialized = True


# ─────────────────────────────────────────────────────────────
# САЙДБАР
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Депозит и риск на сделку")
    st.markdown("Основные параметры для расчёта объёма позиции.")
    
    deposit = st.number_input(
        "Депозит, руб.", min_value=0.0, 
        step=1000.0, format="%.0f", key="deposit_input"
    )
    
    # СПРАВКА ПО ДЕПОЗИТУ ИЗ ТАБЛИЦЫ
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
        index=MARGIN_LEVELS.index("КСУР"),
        format_func=lambda x: MARGIN_HINTS.get(x, x),
    )
    
    st.divider()
    
    st.header("Зоны риска A-B")
    st.markdown("Введите значения вручную, опираясь на справку ниже.")
    
    point_a = st.number_input(
        "Точка A", value=None, step=1.0, format="%.2f", placeholder="Цена точки A", key="point_a_input"
    )
    point_b = st.number_input(
        "Точка B", value=None, step=1.0, format="%.2f", placeholder="Цена точки B", key="point_b_input"
    )
    
    # СПРАВОЧНЫЙ БЛОК ИЗ GOOGLE SHEETS
    st.markdown("<div style='font-size: 0.75rem; font-weight: 600; color: #666; margin-top: 8px;'>Справочные данные из таблицы:</div>", unsafe_allow_html=True)
    
    current_ticker = st.session_state.get("ticker_select", "")
    setups = sheet_ticker_map.get(current_ticker, [])
    
    if setups:
        ref_html = "<div class='sidebar-ref'>"
        for s in setups:
            dir_class = "dir-long" if s.get("dir") == "LONG" else "dir-short"
            dir_text = s.get("dir") or "N/A"
            a_text = fmt_n(s["a"]) if s["a"] is not None else "—"
            b_text = fmt_n(s["b"]) if s["b"] is not None else "—"
            ref_html += f"<div class='sidebar-ref-item'><span class='sidebar-ref-dir {dir_class}'>{dir_text}</span>: A={a_text}, B={b_text}</div>"
        ref_html += "</div>"
        st.markdown(ref_html, unsafe_allow_html=True)
    else:
        st.markdown("<div class='sidebar-ref'><div class='sidebar-ref-item'>Данных для этого тикера нет</div></div>", unsafe_allow_html=True)
    
    st.divider()
    if st.button("Обновить котировки", use_container_width=True):
        fetch_futures.clear()
        fetch_ims.clear()
        load_sheet_data.clear()
        st.rerun()


# ─────────────────────────────────────────────────────────────
# ОСНОВНОЙ ИНТЕРФЕЙС
# ─────────────────────────────────────────────────────────────
st.markdown("<h4 style='margin: 0 0 8px 0;'>Калькулятор фьючерсов MOEX</h4>", unsafe_allow_html=True)

with st.spinner("Загрузка данных..."):
    futures = fetch_futures()
    ims = fetch_ims()

if futures.empty:
    st.error("Не удалось загрузить список фьючерсов с MOEX ISS.")
    st.stop()

futures = merge_margins(futures, ims, margin_type)
futures = futures[(futures["PRICE"] > 0) & ((futures["GO_LONG"] > 0) | (futures["GO_SHORT"] > 0))].copy()

if futures.empty:
    st.warning("Нет контрактов с валидными ценой и ГО.")
    st.stop()

futures = futures.sort_values("SECID").reset_index(drop=True)
instruments: dict[str, Instrument] = {row["SECID"]: _to_instrument(row) for _, row in futures.iterrows()}


def format_ticker(t: str) -> str:
    setups = sheet_ticker_map.get(t, [])
    tw = setups[0]["tw"] if setups and setups[0]["tw"] else ""
    return f"{t} ({tw})" if tw else t

ticker = st.selectbox(
    "Фьючерс", list(instruments.keys()),
    format_func=format_ticker,
    label_visibility="collapsed",
    key="ticker_select"
)

# Логика сброса полей при смене тикера
if "last_ticker" not in st.session_state:
    st.session_state.last_ticker = ticker
elif st.session_state.last_ticker != ticker:
    for key in TRADE_STATE_KEYS:
        if key in st.session_state:
            del st.session_state[key]
    st.session_state.last_ticker = ticker

inst = instruments[ticker]

st.markdown(
    f"""
    <div class="compact-metrics">
        <div class="item"><b>Цена</b><span>{fmt_n(inst.price, inst.decimals)}</span></div>
        <div class="item"><b>Шаг / Ст</b><span>{fmt_n(inst.min_step, inst.decimals)} / {fmt_n(inst.step_price, 2)} руб.</span></div>
        <div class="item"><b>ГО ({margin_type})</b><span>L: {fmt_n(inst.go_long, 0)} / S: {fmt_n(inst.go_short, 0)} руб.</span></div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ИНДИКАТОР ТЕКУЩЕЙ ЗОНЫ ЦЕНЫ
current_zone, _ = determine_zone(inst.price, point_a, point_b)
if current_zone is not None:
    zone_class = f"zone-{current_zone}"
    zone_text = {1: "Зона 1 (Идеальная)", 2: "Зона 2 (Повышенный риск)", 3: "Зона 3 (Запрещена)"}
    st.markdown(f'<div class="zone-indicator {zone_class}">Текущая цена в {zone_text[current_zone]}</div>', unsafe_allow_html=True)
else:
    st.caption(f"1 пункт = {fmt_n(inst.point_value, 2)} руб. | Экспирация: {fmt_date(inst.expiry)}")


# ─────────────────────────────────────────────────────────────
# ПАРАМЕТРЫ СДЕЛКИ
# ─────────────────────────────────────────────────────────────
direction = st.radio(
    "Направление", ["LONG", "SHORT"], horizontal=True, index=0, 
    label_visibility="collapsed", key="direction_radio"
)
sign = 1 if direction == "LONG" else -1

fmt_in = f"%.{inst.decimals}f"
step_ui = float(inst.min_step) if inst.min_step > 0 else 0.01

st.markdown("<div style='font-size: 0.85rem; font-weight: 600; margin-bottom: 4px;'>Уровни цены</div>", unsafe_allow_html=True)
c1, c2 = st.columns(2, gap="small")
sl = c1.number_input("SL", value=None, step=step_ui, format=fmt_in, placeholder="Стоп-лосс", label_visibility="collapsed", key="sl_input")
tp = c2.number_input("TP", value=None, step=step_ui, format=fmt_in, placeholder="Тейк-профит", label_visibility="collapsed", key="tp_input")

st.markdown("<div style='font-size: 0.85rem; font-weight: 600; margin: 8px 0 4px 0;'>Точка входа</div>", unsafe_allow_html=True)
ec1, ec2 = st.columns([3, 2], gap="small")
p1 = ec1.number_input("Цена 1", value=None, step=step_ui, format=fmt_in, placeholder="Цена входа", label_visibility="collapsed", key="entry_p_0")
q1 = ec2.number_input("Контр. 1", min_value=0, value=None, step=1, format="%d", placeholder="Кол-во", label_visibility="collapsed", key="entry_q_0")

entries = []
if p1 is not None:
    entries.append({"price": p1, "qty": q1 if q1 is not None else 0})

with st.expander("Добавить усреднение (до 3 точек)", expanded=False):
    for i in range(1, 3):
        ec1, ec2 = st.columns([3, 2], gap="small")
        p = ec1.number_input(f"Цена {i+1}", value=None, step=step_ui, format=fmt_in, placeholder=f"Цена входа {i+1}", label_visibility="collapsed", key=f"entry_p_{i}")
        q = ec2.number_input(f"Контр. {i+1}", min_value=0, value=None, step=1, format="%d", placeholder=f"Кол-во {i+1}", label_visibility="collapsed", key=f"entry_q_{i}")
        if p is not None:
            entries.append({"price": p, "qty": q if q is not None else 0})

with_qty = [e for e in entries if e["qty"] > 0]

if with_qty:
    entries_qty_sum = sum(e["qty"] for e in with_qty)
    avg_entry = sum(e["price"] * e["qty"] for e in with_qty) / entries_qty_sum
elif entries:
    avg_entry = entries[0]["price"]
    entries_qty_sum = 0
else:
    avg_entry = None
    entries_qty_sum = 0

effective_qty = entries_qty_sum

if avg_entry is not None and sl is not None and avg_entry != sl:
    risk_pts = abs(avg_entry - sl)
    raw_min_tp = avg_entry + sign * risk_pts * required_rr
    min_tp = inst.round_price(raw_min_tp)
    st.info(f"Мин. TP для RR 1:{fmt_n(required_rr, 1)} -> {fmt_n(min_tp, inst.decimals)}")


# ─────────────────────────────────────────────────────────────
# ВАЛИДАЦИЯ И РЕЗУЛЬТАТ
# ─────────────────────────────────────────────────────────────
have_prices = avg_entry is not None and sl is not None and tp is not None
have_qty = effective_qty > 0
warnings = []

entry_zone, zone_boundaries = determine_zone(avg_entry, point_a, point_b)
zone_blocked = False
zone_reduction = 1.0
zone_message = None

if entry_zone is not None and point_a is not None and point_b is not None:
    if direction == "LONG" and entry_zone == 3:
        zone_blocked = True
        zone_message = "ЗАПРЕЩЕНО: вход в зоне 3 (у точки B) для LONG. Высокий риск."
    elif direction == "SHORT" and entry_zone == 1:
        zone_blocked = True
        zone_message = "ЗАПРЕЩЕНО: вход в зоне 1 (у точки A) для SHORT. Высокий риск."
    elif entry_zone == 2:
        zone_reduction = 0.5
        zone_message = "Повышенный риск: зона 2. Рекомендуется сократить объём на 50%."
    else:
        zone_message = f"Зона {entry_zone}: оптимальная для {direction}."

if have_prices:
    if direction == "LONG":
        if sl >= avg_entry: warnings.append("SL должен быть ниже цены входа")
        if tp <= avg_entry: warnings.append("TP должен быть выше цены входа")
    else:
        if sl <= avg_entry: warnings.append("SL должен быть выше цены входа")
        if tp >= avg_entry: warnings.append("TP должен быть ниже цены входа")

for w in warnings:
    st.warning(w)

if entry_zone is not None and zone_message:
    zone_class = f"zone-{entry_zone}"
    st.markdown(f'<div class="zone-indicator {zone_class}">{zone_message}</div>', unsafe_allow_html=True)

st.markdown("<div style='height: 4px;'></div>", unsafe_allow_html=True)

if zone_blocked:
    st.error(zone_message)
elif not have_prices:
    st.info("Заполните цену входа, SL и TP для расчёта.")
elif not have_qty:
    sl_dist = abs(avg_entry - sl)
    tp_dist = abs(tp - avg_entry)
    rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0
    st.markdown(f"<div class='compact-metrics'><div class='item' style='flex:1'><b>Расчётный RR</b><span style='color: #0068c9'>{fmt_n(rr, 2)}</span></div></div>", unsafe_allow_html=True)
    st.caption("Укажите количество контрактов для расчёта объёма и риска.")
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
    qty_rec = min(qty_by_risk, qty_by_margin)
    
    qty_rec = int(qty_rec * zone_reduction)

    rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0
    total_risk = risk_per_ct * effective_qty
    total_reward = profit_per_ct * effective_qty

    rr_ok = (rr >= required_rr) and (sl_dist > 0) and (tp_dist > 0)
    mm_ok = (effective_qty <= qty_rec) and not warnings and effective_qty > 0

    st.markdown(
        f"""
        <div class="compact-metrics" style="background: #e8f4f8; border: 1px solid #d0e8f2;">
            <div class="item"><b>RR</b><span style="color: {'#00cc00' if rr_ok else '#ff4b4b'}">{fmt_n(rr, 2)}</span></div>
            <div class="item"><b>Риск/конт</b><span>{fmt_n(risk_per_ct, 0)} руб.</span></div>
            <div class="item"><b>Реком. объём</b><span style="color: #0068c9">{fmt_n(qty_rec, 0)}</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not mm_ok or not rr_ok:
        if warnings:
            st.error("Некорректные цены сделки")
        elif effective_qty > qty_rec:
            st.error(f"Объём {effective_qty} > реком. {qty_rec}")
        elif not rr_ok:
            ratio = rr / required_rr
            safe_qty = int(math.floor(qty_rec * ratio))
            if safe_qty < 1:
                st.warning("RR слишком низкий. Рекомендуется пропустить сделку.")
            elif effective_qty <= safe_qty:
                st.info(f"RR ниже плана, но объём {effective_qty} <= безопасного {safe_qty}")
            else:
                new_risk = risk_per_ct * safe_qty
                st.warning(f"Сократите до {safe_qty} конт. (риск снизится до {fmt_n(new_risk, 0)} руб.)")
    else:
        st.success(f"Сделка корректна: {effective_qty} конт. <= {qty_rec}, RR = {fmt_n(rr, 2)}")

    with st.expander("Детали расчёта", expanded=False):
        actual_risk_pct = (total_risk / deposit * 100) if deposit else 0
        risk_exceeded = actual_risk_pct > risk_pct
        risk_color = "#ea4335" if risk_exceeded else "#0e1117"
        risk_style = f"color: {risk_color}; font-weight: 700;" if risk_exceeded else ""
        
        zone_info = ""
        if entry_zone is not None and zone_boundaries is not None:
            z1_low, z1_high, z2_low, z2_high, z3_low, z3_high = zone_boundaries
            zone_info = f"""
**Зоны риска A-B:**
- Зона 1 (идеальная): {fmt_n(z1_low, inst.decimals)} — {fmt_n(z1_high, inst.decimals)}
- Зона 2 (повышенный риск): {fmt_n(z2_low, inst.decimals)} — {fmt_n(z2_high, inst.decimals)}
- Зона 3 (запрещена): {fmt_n(z3_low, inst.decimals)} — {fmt_n(z3_high, inst.decimals)}
- Точка входа в зоне: **{entry_zone}**
"""
        
        st.markdown(
            f"""
**Вход:** {fmt_n(avg_entry, inst.decimals)} | **Контрактов:** {fmt_n(effective_qty, 0)}

**SL:** {fmt_n(sl_dist, inst.decimals)} пт = {fmt_n(sl_steps, 1)} шагов x {fmt_n(inst.step_price, 2)} руб. = **{fmt_n(risk_per_ct, 2)} руб. / конт.**
**TP:** {fmt_n(tp_dist, inst.decimals)} пт = {fmt_n(tp_steps, 1)} шагов x {fmt_n(inst.step_price, 2)} руб. = **{fmt_n(profit_per_ct, 2)} руб. / конт.**

**Бюджет риска:** {fmt_n(deposit, 0)} руб. x {fmt_n(risk_pct, 1)}% = **{fmt_n(risk_budget, 0)} руб.**
**Qty по риску:** {fmt_n(qty_by_risk, 0)} конт. | **Qty по ГО:** {fmt_n(qty_by_margin, 0)} конт.
{f'**Корректировка зоны:** x{zone_reduction} (зона 2)' if zone_reduction < 1.0 else ''}

**Итог на {fmt_n(effective_qty, 0)} конт.:**
- Прибыль при TP: **+{fmt_n(total_reward, 2)} руб.**
- Убыток при SL: **-{fmt_n(total_risk, 2)} руб.**
- Фактический риск: <span style="{risk_style}">**{fmt_n(actual_risk_pct, 2)}%** от депозита</span>{' (ПРЕВЫШАЕТ УСТАНОВЛЕННЫЙ РИСК)' if risk_exceeded else ''}

{zone_info}
*Источник данных ГО: таблица клиринга MOEX ISS (параметр {margin_type})*
""",
            unsafe_allow_html=True,
        )