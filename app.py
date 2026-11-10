"""
Калькулятор фьючерсов MOEX — самодостаточная версия.
Загружает ВСЕ фьючерсы и ГО из MOEX ISS напрямую.
Зависимости: streamlit, pandas, requests.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from io import StringIO

import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter, Retry

# ─────────────────────────────────────────────────────────────
# КОНСТАНТЫ (из core/config.py)
# ─────────────────────────────────────────────────────────────
ISS = "https://iss.moex.com/iss/engines/futures/markets/forts"

MARGIN_LEVELS = ["НАЧ. ГО", "КНУР", "КСУР", "КПУР", "КОУР"]
MARGIN_HINTS = {
    "НАЧ. ГО": "НАЧ. ГО — базовое гарантийное обеспечение",
    "КНУР":    "КНУР — начальный (новичкам)",
    "КСУР":    "КСУР — стандартный",
    "КПУР":    "КПУР — повышенный (квалифицированным)",
    "КОУР":    "КОУР — особый уровень риска",
}
IMS_MARGIN_MAP = {
    "НАЧ. ГО": ("IM_BUY",      "IM_SELL"),
    "КНУР":    ("IM_BUY_LRC",  "IM_SELL_LRC"),
    "КСУР":    ("IM_BUY_MRC",  "IM_SELL_MRC"),
    "КПУР":    ("IM_BUY_HRC",  "IM_SELL_HRC"),
    "КОУР":    ("IM_BUY_ERC",  "IM_SELL_ERC"),
}


# ─────────────────────────────────────────────────────────────
# HTTP-СЕССИЯ (из core/market.py::_session)
# ─────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "simple-futures/1.0"})
    s.mount(
        "https://",
        HTTPAdapter(
            max_retries=Retry(
                total=3,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
            ),
            pool_maxsize=8,
        ),
    )
    return s


# ─────────────────────────────────────────────────────────────
# ЗАГРУЗКА ФЬЮЧЕРСОВ (из core/market.py::fetch_moex_forts)
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def fetch_futures() -> pd.DataFrame:
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
        st.error(f"MOEX ISS недоступен: {e}")
        return pd.DataFrame()

    sec = sec.drop_duplicates("SECID", keep="first")
    mkt = mkt.drop_duplicates("SECID", keep="first")
    df = sec.merge(mkt, on="SECID", how="left")

    for col in ("MINSTEP", "STEPPRICE", "INITIALMARGIN", "LAST",
                "PREVSETTLEPRICE", "BUYSELLFEE", "OPENPOSITION", "LASTCHANGEPRCNT"):
        if col not in df.columns:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    df = df[(df["MINSTEP"] > 0) & (df["STEPPRICE"] > 0)].copy()
    df["PRICE"] = df["LAST"].where(df["LAST"] > 0, df["PREVSETTLEPRICE"])
    return df


# ─────────────────────────────────────────────────────────────
# ЗАГРУЗКА ГО ПО КАТЕГОРИЯМ (из core/market.py::fetch_moex_ims)
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=900, show_spinner=False)
def fetch_ims() -> pd.DataFrame:
    # 1) пробуем JSON
    df = pd.DataFrame()
    try:
        r = _session().get(f"{ISS}/ims", params={"iss.meta": "off", "format": "json"}, timeout=15)
        r.raise_for_status()
        block = r.json().get("ims") or {}
        df = pd.DataFrame(block.get("data") or [], columns=block.get("columns") or [])
    except Exception:
        pass

    # 2) fallback: CSV с разделителем "|"
    if df.empty:
        try:
            r = _session().get(f"{ISS}/ims", params={"iss.meta": "off"}, timeout=15)
            r.raise_for_status()
            df = pd.read_csv(StringIO(r.text), sep="|")
            df.columns = [str(c).split("(")[0].strip() for c in df.columns]
        except Exception:
            return pd.DataFrame()

    if df.empty:
        return df

    df.columns = [str(c).upper() for c in df.columns]
    for c in df.columns:
        if c.startswith("IM_") and c != "IM_TIME":
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    if "BOARDID" in df.columns:
        filtered = df[df["BOARDID"] == "RFUD"]
        if not filtered.empty:
            df = filtered
    if "SECID" in df.columns:
        df = df.drop_duplicates("SECID", keep="first")
    return df


def merge_margins(futures: pd.DataFrame, ims: pd.DataFrame, margin_type: str) -> pd.DataFrame:
    df = futures.copy()
    df["GO_LONG"]  = df["INITIALMARGIN"]
    df["GO_SHORT"] = df["INITIALMARGIN"]

    cols = IMS_MARGIN_MAP.get(margin_type)
    if cols and not ims.empty and "SECID" in ims.columns:
        col_b, col_s = cols
        if col_b in ims.columns and col_s in ims.columns:
            m = ims.set_index("SECID")
            df["GO_LONG"]  = df["SECID"].map(m[col_b]).fillna(df["GO_LONG"])
            df["GO_SHORT"] = df["SECID"].map(m[col_s]).fillna(df["GO_SHORT"])

    # если в IMS для этой категории пусто — используем INITIALMARGIN
    df["GO_LONG"]  = df["GO_LONG"].where(df["GO_LONG"]  > 0, df["INITIALMARGIN"])
    df["GO_SHORT"] = df["GO_SHORT"].where(df["GO_SHORT"] > 0, df["INITIALMARGIN"])
    return df


# ─────────────────────────────────────────────────────────────
# INSTRUMENT (упрощённый core/calc.py::Instrument)
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


def _decimals(step: float) -> int:
    if step <= 0:
        return 2
    return max(0, min(6, int(math.ceil(-math.log10(step)))))


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
# СТРАНИЦА
# ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Калькулятор фьючерсов MOEX",
    page_icon="📊",
    layout="centered",
    initial_sidebar_state="collapsed",
)
st.markdown(
    """
    <style>
    .block-container { padding-top: 1rem; padding-bottom: 1rem; }
    @media (max-width: 640px) {
        [data-testid="column"] { width: 100% !important; flex: 1 1 100% !important; }
        .stButton > button { width: 100%; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ─────────────────────────────────────────────────────────────
# САЙДБАР
# ─────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Параметры счёта")
    deposit = st.number_input(
        "Депозит, ₽", min_value=0.0, value=100_000.0, step=10_000.0, format="%.0f"
    )
    risk_pct = st.number_input(
        "Риск на сделку, %", min_value=0.1, max_value=100.0, value=1.0, step=0.1
    )
    margin_util = st.number_input(
        "Макс. загрузка ГО, %", min_value=10, max_value=100, value=100, step=10
    )
    required_rr = st.number_input(
        "Требуемый RR", min_value=0.5, max_value=20.0, value=3.0, step=0.5
    )
    margin_type = st.selectbox(
        "Категория риска",
        MARGIN_LEVELS,
        index=MARGIN_LEVELS.index("КСУР"),
        format_func=lambda x: MARGIN_HINTS.get(x, x),
    )
    if st.button("Обновить котировки"):
        fetch_futures.clear()
        fetch_ims.clear()
        st.rerun()


# ─────────────────────────────────────────────────────────────
# ЗАГРУЗКА
# ─────────────────────────────────────────────────────────────
st.title("Калькулятор фьючерсов MOEX")
st.caption("Простой расчёт объёма позиции и проверки RR / MM")

with st.spinner("Загрузка данных MOEX ISS..."):
    futures = fetch_futures()
    ims = fetch_ims()

if futures.empty:
    st.error("Не удалось загрузить список фьючерсов с MOEX ISS.")
    st.stop()

futures = merge_margins(futures, ims, margin_type)

# Оставляем ликвидные контракты с валидными данными
futures = futures[
    (futures["PRICE"] > 0)
    & ((futures["GO_LONG"] > 0) | (futures["GO_SHORT"] > 0))
].copy()

if futures.empty:
    st.warning("Нет контрактов с валидными ценой и ГО по выбранной категории риска.")
    st.stop()

futures = futures.sort_values("SECID").reset_index(drop=True)
instruments: dict[str, Instrument] = {
    row["SECID"]: _to_instrument(row) for _, row in futures.iterrows()
}


# ─────────────────────────────────────────────────────────────
# ВЫБОР ИНСТРУМЕНТА
# ─────────────────────────────────────────────────────────────
st.subheader("1. Инструмент")

ticker = st.selectbox(
    "Фьючерс",
    list(instruments.keys()),
    format_func=lambda t: f"{t} — {instruments[t].name}",
)
inst = instruments[ticker]

ca, cb, cc = st.columns(3)
ca.metric("Цена", f"{inst.price:,.{inst.decimals}f}")
cb.metric("Шаг / ст. шага", f"{inst.min_step:g} / {inst.step_price:g} ₽")
cc.metric("ГО L / S", f"{inst.go_long:,.0f} / {inst.go_short:,.0f} ₽")
st.caption(f"1 пункт = {inst.point_value:.4f} ₽ · Экспирация: {inst.expiry}")


# ─────────────────────────────────────────────────────────────
# ПАРАМЕТРЫ СДЕЛКИ
# ─────────────────────────────────────────────────────────────
st.subheader("2. Сделка")

direction = st.radio("Направление", ["LONG", "SHORT"], horizontal=True)

fmt = f"%.{inst.decimals}f"
step_ui = float(inst.min_step) if inst.min_step > 0 else 0.01
base = inst.price if inst.price > 0 else 100_000.0

c1, c2, c3 = st.columns(3)
entry = c1.number_input("Цена входа", value=base, step=step_ui, format=fmt)
sl = c2.number_input(
    "Стоп-лосс",
    value=round(base * 0.99, inst.decimals),
    step=step_ui,
    format=fmt,
)
tp = c3.number_input(
    "Тейк-профит",
    value=round(base * 1.03, inst.decimals),
    step=step_ui,
    format=fmt,
)


# ─────────────────────────────────────────────────────────────
# ВАЛИДАЦИЯ
# ─────────────────────────────────────────────────────────────
warnings: list[str] = []
if direction == "LONG":
    if sl >= entry:
        warnings.append("LONG: стоп-лосс должен быть ниже цены входа.")
    if tp <= entry:
        warnings.append("LONG: тейк-профит должен быть выше цены входа.")
else:
    if sl <= entry:
        warnings.append("SHORT: стоп-лосс должен быть выше цены входа.")
    if tp >= entry:
        warnings.append("SHORT: тейк-профит должен быть ниже цены входа.")
for w in warnings:
    st.warning(w)


# ─────────────────────────────────────────────────────────────
# РАСЧЁТЫ
# ─────────────────────────────────────────────────────────────
sl_dist = abs(entry - sl)
tp_dist = abs(tp - entry)

sl_steps = sl_dist / inst.min_step
tp_steps = tp_dist / inst.min_step
risk_per_ct = sl_steps * inst.step_price
profit_per_ct = tp_steps * inst.step_price

margin = inst.margin(direction)
risk_budget = deposit * risk_pct / 100

qty_by_risk = int(risk_budget // risk_per_ct) if risk_per_ct > 0 else 0
qty_by_margin = int((deposit * margin_util / 100) // margin) if margin > 0 else 0
qty_rec = min(qty_by_risk, qty_by_margin)

rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0
rr_ok = (rr >= required_rr) and (sl_dist > 0) and (tp_dist > 0)
mm_ok = (qty_rec >= 1) and not warnings


# ─────────────────────────────────────────────────────────────
# РЕЗУЛЬТАТ
# ─────────────────────────────────────────────────────────────
st.subheader("3. Результат")

m1, m2, m3 = st.columns(3)
m1.metric("Расчётный RR", f"{rr:.2f}")
m2.metric("Риск на 1 контракт", f"{risk_per_ct:,.0f} ₽")
m3.metric("Рекомендуемый объём", f"{qty_rec} конт.")

col_rr, col_mm = st.columns(2)
with col_rr:
    st.markdown("**Проверка RR**")
    if rr_ok:
        st.success(f"RR = {rr:.2f} ≥ {required_rr:.1f}")
    else:
        st.error(f"RR = {rr:.2f} < {required_rr:.1f}")

with col_mm:
    st.markdown("**Проверка MM**")
    if mm_ok:
        st.success(f"Помещается {qty_rec} конт.")
    elif warnings:
        st.error("Некорректные цены сделки (см. предупреждения).")
    else:
        st.error("Ни один контракт не помещается по риску или ГО.")


with st.expander("Детали расчёта"):
    st.markdown(
        f"""
**SL:** `{sl_dist:,.{inst.decimals}f}` пт = `{sl_steps:.2f}` шагов × `{inst.step_price:g}` ₽ = **{risk_per_ct:,.2f} ₽ / контракт**

**TP:** `{tp_dist:,.{inst.decimals}f}` пт = `{tp_steps:.2f}` шагов × `{inst.step_price:g}` ₽ = **{profit_per_ct:,.2f} ₽ / контракт**

**Бюджет риска:** `{deposit:,.0f} ₽ × {risk_pct}%` = **{risk_budget:,.2f} ₽**

**Qty по риску:** `{risk_budget:,.0f} ₽ // {risk_per_ct:,.0f} ₽` = **{qty_by_risk} конт.**

**Qty по ГО ({margin_type}):** `({deposit:,.0f} ₽ × {margin_util}%) // {margin:,.0f} ₽` = **{qty_by_margin} конт.**

**Рекомендуемый объём:** `min({qty_by_risk}, {qty_by_margin})` = **{qty_rec} конт.**

**При {qty_rec} конт.:**
- Прибыль при TP: **{profit_per_ct * qty_rec:,.0f} ₽**
- Убыток при SL: **{risk_per_ct * qty_rec:,.0f} ₽**
- Фактический риск: **{(risk_per_ct * qty_rec) / deposit * 100 if deposit else 0:.2f}%** от депозита
"""
    )

st.divider()
st.caption(
    "Данные: MOEX ISS (`securities.json`, `marketdata`, `/ims`). "
    "ГО зависит от категории риска. Комиссии не учитываются."
)
