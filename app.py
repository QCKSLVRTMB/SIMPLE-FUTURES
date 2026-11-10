"""
Калькулятор фьючерсов MOEX — упрощённая версия.
Использует готовые модули core.market / core.calc / core.config из проекта.
"""
from __future__ import annotations

import streamlit as st

from core.market import fetch_moex_forts, fetch_moex_ims, load_api, load_go, load_marketdata
from core.calc import build_instruments
from core.config import MARGIN_LEVELS, MARGIN_HINTS


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
        fetch_moex_forts.clear()
        fetch_moex_ims.clear()
        st.rerun()


# ─────────────────────────────────────────────────────────────
# ЗАГРУЗКА ДАННЫХ
# ─────────────────────────────────────────────────────────────
st.title("Калькулятор фьючерсов MOEX")
st.caption("Простой расчёт объёма позиции и проверки RR / MM")

with st.spinner("Загрузка данных MOEX ISS..."):
    iss_df = fetch_moex_forts()
    ims_df = fetch_moex_ims()
    api_df = load_api()
    go_df = load_go()
    md_df = load_marketdata()

if iss_df is None or iss_df.empty:
    st.error("Не удалось загрузить данные MOEX ISS. Попробуйте позже.")
    st.stop()

instruments = build_instruments(iss_df, ims_df, go_df, api_df, md_df, margin_type)
if not instruments:
    st.error("Список инструментов пуст.")
    st.stop()

# Оставляем только те, у кого есть цена и ГО
tickers = sorted(
    t for t, i in instruments.items()
    if i.price > 0 and (i.margin_long > 0 or i.margin_short > 0)
)
if not tickers:
    st.warning("Нет инструментов с валидными ценой и ГО.")
    st.stop()


# ─────────────────────────────────────────────────────────────
# ВЫБОР ИНСТРУМЕНТА
# ─────────────────────────────────────────────────────────────
st.subheader("1. Инструмент")

ticker = st.selectbox(
    "Фьючерс",
    tickers,
    format_func=lambda t: f"{t} — {instruments[t].name}",
)
inst = instruments[ticker]

c_a, c_b, c_c = st.columns(3)
c_a.metric("Цена", f"{inst.price:,.{inst.decimals}f}")
c_b.metric("Шаг / ст. шага", f"{inst.min_step:g} / {inst.step_price:g} ₽")
c_c.metric("ГО L / S", f"{inst.margin_long:,.0f} / {inst.margin_short:,.0f} ₽")

st.caption(
    f"1 пункт = {inst.point_value:.4f} ₽ · "
    f"Экспирация: {inst.expiry} · ОИ: {inst.open_interest:,.0f}"
)


# ─────────────────────────────────────────────────────────────
# ПАРАМЕТРЫ СДЕЛКИ
# ─────────────────────────────────────────────────────────────
st.subheader("2. Сделка")

direction = st.radio("Направление", ["LONG", "SHORT"], horizontal=True)

default_entry = float(inst.price) if inst.price > 0 else 100_000.0
fmt = f"%.{inst.decimals}f"
step_val = float(inst.min_step) if inst.min_step > 0 else 0.01

c1, c2, c3 = st.columns(3)
entry = c1.number_input("Цена входа", value=default_entry, step=step_val, format=fmt)
sl    = c2.number_input("Стоп-лосс",  value=round(default_entry * 0.99, inst.decimals), step=step_val, format=fmt)
tp    = c3.number_input("Тейк-профит", value=round(default_entry * 1.03, inst.decimals), step=step_val, format=fmt)


# ─────────────────────────────────────────────────────────────
# ВАЛИДАЦИЯ
# ─────────────────────────────────────────────────────────────
warnings = []
if direction == "LONG":
    if sl >= entry:
        warnings.append("LONG: стоп должен быть ниже цены входа.")
    if tp <= entry:
        warnings.append("LONG: тейк должен быть выше цены входа.")
else:
    if sl <= entry:
        warnings.append("SHORT: стоп должен быть выше цены входа.")
    if tp >= entry:
        warnings.append("SHORT: тейк должен быть ниже цены входа.")

for w in warnings:
    st.warning(w)


# ─────────────────────────────────────────────────────────────
# РАСЧЁТЫ
# ─────────────────────────────────────────────────────────────
sl_dist = abs(entry - sl)
tp_dist = abs(tp - entry)

step = inst.min_step if inst.min_step > 0 else 1.0
step_value = inst.step_price if inst.step_price > 0 else 1.0

sl_steps = sl_dist / step
tp_steps = tp_dist / step
risk_per_ct = sl_steps * step_value
profit_per_ct = tp_steps * step_value

margin = inst.margin(direction)  # ГО с учётом направления

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
        st.error("Ни один контракт не помещается по риску/ГО.")


# ─────────────────────────────────────────────────────────────
# ДЕТАЛИ
# ─────────────────────────────────────────────────────────────
with st.expander("Детали расчёта"):
    st.markdown(
        f"""
**SL:** `{sl_dist:,.{inst.decimals}f}` пт = `{sl_steps:.2f}` шагов × `{step_value:g}` ₽ = **{risk_per_ct:,.2f} ₽ / контракт**

**TP:** `{tp_dist:,.{inst.decimals}f}` пт = `{tp_steps:.2f}` шагов × `{step_value:g}` ₽ = **{profit_per_ct:,.2f} ₽ / контракт**

**Бюджет риска:** `{deposit:,.0f} ₽ × {risk_pct}%` = **{risk_budget:,.2f} ₽**

**Qty по риску:** `{risk_budget:,.0f} ₽ // {risk_per_ct:,.0f} ₽` = **{qty_by_risk} конт.**

**Qty по ГО:** `({deposit:,.0f} ₽ × {margin_util}%) // {margin:,.0f} ₽` = **{qty_by_margin} конт.**

**Рекомендуемый объём:** `min({qty_by_risk}, {qty_by_margin})` = **{qty_rec} конт.**

**При {qty_rec} конт.:**
- Прибыль при TP: **{profit_per_ct * qty_rec:,.0f} ₽**
- Убыток при SL: **{risk_per_ct * qty_rec:,.0f} ₽**
- Фактический риск: **{(risk_per_ct * qty_rec) / deposit * 100 if deposit else 0:.2f}%** от депозита
"""
    )

st.divider()
st.caption(
    "Данные ГО и шага цены — из MOEX ISS через `fetch_moex_forts` / `fetch_moex_ims`. "
    "Категория риска влияет на размер ГО. Комиссии не учитываются."
)
