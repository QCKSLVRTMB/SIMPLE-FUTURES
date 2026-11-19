//@version=6
indicator("QCKSLVR TRADING", shorttitle="QCKSLVR", format=format.volume, overlay=false, max_labels_count=500)

// ─────────────────────────────────────────────────────────────────────
// НАСТРОЙКИ
// ─────────────────────────────────────────────────────────────────────

showSignals = input.bool(true, "Показать все сигналы на графике", group="Отображение сигналов")
showPP      = input.bool(true, "Показать сигналы ПП", group="Отображение сигналов")

showMA         = input.bool(false, "Показать MA объема", group="Гистограмма объема")
lengthVolumeMA = input.int(20, "Период MA объема", minval=1, group="Гистограмма объема")

isMoex          = input.bool(true, "Режим MOEX (5 торговых дней)", group="Поиск аномалий")
periodMultiplier = input.float(1.0, "Множитель периода (1 = стандарт)", step=0.1, group="Поиск аномалий")

anomaly_W    = input.float(100.0, "W: Аномалия (%)", step=10.0, group="Поиск аномалий")
anomaly_D    = input.float(80.0, "D: Аномалия (%)", step=10.0, group="Поиск аномалий")
anomaly_H    = input.float(80.0, "H: Аномалия (%)", step=10.0, group="Поиск аномалий")
anomaly_10m  = input.float(100.0, "10m: Аномалия (%)", step=10.0, group="Поиск аномалий")
anomaly_1m   = input.float(120.0, "1m: Аномалия (%)", step=10.0, group="Поиск аномалий")

moscowTimezone = input.string("Europe/Moscow", "Часовой пояс торговой сессии", group="Торговая сессия")
sessionStartHour = input.int(7, "Начало сессии, час", minval=0, maxval=23, group="Торговая сессия")
sessionStartMinute = input.int(0, "Начало сессии, минута", minval=0, maxval=59, group="Торговая сессия")
sessionEndHour = input.int(23, "Окончание сессии, час", minval=0, maxval=23, group="Торговая сессия")
sessionEndMinute = input.int(50, "Окончание сессии, минута", minval=0, maxval=59, group="Торговая сессия")

showSOS_SOW_W   = input.bool(true, "SOS/SOW на Недельном (W)", group="Видимость: SOS/SOW")
showSOS_SOW_D   = input.bool(true, "SOS/SOW на Дневном (D)", group="Видимость: SOS/SOW")
showSOS_SOW_H   = input.bool(true, "SOS/SOW на Часовом (H)", group="Видимость: SOS/SOW")

showSA_SOT_W    = input.bool(true, "SA/SOT на Недельном (W)", group="Видимость: SA/SOT")
showSA_SOT_D    = input.bool(true, "SA/SOT на Дневном (D)", group="Видимость: SA/SOT")
showSA_SOT_H    = input.bool(true, "SA/SOT на Часовом (H)", group="Видимость: SA/SOT")
showSA_SOT_10m  = input.bool(true, "SA/SOT на 10м", group="Видимость: SA/SOT")
showSA_SOT_1m   = input.bool(true, "SA/SOT на 1м", group="Видимость: SA/SOT")

showND_NS_W     = input.bool(true, "ND/NS на Недельном (W)", group="Видимость: ND/NS")
showND_NS_D     = input.bool(true, "ND/NS на Дневном (D)", group="Видимость: ND/NS")
showND_NS_H     = input.bool(true, "ND/NS на Часовом (H)", group="Видимость: ND/NS")
showND_NS_10m   = input.bool(true, "ND/NS на 10м", group="Видимость: ND/NS")
showND_NS_1m    = input.bool(true, "ND/NS на 1м", group="Видимость: ND/NS")

showPP_W     = input.bool(false, "ПП на Недельном (W)", group="Видимость: ПП")
showPP_D     = input.bool(true,  "ПП на Дневном (D)", group="Видимость: ПП")
showPP_H     = input.bool(true,  "ПП на Часовом (H)", group="Видимость: ПП")

sosSowMinBars = input.int(3, "SOS/SOW: Кол-во баров в импульсе", minval=2, group="Структура SOS/SOW")
sosSowOnlyStrongVolume = input.bool(false, "SOS/SOW: Только чёрный или аномальный объём", group="Структура SOS/SOW")

// Параметры VSA (NR -> NS/ND)
grpCorr = "Коррекция (NR)"
minCorrBars     = input.int(2,  "Мин. кол-во баров в откате",  minval = 2, group = grpCorr, tooltip="Минимальное количество баров в откате, после которого мы начинаем искать сигнал.")
maxCorrBarVol   = input.float(0.30, "Макс. объём одного бара коррекции / Effort Vol", minval = 0.05, group = grpCorr)
maxCloseRange   = input.float(0.50, "Макс. разброс закрытий коррекции × ATR", minval = 0.1, group = grpCorr)
maxStepClosePct = input.float(25.0, "Макс. шаг закрытий отката (% от диапазона усилия)", minval = 0.0, step = 1.0, group = grpCorr)

grpSignal = "Сигнальный бар NS / ND"
dojiBodyRatio   = input.float(0.25, "Дожи: тело ≤ диапазона ×", group = grpSignal)
closeThirdPct   = input.float(0.33, "Граница трети (0.33 = 1/3)", group = grpSignal)

saSotDevPct     = input.float(20.0, "SA/SOT: Отклонение (в % от диапазона/объема)", minval=0.0, maxval=100.0, step=1.0, group="Параметры SA/SOT")
saSotTailRatio  = input.float(1.2, "SA/SOT: Доминирование хвоста", step=0.1, group="Параметры SA/SOT")
saSotAllowRegularVol = input.bool(true, "SA/SOT: Допускать обычные (серые) объёмы", group="Параметры SA/SOT", tooltip="Вкл: анализируются любые объёмы, включая серые (как в исходных правилах). Выкл: только чёрный или аномальный объём сигнального бара.")

vRequireAnomaly = input.bool(true, "V: Только аномальный объем", group="Параметры Перебивка V")
vMaxBars        = input.int(3, "V: Макс. баров для перебивки", minval=1, maxval=10, group="Параметры Перебивка V")

ppVolSpurt      = input.float(25.0, "ПП: Превышение объема (%)", step=5.0, group="Параметры ПП")
ppOnlyAnomaly   = input.bool(false, "ПП: Только аномальный объем", group="Параметры ПП")

showDiag = input.bool(false, "Показать таблицу диагностики", group="Диагностика")

// ─────────────────────────────────────────────────────────────────────
// ТАЙМФРЕЙМ И ВИДИМОСТЬ
// ─────────────────────────────────────────────────────────────────────
string tf = timeframe.period

bool isWeekly = tf == "W" or tf == "1W"
bool isDaily  = tf == "D" or tf == "1D"
bool isHourly = tf == "60" or tf == "1H"
bool is10m    = tf == "10"
bool is1m     = tf == "1"

bool isIntraday = timeframe.isintraday

int barHourMoscow = hour(time, moscowTimezone)
int barMinuteMoscow = minute(time, moscowTimezone)
int barWeekdayMoscow = dayofweek(time, moscowTimezone)

bool isWeekdayMoscow = barWeekdayMoscow >= dayofweek.monday and barWeekdayMoscow <= dayofweek.friday
bool isWeekendMoscow = barWeekdayMoscow == dayofweek.saturday or barWeekdayMoscow == dayofweek.sunday
bool signalsAllowedByDay = not isWeekendMoscow

int barMinutesMoscow = barHourMoscow * 60 + barMinuteMoscow
int sessionStartMinutes = sessionStartHour * 60 + sessionStartMinute
int sessionEndMinutes = sessionEndHour * 60 + sessionEndMinute

bool isMoscowSessionBar = isIntraday and isWeekdayMoscow and barMinutesMoscow >= sessionStartMinutes and barMinutesMoscow < sessionEndMinutes

bool sosSowAllowed = (isWeekly or isDaily or isHourly) and signalsAllowedByDay

bool showSOS_SOW_tf = (isWeekly and showSOS_SOW_W) or (isDaily and showSOS_SOW_D) or (isHourly and showSOS_SOW_H)
bool showSA_SOT_tf  = (isWeekly and showSA_SOT_W) or (isDaily and showSA_SOT_D) or (isHourly and showSA_SOT_H) or (is10m and showSA_SOT_10m) or (is1m and showSA_SOT_1m)
bool showND_NS_tf   = (isWeekly and showND_NS_W) or (isDaily and showND_NS_D) or (isHourly and showND_NS_H) or (is10m and showND_NS_10m) or (is1m and showND_NS_1m)
bool showPP_tf      = (isWeekly and showPP_W) or (isDaily and showPP_D) or (isHourly and showPP_H)

color tfColor = color.gray
if isWeekly
    tfColor := color.purple
else if isDaily
    tfColor := color.blue
else if isHourly
    tfColor := color.green
else if is10m or is1m
    tfColor := color.red

// ─────────────────────────────────────────────────────────────────────
// БАЗОВЫЕ РАСЧЕТЫ И ГИСТОГРАММА ОБЪЕМА
// ─────────────────────────────────────────────────────────────────────
bool isLargeSimple = barstate.isconfirmed and volume > volume[1] and volume > volume[2]

var float volumeMA = na
volumeMA := na(volumeMA) ? volume : volumeMA + (volume - volumeMA) / lengthVolumeMA

float volAvg = na
int activeLookback = 52
float activeAnomalyPct = anomaly_D

if isWeekly
    activeLookback := math.round(52 * periodMultiplier)
    activeAnomalyPct := anomaly_W
else if isDaily
    activeLookback := math.round((isMoex ? 22 : 30) * periodMultiplier)
    activeAnomalyPct := anomaly_D
else if isHourly
    activeLookback := math.round((isMoex ? 85 : 168) * periodMultiplier)
    activeAnomalyPct := anomaly_H
else if is10m
    activeLookback := math.round((isMoex ? 505 : 1008) * periodMultiplier)
    activeAnomalyPct := anomaly_10m
else if is1m
    activeLookback := math.round((isMoex ? 5050 : 10080) * periodMultiplier)
    activeAnomalyPct := anomaly_1m

if not isMoex
    volAvg := ta.sma(volume, activeLookback)
else
    float sumVol = 0.0
    int count = 0
    int i = 0
    while count < activeLookback and i <= bar_index
        int day = dayofweek(time[i])
        if day != dayofweek.saturday and day != dayofweek.sunday
            sumVol += volume[i]
            count += 1
        i += 1
    if count > 0
        volAvg := sumVol / count

bool isAnomaly = not na(volAvg) and volume > volAvg * (1 + activeAnomalyPct / 100)

// VSA базовые расчеты
float atr = ta.atr(14)
float barRange = high - low
float bodySize = math.abs(close - open)
bool isDoji = barRange > 0 and bodySize <= barRange * dojiBodyRatio
float closePos = barRange > 0 ? (close - low) / barRange : 0.5

color histColor = isAnomaly ? tfColor : (isLargeSimple ? color.black : color.gray)
plot(volume, color=histColor, style=plot.style_columns, title="Объем")
plot(showMA ? volumeMA : na, style=plot.style_line, color=color.green, title="MA объема")

// ─────────────────────────────────────────────────────────────────────
// ЛОГИКА СИГНАЛОВ
// ─────────────────────────────────────────────────────────────────────
bool sigBuyBar  = close[0] > close[1]
bool sigSellBar = close[0] < close[1]
bool prevBuy    = close[1] > close[2]
bool prevSell   = close[1] < close[2]

// --- SOS / SOW ---
bool consecBuyUp    = true
bool consecSellDown = true

for i = 0 to sosSowMinBars - 1
    consecBuyUp    := consecBuyUp and close[i] > close[i+1]
    consecSellDown := consecSellDown and close[i] < close[i+1]

bool volumeGrowsInMove = not na(volume[sosSowMinBars - 1]) and volume[0] > volume[sosSowMinBars - 1]

bool strongVolumeInMove = true
for i = 0 to sosSowMinBars - 1
    strongVolumeInMove := strongVolumeInMove and (isLargeSimple[i] or isAnomaly[i])

bool strongVolumeOk = sosSowOnlyStrongVolume ? strongVolumeInMove : true

bool condSOS = signalsAllowedByDay and consecBuyUp and volumeGrowsInMove and strongVolumeOk
bool condSOW = signalsAllowedByDay and consecSellDown and volumeGrowsInMove and strongVolumeOk

bool SOS = showSignals and signalsAllowedByDay and showSOS_SOW_tf and condSOS and barstate.isconfirmed
bool SOW = showSignals and signalsAllowedByDay and showSOS_SOW_tf and condSOW and barstate.isconfirmed

// --- SA / SOT ---
float rangeHigh = math.max(high[0], high[1])
float rangeLow = math.min(low[0], low[1])
float priceRange = rangeHigh - rangeLow
float onePercentOfPriceRange = priceRange > 0 ? priceRange / 100.0 : 0.0

float actualCloseDiff = math.abs(close[0] - close[1])
float closeDevInPctOfRange = onePercentOfPriceRange > 0 ? actualCloseDiff / onePercentOfPriceRange : 0.0
bool saSotCloseOk = closeDevInPctOfRange <= saSotDevPct

float volDevPct = volume[1] == 0 ? 100.0 : math.abs(volume[0] - volume[1]) / volume[1] * 100
bool saSotVolOk = volDevPct <= saSotDevPct

bool previousBarWasSelling = close[1] < open[1]
bool previousBarWasBuying = close[1] > open[1]
bool signalBarBought = close[0] > open[0]
bool signalBarSold = close[0] < open[0]
bool signalGapDown = open[0] < close[1]
bool signalGapUp = open[0] > close[1]

float saBuyTail = close[0] - low[0]
float saSellTail = high[0] - close[0]
bool saTailDominant = saBuyTail >= saSellTail * saSotTailRatio

// Фильтр цвета объёма сигнального бара: чёрный (isLargeSimple) или аномальный (isAnomaly).
// saSotAllowRegularVol = true разрешает также обычные (серые) объёмы (поведение исходных правил).
bool saSotVolColorOk = saSotAllowRegularVol or isLargeSimple or isAnomaly

bool rawSA = previousBarWasSelling and signalBarBought and signalGapDown and saSotVolOk and saSotCloseOk and saTailDominant and saSotVolColorOk

float sotSellTail = high[0] - close[0]
float sotBuyTail = close[0] - low[0]
bool sotTailDominant = sotSellTail >= sotBuyTail * saSotTailRatio

bool rawSOT = previousBarWasBuying and signalBarSold and signalGapUp and saSotVolOk and saSotCloseOk and sotTailDominant and saSotVolColorOk

bool SA = showSignals and signalsAllowedByDay and showSA_SOT_tf and rawSA and barstate.isconfirmed
bool SOT = showSignals and signalsAllowedByDay and showSA_SOT_tf and rawSOT and barstate.isconfirmed

// --- NR → ND / NS (VSA) ---
int barsSinceSOS = ta.barssince(condSOS)
int barsSinceSOW = ta.barssince(condSOW)
float highestHighEffort = ta.highest(high, sosSowMinBars)
float lowestLowEffort = ta.lowest(low, sosSowMinBars)

float ns_maxVol = na
float ns_effortVol = na
float ns_maxClose = na
float ns_minClose = na
float ns_maxStep = na
float ns_maxStepAllowed = na
bool ns_brokeLow = false
bool ns_volWeak = false
bool ns_tight = false
bool ns_stepOk = false
bool ns_signal = false
bool ns_firstBarsOk = false

if not na(barsSinceSOS) and barsSinceSOS >= minCorrBars
    float effortHigh = highestHighEffort[barsSinceSOS]
    float effortLow  = lowestLowEffort[barsSinceSOS]
    ns_effortVol  := volume[barsSinceSOS]
    float effortRange = effortHigh - effortLow

    // Проверка первых двух баров отката (для NS они должны быть ПРОДАЖАМИ)
    ns_firstBarsOk := true
    if barsSinceSOS >= 2
        if close[barsSinceSOS - 1] >= close[barsSinceSOS] or close[barsSinceSOS - 2] >= close[barsSinceSOS - 1]
            ns_firstBarsOk := false

    float maxVol = 0.0
    float maxClose = -1.0
    float minClose = 9999999.0
    float maxStep = 0.0
    bool brokeLow = false

    for i = 1 to barsSinceSOS - 1
        maxVol := math.max(maxVol, volume[i])
        maxClose := math.max(maxClose, close[i])
        minClose := math.min(minClose, close[i])
        if math.abs(close[i] - close[i+1]) > maxStep
            maxStep := math.abs(close[i] - close[i+1])
        if low[i] < effortLow
            brokeLow := true

    if math.abs(close[0] - close[1]) > maxStep
        maxStep := math.abs(close[0] - close[1])
    maxClose := math.max(maxClose, close[0])
    minClose := math.min(minClose, close[0])
    if low[0] < effortLow
        brokeLow := true

    ns_maxVol := maxVol
    ns_maxClose := maxClose
    ns_minClose := minClose
    ns_maxStep := maxStep
    ns_maxStepAllowed := effortRange * maxStepClosePct / 100.0
    ns_brokeLow := brokeLow

    bool isVolumeWeak = maxVol < ns_effortVol * maxCorrBarVol
    bool isTight = (maxClose - minClose) < atr * maxCloseRange
    bool isStepOk = maxStep <= ns_maxStepAllowed

    bool isLowestVolume = volume[0] < volume[1] and volume[0] < volume[2]
    bool nsShapeOk = open[0] > close[0] or isDoji
    bool nsCloseOk = isDoji or closePos <= closeThirdPct

    ns_signal := isVolumeWeak and isTight and isLowestVolume and nsShapeOk and nsCloseOk and not brokeLow and isStepOk and ns_firstBarsOk
    ns_volWeak := isVolumeWeak
    ns_tight := isTight
    ns_stepOk := isStepOk

float nd_maxVol = na
float nd_effortVol = na
float nd_maxClose = na
float nd_minClose = na
float nd_maxStep = na
float nd_maxStepAllowed = na
bool nd_brokeHigh = false
bool nd_volWeak = false
bool nd_tight = false
bool nd_stepOk = false
bool nd_signal = false
bool nd_firstBarsOk = false

if not na(barsSinceSOW) and barsSinceSOW >= minCorrBars
    float effortHigh = highestHighEffort[barsSinceSOW]
    float effortLow  = lowestLowEffort[barsSinceSOW]
    nd_effortVol  := volume[barsSinceSOW]
    float effortRange = effortHigh - effortLow

    // Проверка первых двух баров отката (для ND они должны быть ПОКУПКАМИ)
    nd_firstBarsOk := true
    if barsSinceSOW >= 2
        if close[barsSinceSOW - 1] <= close[barsSinceSOW] or close[barsSinceSOW - 2] <= close[barsSinceSOW - 1]
            nd_firstBarsOk := false

    float maxVol = 0.0
    float maxClose = -1.0
    float minClose = 9999999.0
    float maxStep = 0.0
    bool brokeHigh = false

    for i = 1 to barsSinceSOW - 1
        maxVol := math.max(maxVol, volume[i])
        maxClose := math.max(maxClose, close[i])
        minClose := math.min(minClose, close[i])
        if math.abs(close[i] - close[i+1]) > maxStep
            maxStep := math.abs(close[i] - close[i+1])
        if high[i] > effortHigh
            brokeHigh := true

    if math.abs(close[0] - close[1]) > maxStep
        maxStep := math.abs(close[0] - close[1])
    maxClose := math.max(maxClose, close[0])
    minClose := math.min(minClose, close[0])
    if high[0] > effortHigh
        brokeHigh := true

    nd_maxVol := maxVol
    nd_maxClose := maxClose
    nd_minClose := minClose
    nd_maxStep := maxStep
    nd_maxStepAllowed := effortRange * maxStepClosePct / 100.0
    nd_brokeHigh := brokeHigh

    bool isVolumeWeak = maxVol < nd_effortVol * maxCorrBarVol
    bool isTight = (maxClose - minClose) < atr * maxCloseRange
    bool isStepOk = maxStep <= nd_maxStepAllowed

    bool isLowestVolume = volume[0] < volume[1] and volume[0] < volume[2]
    bool ndShapeOk = close[0] > open[0] or isDoji
    bool ndCloseOk = isDoji or closePos >= (1.0 - closeThirdPct)

    nd_signal := isVolumeWeak and isTight and isLowestVolume and ndShapeOk and ndCloseOk and not brokeHigh and isStepOk and nd_firstBarsOk
    nd_volWeak := isVolumeWeak
    nd_tight := isTight
    nd_stepOk := isStepOk

bool ND = showSignals and signalsAllowedByDay and showND_NS_tf and nd_signal and barstate.isconfirmed
bool NS = showSignals and signalsAllowedByDay and showND_NS_tf and ns_signal and barstate.isconfirmed

// --- Перебивка V ---
var float sessMaxVol = 0
var int sessMaxVolIdx = -1
var float sessMaxVolHigh = na
var float sessMaxVolLow = na
var bool sessMaxVolIsRed = false
var bool sessMaxVolIsAnomaly = false

bool isNewSession = ta.change(time("D")) != 0

if signalsAllowedByDay and (isNewSession or volume > sessMaxVol)
    sessMaxVol := volume
    sessMaxVolIdx := bar_index
    sessMaxVolHigh := high
    sessMaxVolLow := low
    sessMaxVolIsRed := (close < open)
    sessMaxVolIsAnomaly := isAnomaly

int maxOffset = bar_index - sessMaxVolIdx
bool rawVBuy = false
bool rawVSell = false
bool vAnomalyOk = vRequireAnomaly ? sessMaxVolIsAnomaly : true

if signalsAllowedByDay and isIntraday and vAnomalyOk and (maxOffset >= 1 and maxOffset <= vMaxBars)
    if sessMaxVolIsRed
        bool firstBreak = true
        for i = 1 to maxOffset - 1
            if close[i] > sessMaxVolHigh
                firstBreak := false
        if firstBreak and close[0] > sessMaxVolHigh
            rawVBuy := true
    else
        bool firstBreak = true
        for i = 1 to maxOffset - 1
            if close[i] < sessMaxVolLow
                firstBreak := false
        if firstBreak and close[0] < sessMaxVolLow
            rawVSell := true

bool VBuy = showSignals and signalsAllowedByDay and rawVBuy and barstate.isconfirmed
bool VSell = showSignals and signalsAllowedByDay and rawVSell and barstate.isconfirmed

// --- ПП ---
bool ppAnomalyOk = ppOnlyAnomaly ? isAnomaly[0] : true
bool rawPPBuy = prevSell and sigBuyBar and (volume[0] >= volume[1] * (1 + ppVolSpurt / 100)) and ppAnomalyOk and close[0] > high[1]
bool rawPPSell = prevBuy and sigSellBar and (volume[0] >= volume[1] * (1 + ppVolSpurt / 100)) and ppAnomalyOk and close[0] < low[1]

bool PPBuy  = showSignals and signalsAllowedByDay and showPP and showPP_tf and rawPPBuy and barstate.isconfirmed
bool PPSell = showSignals and signalsAllowedByDay and showPP and showPP_tf and rawPPSell and barstate.isconfirmed

// ─────────────────────────────────────────────────────────────────────
// ОТРИСОВКА
// ─────────────────────────────────────────────────────────────────────
plotshape(SOS, title="SOS", style=shape.labelup, location=location.belowbar, color=color.new(color.orange, 100), text="SOS", textcolor=color.orange, size=size.small, force_overlay=true)
plotshape(SOW, title="SOW", style=shape.labeldown, location=location.abovebar, color=color.new(color.orange, 100), text="SOW", textcolor=color.orange, size=size.small, force_overlay=true)

plotshape(SA, title="SA", style=shape.labelup, location=location.belowbar, color=color.new(color.green, 100), text="SA", textcolor=color.green, size=size.small, force_overlay=true)
plotshape(SOT, title="SOT", style=shape.labeldown, location=location.abovebar, color=color.new(color.red, 100), text="SOT", textcolor=color.red, size=size.small, force_overlay=true)

plotshape(ND, title="ND", style=shape.labeldown, location=location.abovebar, color=color.new(color.red, 100), text="ND", textcolor=color.red, size=size.small, force_overlay=true)
plotshape(NS, title="NS", style=shape.labelup, location=location.belowbar, color=color.new(color.green, 100), text="NS", textcolor=color.green, size=size.small, force_overlay=true)

plotshape(PPBuy, title="ПП", style=shape.labelup, location=location.belowbar, color=color.new(color.blue, 100), text="ПП", textcolor=color.blue, size=size.small, force_overlay=true)
plotshape(PPSell, title="ПП", style=shape.labeldown, location=location.abovebar, color=color.new(color.blue, 100), text="ПП", textcolor=color.blue, size=size.small, force_overlay=true)

if VBuy
    label.new(sessMaxVolIdx, low[maxOffset], text="V", style=label.style_label_up, color=color.new(color.green, 100), textcolor=color.green, size=size.small, force_overlay=true)
if VSell
    label.new(sessMaxVolIdx, high[maxOffset], text="V", style=label.style_label_down, color=color.new(color.red, 100), textcolor=color.red, size=size.small, force_overlay=true)

// ─────────────────────────────────────────────────────────────────────
// АЛЕРТЫ (оповещение на открытии нового бара о сигнале [1])
// ─────────────────────────────────────────────────────────────────────
string sigName = ""

if SOS[1]
    sigName := "SOS Сила"
else if SOW[1]
    sigName := "SOW Слабость"
else if SA[1]
    sigName := "SA Поглощение"
else if SOT[1]
    sigName := "SOT Тест"
else if ND[1]
    sigName := "ND Нет спроса"
else if NS[1]
    sigName := "NS Нет предложения"
else if VBuy[1]
    sigName := "Перебивка V Вверх"
else if VSell[1]
    sigName := "Перебивка V Вниз"
else if PPBuy[1]
    sigName := "ПП Вверх"
else if PPSell[1]
    sigName := "ПП Вниз"

if barstate.isnew and sigName != ""
    string barOpen  = str.format_time(time[1], "HH:mm", syminfo.timezone)
    string barClose = str.format_time(time_close[1], "HH:mm", syminfo.timezone)
    string dateStr  = str.format_time(time[1], "yyyy-MM-dd", syminfo.timezone)
    string alert_text = "🔔 <b>Сигнал QCKSLVR</b>\\n\\n"
    alert_text += "📊 <b>Инструмент:</b> " + syminfo.ticker + "\\n"
    alert_text += "⏱ <b>Таймфрейм:</b> " + timeframe.period + "\\n"
    alert_text += "💰 <b>Цена закрытия:</b> " + str.tostring(close[1], format.mintick) + "\\n"
    alert_text += "🕒 <b>Сигнальный бар:</b> " + dateStr + " " + barOpen + "–" + barClose + "\\n\\n"
    alert_text += "⚡️ <b>Сигнал:</b> " + sigName

    string json_msg = "{\"chat_id\":\"-1004347291221\",\"parse_mode\":\"HTML\",\"text\":\"" + alert_text + "\"}"
    alert(json_msg, alert.freq_once_per_bar)

alertcondition(SOS, title="Сигнал SOS", message="SOS")
alertcondition(SOW, title="Сигнал SOW", message="SOW")
alertcondition(SA, title="Сигнал SA", message="SA")
alertcondition(SOT, title="Сигнал SOT", message="SOT")
alertcondition(ND, title="Сигнал ND", message="ND")
alertcondition(NS, title="Сигнал NS", message="NS")
alertcondition(VBuy or VSell, title="Сигнал V", message="V")
alertcondition(PPBuy or PPSell, title="Сигнал ПП", message="ПП")

// ─────────────────────────────────────────────────────────────────────
// ТАБЛИЦА ДИАГНОСТИКИ
// ─────────────────────────────────────────────────────────────────────
var table diagTable = table.new(position.bottom_right, 2, 20, border_width=1, border_color=color.gray)

if showDiag and barstate.islast
    string visStr = str.tostring(showSOS_SOW_tf) + " | " + str.tostring(showSA_SOT_tf) + " | " + str.tostring(showND_NS_tf) + " | " + str.tostring(showPP_tf)
    
    table.cell(diagTable, 0, 0, "Условие", text_color=color.white, bgcolor=color.new(color.blue, 0))
    table.cell(diagTable, 1, 0, "Текущее", text_color=color.white, bgcolor=color.new(color.blue, 0))

    table.cell(diagTable, 0, 1, "ТФ / разрешен", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 1, tf + " / " + str.tostring(sosSowAllowed), text_color=color.white, bgcolor=color.new(sosSowAllowed ? color.green : color.red, 70))

    table.cell(diagTable, 0, 2, "Вид: S/SA/ND/ПП", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 2, visStr, text_color=color.white, bgcolor=color.new((showSOS_SOW_tf or showSA_SOT_tf or showND_NS_tf or showPP_tf) ? color.green : color.red, 70))

    table.cell(diagTable, 0, 3, "Объем / Средний", text_color=color.white, bgcolor=color.new(color.gray, 70))
    float volRatio = na(volAvg) or volAvg == 0 ? na : volume / volAvg
    table.cell(diagTable, 1, 3, (na(volRatio) ? "н/д" : str.tostring(math.round(volRatio * 10) / 10) + "×"), text_color=color.white, bgcolor=color.new(isAnomaly ? color.green : color.red, 70))

    table.cell(diagTable, 0, 4, "Тип бара (Close/Close)", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 4, "Покупки: " + str.tostring(sigBuyBar) + " / Продажи: " + str.tostring(sigSellBar), text_color=color.white, bgcolor=color.new((sigBuyBar or sigSellBar) ? color.green : color.red, 70))

    table.cell(diagTable, 0, 5, "SA/SOT: Отклонение Close", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 5, str.tostring(math.round(closeDevInPctOfRange * 10) / 10) + "% (макс " + str.tostring(saSotDevPct) + "%)", text_color=color.white, bgcolor=color.new(saSotCloseOk ? color.green : color.red, 70))

    table.cell(diagTable, 0, 6, "SA/SOT: Отклонение объема", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 6, str.tostring(math.round(volDevPct * 10) / 10) + "% (макс " + str.tostring(saSotDevPct) + "%)", text_color=color.white, bgcolor=color.new(saSotVolOk ? color.green : color.red, 70))

    table.cell(diagTable, 0, 7, "SA/SOT: Хвосты (Низ/Верх)", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 7, str.tostring(math.round(saBuyTail * 100) / 100) + " / " + str.tostring(math.round(saSellTail * 100) / 100), text_color=color.white, bgcolor=color.new((saTailDominant or sotTailDominant) ? color.green : color.red, 70))

    table.cell(diagTable, 0, 8, "SA/SOT: Open vs Prev Close", text_color=color.white, bgcolor=color.new(color.gray, 70))
    bool openCondition = (previousBarWasSelling and signalGapDown) or (previousBarWasBuying and signalGapUp)
    table.cell(diagTable, 1, 8, str.tostring(open) + " vs " + str.tostring(close[1]), text_color=color.white, bgcolor=color.new(openCondition ? color.green : color.red, 70))

    table.cell(diagTable, 0, 9, "VSA: Баров от SOS/SOW", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 9, "SOS: " + str.tostring(barsSinceSOS) + " / SOW: " + str.tostring(barsSinceSOW), text_color=color.white, bgcolor=color.new(color.gray, 70))

    table.cell(diagTable, 0, 10, "VSA: Первые 2 бара отката", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 10, "NS: " + str.tostring(ns_firstBarsOk) + " / ND: " + str.tostring(nd_firstBarsOk), text_color=color.white, bgcolor=color.new((ns_firstBarsOk or nd_firstBarsOk) ? color.green : color.red, 70))

    table.cell(diagTable, 0, 11, "VSA: Слабый объем отката", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 11, "NS: " + str.tostring(ns_volWeak) + " / ND: " + str.tostring(nd_volWeak), text_color=color.white, bgcolor=color.new((ns_volWeak or nd_volWeak) ? color.green : color.red, 70))

    table.cell(diagTable, 0, 12, "VSA: Узость закрытий", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 12, "NS: " + str.tostring(ns_tight) + " / ND: " + str.tostring(nd_tight), text_color=color.white, bgcolor=color.new((ns_tight or nd_tight) ? color.green : color.red, 70))

    table.cell(diagTable, 0, 13, "VSA: Понижение объема", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 13, str.tostring(volume[0] < volume[1] and volume[0] < volume[2]), text_color=color.white, bgcolor=color.new((volume[0] < volume[1] and volume[0] < volume[2]) ? color.green : color.red, 70))

    table.cell(diagTable, 0, 14, "VSA: Шаг закрытий", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 14, "NS: " + str.tostring(ns_stepOk) + " / ND: " + str.tostring(nd_stepOk), text_color=color.white, bgcolor=color.new((ns_stepOk or nd_stepOk) ? color.green : color.red, 70))

    table.cell(diagTable, 0, 15, "SOS/SOW: Сильные объёмы", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 15, str.tostring(strongVolumeInMove) + (sosSowOnlyStrongVolume ? " (вкл)" : " (выкл)"), text_color=color.white, bgcolor=color.new(strongVolumeOk ? color.green : color.red, 70))

    table.cell(diagTable, 0, 16, "Сырой SOS / SOW", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 16, str.tostring(condSOS) + " / " + str.tostring(condSOW), text_color=color.white, bgcolor=color.new((condSOS or condSOW) ? color.green : color.gray, 70))

    table.cell(diagTable, 0, 17, "Сырой SA / SOT", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 17, str.tostring(rawSA) + " / " + str.tostring(rawSOT), text_color=color.white, bgcolor=color.new((rawSA or rawSOT) ? color.green : color.gray, 70))

    table.cell(diagTable, 0, 18, "Сырой ND / NS (VSA)", text_color=color.white, bgcolor=color.new(color.gray, 70))
    table.cell(diagTable, 1, 18, str.tostring(nd_signal) + " / " + str.tostring(ns_signal), text_color=color.white, bgcolor=color.new((nd_signal or ns_signal) ? color.green : color.gray, 70))

    table.cell(diagTable, 0, 19, "Сырой V / ПП", text_color=color.white, bgcolor=color.new(color.gray, 70))
    string vText = rawVBuy ? "Buy" : rawVSell ? "Sell" : "No"
    string ppText = rawPPBuy ? "Buy" : rawPPSell ? "Sell" : "No"
    table.cell(diagTable, 1, 19, vText + " / " + ppText, text_color=color.white, bgcolor=color.new((rawVBuy or rawVSell or rawPPBuy or rawPPSell) ? color.green : color.gray, 70))
