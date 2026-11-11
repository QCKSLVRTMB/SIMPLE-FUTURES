"""
Калькулятор фьючерсов MOEX — самодостаточная версия.
Загружает ВСЕ фьючерсы и ГО из MOEX ISS напрямую.
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
# ФОРМАТИРОВАНИЕ
# ─────────────────────────────────────────────────────────────
def fmt_n(v, decimals: int = 0) -> str:
    """Без разделителя тысяч, запятая как десятичный, ≤ 2 знаков."""
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


# ─────────────────────────────────────────────────────────────
# HTTP-СЕССИЯ
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


@st.cache_data(ttl=900, show_spinner=False)
def fetch_ims() -> pd.DataFrame:
    df = pd.DataFrame()
    try:
        r = _session().get(f"{ISS}/ims", params={"iss.meta": "off", "format": "json"}, timeout=15)
        r.raise_for_status()
        block = r.json().get("ims") or {}
        df = pd.DataFrame(block.get("data") or [], columns=block.get("columns") or [])
    except Exception:
        pass

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
    df["GO_LONG"] = df["INITIALMARGIN"]
    df["GO_SHORT"] = df["INITIALMARGIN"]

    cols = IMS_MARGIN_MAP.get(margin_type)
    if cols and not ims.empty and "SECID" in ims.columns:
        col_b, col_s = cols
        if col_b in ims.columns and col_s in ims.columns:
            m = ims.set_index("SECID")
            df["GO_LONG"] = df["SECID"].map(m[col_b]).fillna(df["GO_LONG"])
            df["GO_SHORT"] = df["SECID"].map(m[col_s]).fillna(df["GO_SHORT"])

    df["GO_LONG"] = df["GO_LONG"].where(df["GO_LONG"] > 0, df["INITIALMARGIN"])
    df["GO_SHORT"] = df["GO_SHORT"].where(df["GO_SHORT"] > 0, df["INITIALMARGIN"])
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
ca.metric("Цена", fmt_n(inst.price, inst.decimals))
cb.metric("Шаг / ст. шага", f"{fmt_n(inst.min_step, inst.decimals)} / {fmt_n(inst.step_price, 2)} ₽")
cc.metric("ГО L / S", f"{fmt_n(inst.go_long, 0)} / {fmt_n(inst.go_short, 0)} ₽")
st.caption(
    f"1 пункт = {fmt_n(inst.point_value, 2)} ₽ · Экспирация: {fmt_date(inst.expiry)}"
)


# ─────────────────────────────────────────────────────────────
# ПАРАМЕТРЫ СДЕЛКИ
# ─────────────────────────────────────────────────────────────
st.subheader("2. Сделка")

direction = st.radio("Направление", ["LONG", "SHORT"], horizontal=True)
sign = 1 if direction == "LONG" else -1

fmt_in = f"%.{inst.decimals}f"
step_ui = float(inst.min_step) if inst.min_step > 0 else 0.01

c1, c2 = st.columns(2)
sl = c1.number_input("Стоп-лосс (SL)", value=None, step=step_ui, format=fmt_in, placeholder="SL")
tp = c2.number_input("Тейк-профит (TP)", value=None, step=step_ui, format=fmt_in, placeholder="TP")

st.markdown("**Точки входа (до 3)**")
entries: list[dict] = []
for i in range(3):
    ec1, ec2 = st.columns([3, 2])
    p = ec1.number_input(
        f"Цена входа {i + 1}",
        value=None, step=step_ui, format=fmt_in,
        placeholder="—",
        key=f"entry_p_{i}",
    )
    q = ec2.number_input(
        f"Контрактов {i + 1}",
        min_value=0, value=None, step=1, format="%d",
        placeholder="0",
        key=f"entry_q_{i}",
    )
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

if avg_entry is not None:
    parts = [f"Средняя цена входа: **{fmt_n(avg_entry, inst.decimals)}**"]
    if entries_qty_sum > 0:
        parts.append(f"Всего контрактов: **{fmt_n(entries_qty_sum, 0)}**")
    st.info(" · ".join(parts))

if avg_entry is not None and sl is not None and avg_entry != sl:
    risk_pts = abs(avg_entry - sl)
    raw_min_tp = avg_entry + sign * risk_pts * required_rr
    min_tp = inst.round_price(raw_min_tp)
    st.info(
        f"Минимальный TP для RR = 1 : {fmt_n(required_rr, 2)} → "
        f"**{fmt_n(min_tp, inst.decimals)}** "
        f"(риск {fmt_n(risk_pts, inst.decimals)} пт × {fmt_n(required_rr, 2)} = "
        f"{fmt_n(risk_pts * required_rr, inst.decimals)} пт)"
    )
elif avg_entry is not None and sl is None:
    st.caption("Введите стоп-лосс — покажу минимальный TP для заданного RR.")
elif avg_entry is None:
    st.caption("Введите цену входа — покажу минимальный TP для заданного RR.")


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

for w in warnings:
    st.warning(w)


# ─────────────────────────────────────────────────────────────
# РЕЗУЛЬТАТ
# ─────────────────────────────────────────────────────────────
st.subheader("3. Результат")

if not have_prices:
    st.info("Заполните хотя бы одну цену входа, стоп-лосс и тейк-профит для расчёта.")
elif not have_qty:
    sl_dist = abs(avg_entry - sl)
    tp_dist = abs(tp - avg_entry)
    rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0
    st.metric("Расчётный RR", fmt_n(rr, 2))
    st.info("Укажите количество контрактов справа от цен входа для расчёта риска, сайзинга и MM.")
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

    rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0
    total_risk = risk_per_ct * effective_qty
    total_reward = profit_per_ct * effective_qty

    rr_ok = (rr >= required_rr) and (sl_dist > 0) and (tp_dist > 0)
    mm_ok = (effective_qty <= qty_rec) and not warnings and effective_qty > 0

    m1, m2, m3 = st.columns(3)
    m1.metric("Расчётный RR", fmt_n(rr, 2))
    m2.metric("Риск на 1 контракт", f"{fmt_n(risk_per_ct, 2)} ₽")
    m3.metric("Рекомендуемый объём", f"{fmt_n(qty_rec, 0)} конт.")

    col_rr, col_mm = st.columns(2)
    with col_rr:
        st.markdown("**Проверка RR**")
        if rr_ok:
            st.success(f"RR = {fmt_n(rr, 2)} ≥ {fmt_n(required_rr, 2)}")
        else:
            st.error(f"RR = {fmt_n(rr, 2)} < {fmt_n(required_rr, 2)}")

    with col_mm:
        st.markdown("**Проверка MM**")
        if warnings:
            st.error("Некорректные цены сделки (см. предупреждения).")
        elif mm_ok:
            st.success(f"{fmt_n(effective_qty, 0)} конт. ≤ рекомендуемых {fmt_n(qty_rec, 0)}")
        else:
            st.error(f"{fmt_n(effective_qty, 0)} конт. > рекомендуемых {fmt_n(qty_rec, 0)}")

        if have_prices and have_qty and not warnings and rr > 0 and rr < required_rr:
            ratio = rr / required_rr
            safe_qty = int(math.floor(qty_rec * ratio))
            if safe_qty < 1:
                st.warning(
                    f"RR = {fmt_n(rr, 2)} при требуемом {fmt_n(required_rr, 2)} — слишком низкий. "
                    f"Рекомендуется пропустить сделку."
                )
            elif effective_qty <= safe_qty:
                st.info(
                    f"RR = {fmt_n(rr, 2)} < {fmt_n(required_rr, 2)} "
                    f"({fmt_n(ratio * 100, 0)}% от плана). "
                    f"Позиция в пределах безопасного объёма: "
                    f"{fmt_n(effective_qty, 0)} ≤ {fmt_n(safe_qty, 0)} конт."
                )
            else:
                new_risk = risk_per_ct * safe_qty
                st.info(
                    f"RR = {fmt_n(rr, 2)} < {fmt_n(required_rr, 2)} "
                    f"({fmt_n(ratio * 100, 0)}% от плана). "
                    f"Сократите позицию до **{fmt_n(safe_qty, 0)}** конт. — "
                    f"риск снизится с {fmt_n(total_risk, 2)} ₽ до {fmt_n(new_risk, 2)} ₽."
                )

    with st.expander("Детали расчёта"):
        st.markdown(
            f"""
**Взвешенная цена входа:** `{fmt_n(avg_entry, inst.decimals)}`
**Количество контрактов:** `{fmt_n(effective_qty, 0)}`

**SL:** `{fmt_n(sl_dist, inst.decimals)}` пт = `{fmt_n(sl_steps, 2)}` шагов × `{fmt_n(inst.step_price, 2)}` ₽ = **{fmt_n(risk_per_ct, 2)} ₽ / контракт**

**TP:** `{fmt_n(tp_dist, inst.decimals)}` пт = `{fmt_n(tp_steps, 2)}` шагов × `{fmt_n(inst.step_price, 2)}` ₽ = **{fmt_n(profit_per_ct, 2)} ₽ / контракт**

**Бюджет риска:** `{fmt_n(deposit, 0)} ₽ × {fmt_n(risk_pct, 2)}%` = **{fmt_n(risk_budget, 2)} ₽**

**Qty по риску:** `{fmt_n(risk_budget, 0)} ₽ // {fmt_n(risk_per_ct, 2)} ₽` = **{fmt_n(qty_by_risk, 0)} конт.**

**Qty по ГО ({margin_type}):** `({fmt_n(deposit, 0)} ₽ × {fmt_n(margin_util, 0)}%) // {fmt_n(margin, 0)} ₽` = **{fmt_n(qty_by_margin, 0)} конт.**

**Рекомендуемый объём:** `min({fmt_n(qty_by_risk, 0)}, {fmt_n(qty_by_margin, 0)})` = **{fmt_n(qty_rec, 0)} конт.**

**Сделка на {fmt_n(effective_qty, 0)} конт.:**
- Прибыль при TP: **{fmt_n(total_reward, 2)} ₽**
- Убыток при SL: **{fmt_n(total_risk, 2)} ₽**
- Фактический риск: **{fmt_n((total_risk) / deposit * 100 if deposit else 0, 2)}%** от депозита
"""
        )
