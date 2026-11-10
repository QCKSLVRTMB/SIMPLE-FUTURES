import streamlit as st
import pandas as pd
import requests
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# НАСТРОЙКИ СТРАНИЦЫ
# ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Калькулятор фьючерсов MOEX",
    page_icon="📊",
    layout="centered",  # centered лучше для мобильных
    initial_sidebar_state="collapsed"
)

# ─────────────────────────────────────────────────────────────
# МОБИЛЬНАЯ ОПТИМИЗАЦИЯ (CSS)
# ─────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* Убираем лишние отступы на мобильных */
.block-container { padding-top: 1rem; padding-bottom: 1rem; }
/* Колонки на узких экранах складываются в одну */
@media (max-width: 640px) {
    [data-testid="column"] { width: 100% !important; flex: 1 1 100% !important; }
}
/* Кнопки на всю ширину */
.stButton > button { width: 100%; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# ЗАГРУЗКА ДАННЫХ ИЗ MOEX ISS
# ─────────────────────────────────────────────────────────────
@st.cache_data(ttl=300, show_spinner=False)
def load_futures():
    """Возвращает DataFrame со всеми фьючерсами и их параметрами."""
    url = (
        "https://iss.moex.com/iss/engines/futures/markets/forts/securities.json"
        "?iss.meta=off&iss.only=securities"
    )
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()["securities"]
        df = pd.DataFrame(data["data"], columns=data["columns"])
        # Оставляем только активные контракты (есть цена шага и ГО)
        df = df[df["STEPPRICE"].notna() & df["MINSTEP"].notna()]
        df = df[df["STEPPRICE"] > 0]
        return df
    except Exception as e:
        st.error(f"Не удалось загрузить список фьючерсов: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=300, show_spinner=False)
def load_margin():
    """Возвращает DataFrame с ГО (начальная маржа) по фьючерсам."""
    url = (
        "https://iss.moex.com/iss/rms/engines/futures/objects/limits.json"
        "?iss.meta=off&iss.only=limits"
    )
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()["limits"]
        df = pd.DataFrame(data["data"], columns=data["columns"])
        # Оставляем нужные колонки: тикер, ГО лонг, ГО шорт
        df = df[["SECID", "MR1", "MR2"]].drop_duplicates("SECID")
        df = df.rename(columns={"MR1": "GO_LONG", "MR2": "GO_SHORT"})
        return df
    except Exception as e:
        st.error(f"Не удалось загрузить ГО: {e}")
        return pd.DataFrame()

# Загружаем один раз
futures_df = load_futures()
margin_df = load_margin()

if futures_df.empty:
    st.stop()

# Объединяем
df = futures_df.merge(margin_df, on="SECID", how="left")
# Если ГО не пришло — ставим заглушку, чтобы не падало
df["GO_LONG"]  = df["GO_LONG"].fillna(0)
df["GO_SHORT"] = df["GO_SHORT"].fillna(0)

# ─────────────────────────────────────────────────────────────
# ЗАГОЛОВОК
# ─────────────────────────────────────────────────────────────
st.title("Калькулятор фьючерсов MOEX")
st.caption("Простой расчёт объёма позиции и проверки RR / MM")

# ─────────────────────────────────────────────────────────────
# САЙДБАР: ПАРАМЕТРЫ СЧЁТА
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
    rr_req = st.number_input(
        "Требуемый RR", min_value=0.5, max_value=20.0, value=3.0, step=0.5
    )

# ─────────────────────────────────────────────────────────────
# ВЫБОР ИНСТРУМЕНТА
# ─────────────────────────────────────────────────────────────
st.subheader("1. Инструмент")

# Строим удобные метки: "SiZ5 — Si-12.25"
df["label"] = df["SECID"] + " — " + df["SHORTNAME"].fillna("")
labels = df["label"].tolist()

selected_label = st.selectbox("Фьючерс", labels)
row = df[df["label"] == selected_label].iloc[0]

# Параметры инструмента
ticker = row["SECID"]
step = float(row["MINSTEP"])            # шаг цены
step_value = float(row["STEPPRICE"])    # стоимость шага
go_long = float(row["GO_LONG"])
go_short = float(row["GO_SHORT"])

st.caption(
    f"Шаг: {step:g} · стоимость шага: {step_value:g} ₽ · "
    f"ГО Long: {go_long:,.0f} ₽ · ГО Short: {go_short:,.0f} ₽"
)

# ─────────────────────────────────────────────────────────────
# ПАРАМЕТРЫ СДЕЛКИ
# ─────────────────────────────────────────────────────────────
st.subheader("2. Сделка")
direction = st.radio("Направление", ["LONG", "SHORT"], horizontal=True)

c1, c2, c3 = st.columns(3)
entry = c1.number_input("Цена входа", value=100_000.0, format="%.4f")
sl    = c2.number_input("Стоп-лосс", value=99_400.0, format="%.4f")
tp    = c3.number_input("Тейк-профит", value=101_800.0, format="%.4f")

go = go_long if direction == "LONG" else go_short

# ─────────────────────────────────────────────────────────────
# ВАЛИДАЦИЯ НАПРАВЛЕНИЯ
# ─────────────────────────────────────────────────────────────
warnings = []
if direction == "LONG":
    if sl >= entry:
        warnings.append("Для LONG стоп-лосс должен быть ниже цены входа.")
    if tp <= entry:
        warnings.append("Для LONG тейк-профит должен быть выше цены входа.")
else:
    if sl <= entry:
        warnings.append("Для SHORT стоп-лосс должен быть выше цены входа.")
    if tp >= entry:
        warnings.append("Для SHORT тейк-профит должен быть ниже цены входа.")

for w in warnings:
    st.warning(w)

# ─────────────────────────────────────────────────────────────
# РАСЧЁТЫ
# ─────────────────────────────────────────────────────────────
sl_dist = abs(entry - sl)
tp_dist = abs(tp - entry)

sl_steps = sl_dist / step if step > 0 else 0
tp_steps = tp_dist / step if step > 0 else 0
risk_per_ct   = sl_steps * step_value    # ₽ риска на 1 контракт
profit_per_ct = tp_steps * step_value    # ₽ прибыли на 1 контракт

risk_budget   = deposit * risk_pct / 100
qty_by_risk   = int(risk_budget // risk_per_ct)          if risk_per_ct > 0 else 0
qty_by_margin = int((deposit * margin_util / 100) // go) if go > 0 else 0
qty_rec       = min(qty_by_risk, qty_by_margin)

rr_actual = (tp_dist / sl_dist) if sl_dist > 0 else 0.0

rr_ok = (rr_actual >= rr_req) and (sl_dist > 0) and (tp_dist > 0)
mm_ok = (qty_rec >= 1) and not warnings

# ─────────────────────────────────────────────────────────────
# РЕЗУЛЬТАТ
# ─────────────────────────────────────────────────────────────
st.subheader("3. Результат")

m1, m2, m3 = st.columns(3)
m1.metric("Расчётный RR", f"{rr_actual:.2f}")
m2.metric("Риск на 1 контракт", f"{risk_per_ct:,.0f} ₽")
m3.metric("Рекомендуемый объём", f"{qty_rec} конт.")

col_rr, col_mm = st.columns(2)
with col_rr:
    st.markdown("**Проверка RR**")
    if rr_ok:
        st.success(f"RR = {rr_actual:.2f} ≥ {rr_req:.1f}")
    else:
        st.error(f"RR = {rr_actual:.2f} < {rr_req:.1f}")

with col_mm:
    st.markdown("**Проверка MM**")
    if mm_ok:
        st.success(
            f"Помещается {qty_rec} конт. "
            f"(риск ≤ {risk_budget:,.0f} ₽, ГО ≤ {deposit:,.0f} ₽)"
        )
    elif warnings:
        st.error("Некорректные цены (см. предупреждения)")
    else:
        st.error("Ни один контракт не помещается по риску или ГО")

# ─────────────────────────────────────────────────────────────
# ДЕТАЛИ РАСЧЁТА
# ─────────────────────────────────────────────────────────────
with st.expander("Детали расчёта"):
    st.markdown(f"""
**SL:** `{sl_dist:,.4f}` пт = `{sl_steps:.2f}` шагов × `{step_value:g}` ₽ = **{risk_per_ct:,.2f} ₽ / контракт**

**TP:** `{tp_dist:,.4f}` пт = `{tp_steps:.2f}` шагов × `{step_value:g}` ₽ = **{profit_per_ct:,.2f} ₽ / контракт**

**Бюджет риска:** `{deposit:,.0f} ₽ × {risk_pct}%` = **{risk_budget:,.2f} ₽**

**Qty по риску:** `{risk_budget:,.0f} ₽ // {risk_per_ct:,.0f} ₽` = **{qty_by_risk} конт.**

**Qty по ГО:** `({deposit:,.0f} ₽ × {margin_util}%) // {go:,.0f} ₽` = **{qty_by_margin} конт.**

**Рекомендуемый объём:** `min({qty_by_risk}, {qty_by_margin})` = **{qty_rec} конт.**

**При {qty_rec} конт.:**
- Прибыль при TP: **{profit_per_ct * qty_rec:,.0f} ₽**
- Убыток при SL: **{risk_per_ct * qty_rec:,.0f} ₽**
- Фактический риск: **{(risk_per_ct * qty_rec) / deposit * 100 if deposit else 0:.2f}%** от депозита
""")

st.divider()
st.caption(
    "Данные ГО и шага цены загружаются из MOEX ISS и могут запаздывать. "
    "Комиссии не учитываются. Не является инвестиционной рекомендацией."
)
