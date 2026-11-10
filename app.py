"""
Простейший калькулятор фьючерсов MOEX.
Один файл — весь проект.

Запуск локально:   streamlit run app.py
Деплой:            streamlit.io → New app → выбрать репозиторий и app.py
"""

import streamlit as st

# ─────────────────────────────────────────────────────────────
# Справочник популярных фьючерсов MOEX.
# ⚠ Значения ОРИЕНТИРОВОЧНЫЕ — уточняйте на moex.com (ГО меняется
#   со временем, стоимость шага у RTS/BR/GOLD зависит от курса USD).
# Пользователь всегда может ввести свои значения в «Другой».
# ─────────────────────────────────────────────────────────────
PRESETS = {
    "Si (USD/RUB)":      {"step": 1,    "step_value": 1.0,  "go_long": 7_000,  "go_short": 7_000},
    "RTS (индекс РТС)":  {"step": 10,   "step_value": 20.0, "go_long": 7_000,  "go_short": 7_000},
    "BR (Brent)":        {"step": 0.01, "step_value": 7.7,  "go_long": 8_000,  "go_short": 8_000},
    "SBRF (Сбербанк)":   {"step": 1,    "step_value": 1.0,  "go_long": 1_000,  "go_short": 1_000},
    "GAZR (Газпром)":    {"step": 1,    "step_value": 1.0,  "go_long": 500,    "go_short": 500},
    "LKOH (Лукойл)":     {"step": 1,    "step_value": 1.0,  "go_long": 3_000,  "go_short": 3_000},
    "GOLD (золото)":     {"step": 0.1,  "step_value": 9.0,  "go_long": 10_000, "go_short": 10_000},
}

st.set_page_config(page_title="Калькулятор фьючерсов MOEX", page_icon="📊", layout="centered")
st.title("📊 Калькулятор фьючерсов MOEX")
st.caption("Простой расчёт объёма позиции и проверки RR / MM")

# ───────────── 0. Параметры счёта (сайдбар) ─────────────
with st.sidebar:
    st.header("Параметры счёта")
    deposit     = st.number_input("Депозит, ₽",             value=100_000.0, step=10_000.0, min_value=0.0)
    risk_pct    = st.number_input("Риск на сделку, %",      value=1.0,       step=0.1,      min_value=0.1, max_value=100.0)
    margin_util = st.number_input("Макс. загрузка ГО, %",   value=100.0,     step=10.0,     min_value=10.0, max_value=100.0)
    rr_req      = st.number_input("Требуемый RR",           value=3.0,       step=0.5,      min_value=0.5)

# ───────────── 1. Инструмент ─────────────
st.subheader("1. Инструмент")
ticker = st.selectbox("Фьючерс", list(PRESETS.keys()) + ["Другой (ввести вручную)"])

if ticker == "Другой (ввести вручную)":
    col_a, col_b = st.columns(2)
    with col_a:
        step       = st.number_input("Шаг цены",          value=10.0,    min_value=0.0001, format="%.4f")
        step_value = st.number_input("Стоимость шага, ₽", value=15.0,    min_value=0.01,   format="%.4f")
    with col_b:
        go_long    = st.number_input("ГО Лонг, ₽",        value=20_000.0, min_value=1.0)
        go_short   = st.number_input("ГО Шорт, ₽",        value=20_000.0, min_value=1.0)
else:
    d = PRESETS[ticker]
    step, step_value = d["step"], d["step_value"]
    go_long, go_short = d["go_long"], d["go_short"]
    st.caption(
        f"Шаг: {step} · стоимость шага: {step_value} ₽ · "
        f"ГО Long: {go_long:,} ₽ · ГО Short: {go_short:,} ₽ "
        f"(⚠ проверьте на moex.com)"
    )

# ───────────── 2. Параметры сделки ─────────────
st.subheader("2. Сделка")
direction = st.radio("Направление", ["LONG", "SHORT"], horizontal=True)

c1, c2, c3 = st.columns(3)
entry = c1.number_input("Цена входа",   value=100_000.0, format="%.4f")
sl    = c2.number_input("Стоп-лосс",    value=99_400.0,  format="%.4f")
tp    = c3.number_input("Тейк-профит",  value=101_800.0, format="%.4f")

go = go_long if direction == "LONG" else go_short

# ───────────── 3. Валидация направления ─────────────
warnings = []
if direction == "LONG":
    if sl >= entry: warnings.append("Для LONG SL должен быть ниже входа.")
    if tp <= entry: warnings.append("Для LONG TP должен быть выше входа.")
else:
    if sl <= entry: warnings.append("Для SHORT SL должен быть выше входа.")
    if tp >= entry: warnings.append("Для SHORT TP должен быть ниже входа.")
for w in warnings:
    st.warning(w)

# ───────────── 4. Расчёты ─────────────
sl_dist = abs(entry - sl)
tp_dist = abs(tp - entry)

sl_steps   = sl_dist / step if step > 0 else 0
tp_steps   = tp_dist / step if step > 0 else 0
risk_per_ct   = sl_steps * step_value    # ₽ риска на 1 контракт
profit_per_ct = tp_steps * step_value    # ₽ прибыли на 1 контракт

risk_budget   = deposit * risk_pct / 100
qty_by_risk   = int(risk_budget // risk_per_ct)                if risk_per_ct > 0 else 0
qty_by_margin = int((deposit * margin_util / 100) // go)       if go > 0 else 0
qty_rec       = min(qty_by_risk, qty_by_margin)

rr_actual = (tp_dist / sl_dist) if sl_dist > 0 else 0.0

# Проверки
rr_ok = (rr_actual >= rr_req) and (sl_dist > 0) and (tp_dist > 0)
mm_ok = (qty_rec >= 1) and not warnings

# ───────────── 5. Результат ─────────────
st.subheader("3. Результат")

m1, m2, m3 = st.columns(3)
m1.metric("Расчётный RR",       f"{rr_actual:.2f}")
m2.metric("Риск на 1 контракт", f"{risk_per_ct:,.0f} ₽")
m3.metric("Рекомендуемый объём", f"{qty_rec} конт.")

col_rr, col_mm = st.columns(2)
with col_rr:
    st.markdown("**Проверка RR**")
    if rr_ok:
        st.success(f"🟢 RR = {rr_actual:.2f} ≥ {rr_req:.1f}")
    else:
        st.error(f"🔴 RR = {rr_actual:.2f} < {rr_req:.1f}")

with col_mm:
    st.markdown("**Проверка MM**")
    if mm_ok:
        st.success(f"🟢 Помещается {qty_rec} конт. (риск ≤ {risk_budget:,.0f} ₽, ГО ≤ {deposit:,.0f} ₽)")
    elif warnings:
        st.error("🔴 Некорректные цены (см. предупреждения)")
    else:
        st.error("🔴 Ни один контракт не помещается по риску/ГО")

# ───────────── 6. Детализация ─────────────
with st.expander("📋 Детали расчёта"):
    st.markdown(f"""
**SL:** `{sl_dist:,.4f}` пт = `{sl_steps:.2f}` шагов × `{step_value}` ₽ = **{risk_per_ct:,.2f} ₽ / контракт**

**TP:** `{tp_dist:,.4f}` пт = `{tp_steps:.2f}` шагов × `{step_value}` ₽ = **{profit_per_ct:,.2f} ₽ / контракт**

**Бюджет риска:** `{deposit:,.0f} ₽ × {risk_pct}%` = **{risk_budget:,.2f} ₽**

**Qty по риску:** `{risk_budget:,.0f} ₽ // {risk_per_ct:,.0f} ₽` = **{qty_by_risk} конт.**

**Qty по ГО:** `({deposit:,.0f} ₽ × {margin_util}%) // {go:,.0f} ₽` = **{qty_by_margin} конт.**

**Рекомендуемый объём:** `min({qty_by_risk}, {qty_by_margin})` = **{qty_rec} конт.**

**При {qty_rec} конт.:**
- Прибыль при TP: **{profit_per_ct * qty_rec:,.0f} ₽**
- Убыток при SL:  **{risk_per_ct  * qty_rec:,.0f} ₽**
- Фактический риск: **{(risk_per_ct * qty_rec) / deposit * 100 if deposit else 0:.2f}%** от депозита
""")

st.divider()
st.caption(
    "⚠ Значения ГО / шага цены в пресетах ориентировочные — сверяйтесь с moex.com. "
    "Комиссии не учитываются. Не является инвестиционной рекомендацией."
)
