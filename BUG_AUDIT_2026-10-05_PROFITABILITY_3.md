# Deep Audit — Profitability Blockers (2026-10-05)

**Scope:** Live repo `origin/main` @ `5c837f9` ka poora code + real production data (`storage/trades/diary/*.json` — 519 closed trades, 2026-09-03 se 2026-10-05; `virtual_portfolio_state.json`; `trades_master.csv`; `reports/full_report.csv`).
**Method:** 5 alag subsystem audits (exits, stops/targets, entry pipeline, sizing/accounting, signal quality) + trade-level data forensics. Har CRITICAL/HIGH finding ka code maine khud dobara padh ke verify kiya. Jo cheez sirf data-analysis se aayi hai ya verify nahi hui, wo clearly "SUSPECTED" ya "estimate" likhi hai.

Pichle audit (`BUG_AUDIT_2026-09-18.md`) me jo fix ho chuka hai, wo yahan dobara nahi hai.

---

## 0. Pehle meri galti maan leta hoon

Pichle jawab me maine 6 points diye the. Usme **sabse badi wajah hi miss ho gayi thi** — risk engine ka "unsafe" exit, jo 90% trades ko 1-2 din me kaat deta hai (neeche C1–C4). Aur maine kaha tha ki ₹5 wale small-caps loss ka kaaran hain — data ne ye **galat** saabit kiya: ₹50 se kam price wale 46 trades ne **+₹2,233** kamaye (win rate 61%). Real-life slippage ka risk wahan hai, lekin paper loss unse nahi aa raha.

Aur ek bug **maine khud** 2026-10-05 ke fix me daala (M10 dekho).

---

## 1. Paisa kahan gaya — exit reason ke hisaab se

| Exit reason | Trades | Win % | Net P&L | Avg hold (din) | Avg final % |
|---|---|---|---|---|---|
| Risk engine "unsafe" (forced exit) | **467** | 45% | **+₹90** | 2.2 | −0.27% |
| Stop-loss | 31 | 0% | **−₹9,778** | 6.1 | **−6.73%** |
| Trend reversal | 10 | 20% | −₹999 | 4.7 | −1.86% |
| Target | 9 | 89% | +₹2,760 | 9.2 | +7.64% |
| High volatility | 2 | 50% | +₹5 | 1.0 | +0.13% |
| **Total** | **519** | **43%** | **≈ −₹7,922** | | |

Seedhi baat: **90% trades ko system khud 2 din me ~0% pe bech deta hai.** Jo bachte hain, unme se slow-losers wide stop pe −6.7% pe katte hain, aur winners target tak pahunchne hi nahi dete. Sirf 9 trades ne kabhi target dekha. 234 trades usi din khule aur band hue (net −₹2,117).

---

## 2. CRITICAL — inke rehte system profitable ho hi nahi sakta

### C1. Entry wala RiskManager har existing position pe dobara chalta hai, aur "unsafe" = turant FULL EXIT
- `paper_trading/paper_trading_engine.py:416` — `"emergency_exit": not risk_result.safe,`
- `risk/exit_strategy.py:654-662` — `if emergency_exit: action = FULL_EXIT, exit_percent = 100.0`. Ye branch stop-loss (`:668`) aur target (`:695`) se **pehle** check hoti hai, isliye sabko override karti hai.
- `risk/risk_manager.py:122` `MAX_TOTAL_RISK = 35.0`, `:695` `safe = total_risk <= self.MAX_TOTAL_RISK`.
- **Asar:** 467/519 exits. Ye risk score "naya trade lena chahiye ya nahi" ke liye bana tha, "khule trade ko bechna hai ya nahi" ke liye nahi.
- **Imaandari se:** in 467 trades ne seedha paisa nahi gawaya (net +₹90). Inka nuksaan ye hai ki trades ko 2 din me kaat dene se winners ko badhne ka mauka hi nahi milta. Ye ek inference hai — fix ke baad real data se hi pakka hoga (section 7 dekho). 467 me se 51 exits ValidationEngine reject se aaye (total_risk 100), baaki 416 ne 35 ki line cross ki.

### C2. Entry aur exit me koi buffer nahi, aur exit check entry ke 2 minute baad chalta hai
- Entry aur exit dono ke liye same threshold (35) hai. Koi minimum holding period, grace period ya alag exit threshold nahi hai.
- `.github/workflows/morning_executor.yml:5` 9:16 IST pe chalta hai; `.github/workflows/paper_trading.yml:4-6` uske khatam hote hi (`workflow_run`) chal jaata hai.
- Morning Executor entry pe weighted RiskManager **chalata hi nahi** — uski apni docstring yahi kehti hai (`scripts/morning_executor.py:248-256`). Matlab entry ek kamzor check se approve hoti hai, aur 2 minute baad strict check se bech di jaati hai.
- **Data:** 234 trades same-day close, median ~2.4 minute baad. Example: ACMESOLAR 09-18 ko 411.5 pe khula, 118 second baad +4.18% pe "unsafe" bol ke bech diya.

### C3. Gap risk direction nahi dekhta — accha gap-up bhi "risk" hai
- `features/indicators/breakout.py:69-70` — `gap_up = gap_pct >= 1.0`, `gap_down = gap_pct <= -1.0`
- `risk/risk_manager.py:228-230` — `if gap_up or gap_down: gap_risk = 75.0` (weight 0.09 → +5.85 points)
- Morning Executor 1.75×ATR tak ke gap pe khud entry leta hai (`morning_executor.py:64-65`), aur wahi gap agle check me position ko unsafe bana deta hai. Long position ke liye favourable gap-up bhi position ko kill karta hai.
- **Counterfactual (logged components se recompute):** 416 weighted-unsafe exits me se 221 sirf gap=10 karne se safe rehte.

### C4. Bot ki apni positions ki ginti har position ko "unsafe" banati hai (feedback loop)
- `risk/risk_manager.py:442-446` — `if open_positions >= 15: portfolio_risk += 35.0` (weight 0.14 → +4.9 points), plus exposure bands.
- Bot roz 40–57 positions kholta hai. Isse har position ka risk score badhta hai → positions bechi jaati hain → risk girta hai → agle din wapis khareedta hai.
- **Counterfactual:** portfolio risk = 0 karne pe 416 me se sirf 35 unsafe bachte. Gap aur portfolio dono hata do to **0**.

### C5. Stop aur target ka structure hi ghaate ka hai
- `risk/stop_target.py:36-40` — `ATR_STOP = 2.0`, `PARTIAL_TARGET = 2.0`, `FINAL_TARGET = 3.5` → **Target1 = 1R, Target2 = 1.75R**. Stop kabhi Target1 se paas nahi hota.
- Real ATR se rebuild karne pe median stop distance ~7.7% hai, jabki trades ka average best open profit (MFE) sirf ~1.2% hai. Matlab target door hai, stop pahunch me.
- 1R target, ~1.2R real loss (gap-through ki wajah se, H1) aur day-low fill ke saath **breakeven ke liye ~54% win rate chahiye. System ka 43% hai.**

### C6. Asli position sizing engine kabhi use hi nahi hota
- `scripts/morning_executor.py:384-385`:
  `allocation = min(available * 0.05, 500000.0 / max(len(candidates), 1))`
  `quantity = int(allocation / open_price)`
- `risk/position_sizing.py` ka risk-based quantity (`MAX_RISK_PER_TRADE = 0.02`) `scanner.py` me calculate hota hai, lekin `generate_full_report.py:592-604` use `candidates_order.json` me likhta hi nahi. ₹5,00,000 hardcoded hai.
- `max_trade_candidates: 100` (`generate_full_report.py:342`) aur `MAX_OPEN_POSITIONS = 100` (`core/constants.py:53`).
- **Asar:** 09-21 ko 50 candidates, median position ₹3,565. Har trade capital ka ~0.1% risk karta hai, design ke 2% ke bajaye. 40+ chhoti positions — jo khud C4 ko trigger karti hain.

---

## 3. HIGH — har ek ko fix karna zaroori hai

### H1. Stop din me sirf ek baar, 9:20 pe, kuch minute purane bar pe check hota hai
- `data/market_data.py:98` — `end = datetime.now() + pd.Timedelta(days=1)` aaj ka adhoora bar le aata hai; `latest_low` sirf pehle kuch minute ka low hai.
- Kal ka poora intraday range stop ke against kabhi check nahi hota. Din me stop toota to agli subah ke open pe bikta hai — gap-down seedha stop ke paar.
- **Real examples:** IKIO −10.66%, APOLLO −10.62%, TEJASNET −9.40%, GNFC −9.15% (est. stop ≈ −5.1%). Average configured stop −5.72% vs real −6.73%.

### H2. Stop day ke LOW pe fill, target day ke HIGH pe fill
- `risk/exit_strategy.py:618` `stop_touch_price = day_low`, `:678` `resolved_exit_price = stop_touch_price`. Targets ke liye `day_high` (`:705`, `:721`).
- Real stop order stop price pe ya gap ho to open pe bharta hai. 31 stops me se 30 ka fill us waqt ke market price se bura hai. Dono taraf galat; net effect P&L ko thoda neeche dikhata hai (31 stops vs 9 targets).

### H3. Target1 roz dobara fire hota hai — position har din aadhi
- "Partial already taken" ka koi flag nahi (verify kiya: `exit_strategy.py` / `paper_trading_engine.py` me aisa koi state nahi). `:363` `if partial_touch_price >= partial_target` → `:715` `exit_percent = 50.0` har din.
- **Data:** SOUTHBANK 38 → 19 → 10 → 4 shares chaar din lagatar.

### H4. Break-even sticky nahi, aur trailing stop initial stop se bhi dheela
- Break-even sirf aaj ke price se decide hota hai (`exit_strategy.py:266`), store nahi hota.
- `ATR_TRAILING = 3.0` (`:123`) initial 2.0 ATR stop se zyada wide hai — Target1 ke baad bhi trailing stop entry se neeche reh sakta hai.
- **Data:** MCX 5.5% MFE ke baad +0.39% pe band; GRANULES 5.29% MFE ke baad +0.10%.

### H5. Stop roz current ATR se dobara calculate hota hai — volatility badhe to stop door chala jaata hai
- `exit_strategy.py` ~`:225-234` — stop store nahi hota, har run me `entry_price` aur aaj ke `atr_14` se naya banta hai. GNFC/HAL/APOLLO me selloff ke din stop 0.2–0.5% aur neeche gaya.

### H6. Bot pehle se gap-up ho chuke stocks khareedta hai
- Sirf ek chase guard hai: `morning_executor.py:348` `if target1 and open_price >= target1`. 1.75×ATR gap-up pe 0.25 ATR upside vs 3.75 ATR downside bachta hai.
- **Data (maine verify kiya):** BUY with gap ≥ +1%: **115 trades, −₹6,140, win 33%** — poore net loss ka ~78%. Gap < 1%: 240 trades, −₹1,748. Gap-down ≤ −1%: 141 trades, −₹1,248, win 48%.

### H7. Decision gate ek hi signal ko do baar (aur ek milte-julte signal ko teesri baar) gin raha hai
- `decision/decision_engine.py:211-215` — `buy_combined = score*0.35 + probability*0.35 + confidence*0.30`
- `strategy/buy_probability.py:96-98` — `probability = 100 / (1 + exp(-(score - 50)/10))` — ye `buy_score.overall` ka hi sigmoid hai, real probability nahi. Matlab 70% weight ek hi signal ka hai.
- `buy_strategy.py:1274` — `confidence = round(overall_score, 2)` — ye strategy ka apna score hai (`tier2*0.45 + tier3*0.55`). `buy_score.overall` se exactly same nahi, lekin bahut overlap hai (`buy_score.technical` = tier2). *(Correction: pehle draft me "teen baar same score" likha tha — independent re-check me ye partly true nikla.)*
- System ki "win probability" 74–95% dikhati hai; real win rate 43%. `BUY_THRESHOLD = 70`, `MIN_CONFIDENCE`, `MIN_PROBABILITY` (`decision_engine.py:146-152`) kisi decision me use hi nahi hote.

### H8. Entry signal me timing ka edge lagbhag nahi hai
- `buy_strategy.py:1195` — `overall = tier2*0.45 + tier3*0.55`. Bina news ke tier3 = aadha fundamentals (quarterly badalte hain) + aadha market context (stock ka apna regime label). Uptrend wale stock ko 58 threshold ki taraf ~19 points free mil jaate hain.
- Roz scan hue ~2,400 stocks me se 9–18% ko BUY milta hai — gate selective nahi.
- **Supporting evidence (subagent analysis, `reports/full_report.csv`, 14 scan days, 2026-09-18/10-05 ke fixes se pehle ka data — proof nahi, sirf indication):** score ka agle 5 din ke excess return se rank correlation ≈ −0.016, yaani lagbhag zero.
- Diary data bhi yahi kehta hai: `entry_thesis_confidence` 50–70 ke saare buckets loss me hain; sirf 70+ (42 trades) positive.

### H9. Ranking ulta kaam kar raha lagta hai (SUSPECTED — maine khud recompute nahi kiya)
- `decision_engine.py:259` — ranking = score*0.5 + probability*0.5 (dono same score).
- Subagent ne 490 trades ko unki entry-night scan row se match kiya: top-third ranking ne −₹4,693 gawaye, bottom-third ne +₹570 kamaye.

### H10. Score ke kuch hisse constant hain ya galat stocks ko pass karte hain
- Volatility factor practically hamesha ~50: `atr_filter` (`atr_14 < close*0.05`, `buy_strategy.py:490-492`) lagbhag har stock pe True. ADX < 20 pe isko 40% weight milta hai (`:1090`).
- ADX < 20 pe trend state `"RANGE"` ho jaata hai (`:1216-1217`), aur veto sirf `"DOWNTREND"` reject karta hai (`state_rules.py:100`) — trendless stocks trend-entry pass kar lete hain.
- Overextension cap 4×ATR% (`:664-682`) dheela hai; RSI > 70 reject nahi, sirf ek vote kam.

### H11. Re-entry cooldown nahi, aur duplicate check dead code hai
- `risk/portfolio_rules.py:479-488` `candidate_symbol` aur `holdings` padhta hai — dono kahin set hi nahi hote, isliye check kabhi trigger nahi hota. Ye sirf dead code hai: duplicate open position phir bhi nahi ban sakti, kyunki `portfolio.py:140` already-open symbol ko mana kar deta hai. Asli masla cooldown ka na hona hai.
- **Data (verify kiya):** 222 symbols me se 125 do ya zyada baar trade hue; 297 re-entries ka net **−₹3,666**. STEELXIND 10 baar.

### H12. Company news check kabhi chalta hi nahi
- `scripts/morning_executor.py:107` `h.get("published")` padhta hai, jabki `data/news_data.py:142` `"published_at"` likhta hai → har headline drop.
- 543 entries me se **0** ne kabhi company news evaluate ki.
- Fix karne pe bhi: `core/trading_calendar.py:93` `now_ist()` IST time deta hai lekin tzinfo UTC rehta hai → cutoff 5.5 ghante galat hoga.

### H13. Transaction cost zero maana gaya hai
- Paper path me brokerage, STT, stamp, DP charge, slippage kuch nahi (`execution/broker.py` me fields hain, lekin paper path use import nahi karta).
- **Estimate (subagent, real turnover ₹28.7L buy / ₹28.6L sell pe):** STT ₹5,738 + stamp ₹431 + exchange/SEBI/GST ₹203 + DP ~₹8,268 ≈ **₹14,600**. Isse net P&L ≈ **−₹22,500**, profit factor ≈ 0.52. Ye estimate hai — broker plan ke hisaab se badlega.

### H14. SELL trades real me ho hi nahi sakte
- 23 SELL positions kai din hold hui (+₹1,214). NSE cash me retail short usi din square off karna padta hai; overnight short sirf F&O stocks me futures se, jinme ye symbols nahi hain.

---

## 4. MEDIUM

| # | Bug | Jagah | Asar |
|---|---|---|---|
| M1 | Liquidity risk hamesha ≥20 kyunki `turnover` column kabhi banta hi nahi | `risk_manager.py:321` | Har position pe +2.4 constant risk points; 99 exits akele isse unsafe hue |
| M2 | Held position pe validation reject → forced exit ("Average volume too low", "Circuit breaker active"), shayad 9:20 ke adhoore bar ki wajah se (SUSPECTED) | `risk_manager.py:156-173`, `market_data.py:189`, `circuit_bands.py:274-280` | 51 trades, −₹1,661 |
| M3 | Trend reversal exit ek hi bar ke `ema_20 < ema_50` pe, jabki BUY usi condition me entry le sakta hai | `exit_strategy.py:589`, `buy_strategy.py:901` | 10 trades, −₹999; 5 pehle hi check pe bahar |
| M4 | High volatility pe roz 50% bechna, bina state ke | `exit_strategy.py:759-767` | Position baar baar aadhi |
| M5 | Cash accounting me unrealized P&L mix ho jaata hai; `add_position` exposure update nahi karta | `portfolio.py:132-176`, `:377`, `:422` | ~₹697 cash drift; morning checks stale exposure dekhte hain |
| M6 | Daily/weekly loss limits morning executor me enforce nahi; 15% drawdown pe sab ek saath day-low pe bikega | `morning_executor.py:248-296`, `portfolio_limits.py:126` | Abhi active nahi (DD ~2.2%) |
| M7 | Diary me `buy_probability`/`buy_confidence` hardcoded 0.0, CSV OPEN rows me regime "N/A"; learning engine trade ko latest scan row se match karta hai, entry-night row se nahi | `morning_executor.py:413,438`, `learning_engine.py:65` | Analytics/reports galat. Live parameters pe asar nahi (learning sirf observe karta hai — verify hua) |
| M8 | `candidates_order.json` ki date aaj se compare nahi hoti — raat ka scan fail ho to purana file dobara execute hoga | `morning_executor.py:311-313` | Abhi tak hua nahi, lekin risk hai |
| M9 | Backtest engine me look-ahead: signal bar ke close pe fill aur purane dino ke liye aaj ke fundamentals | `analytics/backtest_engine.py:266`, `scripts/run_backtest.py:179` | Koi bhi backtest zarurat se zyada accha dikhega — tuning se pehle fix zaroori |
| M10 | **Mera bug (2026-10-05 fix):** sector_score me stock khud bhi gina jaata hai, jabki comment "every OTHER" kehta hai | `execution/scanner.py` `_compute_universe_context()` | Chhote sectors me stock ka apna regime hi sector score ban jaata hai. Fix: khud ko exclude karo |
| M11 | `highest_price`/`lowest_price`/MFE/MAE sirf 9:20 snapshot se track hote hain | `portfolio.py:183-184` | MFE/MAE kam dikhte hain; trailing stop inhi pe chalta hai |
| M12 | Delivery % BUY aur SELL dono me bina invert kiye; `_volatility_score` rupee ATR use karta hai (`atr < 1` → bonus, sasta stock = "kam volatile"); `_risk_score` lagbhag constant | `buy_strategy.py:1053-1057`, `sell_strategy.py:1001-1005`, `buy_scoring.py:424-431,453-525` | Score me noise |

---

## 5. Jo check kiya aur sahi nikla (refuted)

- Live trading me look-ahead bias nahi mila: scan 20:30 IST pe complete bar pe, fill agle din ke open pe. `chikou_span` (future-shifted) kisi signal me use nahi hota.
- Same din same symbol ki duplicate entry nahi hui.
- Learning engine live parameters nahi badalta (`learning_engine.py:9`, optimizer ka output sirf email report padhta hai).
- `repair_exit_prices.py` wala purana NaN-exit bug ab guarded hai (`paper_trading_engine.py:307`).

---

## 6. Fix karne ka order (meri recommendation)

Ek blunt baat pehle: **ye saare bugs fix karna zaroori hai, lekin kaafi hai ya nahi, ye guarantee nahi.** Churn band hone ke baad bachi hui trades ko 1R target aur near-random entry ka saamna karna padega. Har phase ke baad paper data dekh ke hi agla kadam.

| Phase | Kya | Kyun pehle |
|---|---|---|
| **A — Churn band** | C1–C4: exit ke liye alag (zyada) threshold ya sirf hard-risk overrides; minimum hold (aaj khuli position ko aaj exit-check se bahar); gap risk direction-aware; portfolio-count ko per-position exit se hatao | 467 trades yahin mar rahe hain; iske bina baaki fixes ka asar dikhega hi nahi |
| **B — Stop/target/fill** | C5, H1–H5: R:R badlo (backtest ke baad), stop sirf tight ho sakta hai, partial-taken flag, sticky break-even, fill = `min(open, stop)`, kal ka poora range check | Stop losses akele poore net loss se zyada hain |
| **C — Entry & sizing** | C6, H6, H11, H12, M8: position_sizing.py wire karo, positions 10–15 tak, gap-up chase filter, cooldown, news key fix, stale-file check | Chhoti positions aur gap-up entries |
| **D — Signal** | H7–H10, M12: decision gate de-dup, tier3 weight cap, RANGE veto, constant checks hatao | Edge banane ka kaam — sabse lamba |
| **E — Realism** | H13, H14, M9: cost model, SELL sirf intraday/F&O ya band, backtest look-ahead fix | Taaki numbers sach bolein |

---

## 7. Is audit ko kaise verify karein

**Independent re-check (ho chuka):** Ek alag reviewer ko sirf claims diye gaye, conclusions nahi, aur kaha gaya ki inhe galat saabit karo. 18 core claims (11 code + 7 data) me se 16 exactly CONFIRMED, 2 PARTLY TRUE (H7 aur H11 — dono upar correct kar diye hain). Saare data numbers raw diary JSON se dobara calculate karke match hue (jaise −₹7,922.35, 467 trades / +₹90.11, 31 stops / −₹9,778.41, same-day median 2.4 minute).

**Khud check karne ke liye (GitHub pe, 5 minute):**
1. `paper_trading/paper_trading_engine.py` line 416 → `"emergency_exit": not risk_result.safe,`
2. `risk/stop_target.py` lines 36–40 → `ATR_STOP = 2.0`, `PARTIAL_TARGET = 2.0`
3. `scripts/morning_executor.py` line 384 → `allocation = min(available * 0.05, 500000.0 / ...)`
4. `scripts/morning_executor.py` line 107 `"published"` vs `data/news_data.py` line 142 `"published_at"`
5. `storage/trades/diary/` me koi bhi 10 files kholo — lagbhag 9 me `exit_reason` "Risk engine flagged this symbol as unsafe" se shuru hoga.

**Sabse pakka test — falsifiable prediction:** Agar C1–C4 sahi hain, to Phase A fix ke 3–5 trading din baad:
- "Risk engine flagged" exits 90% se gir ke 10% se kam ho jaane chahiye,
- same-day open+close lagbhag 0 hone chahiye,
- average holding 2.2 din se kaafi upar jaana chahiye.

Agar aisa nahi hua, to audit ka ye hissa galat tha.

**Kya pakka hai, kya nahi:** C1–C6 aur zyadatar HIGH findings code se proven hain. Transaction cost (₹14,600) ek estimate hai. H9 (ranking ulta) aur M2 SUSPECTED hain. Aur "churn ki wajah se winners nahi bante" ek inference hai, jo upar wale test se hi saabit hoga.

---

## 8. Corrections aur fix status (2026-10-06)

**Correction — C6 (position sizing):** sizing engine bypass hona sahi tha, lekin `reports/full_report.csv` me sizing engine khud bhi median **₹6,196** position deta hai — executor jitni hi chhoti. Sirf wire karne se positions badi nahi hongi. Aur size badhane se profit factor nahi badalta, sirf rupee me nuksaan/faayda bada hota hai. Isliye sizing ka kaam tab tak ruka hai jab tak system profit me na aaye.

**Correction — H6 (gap-up entries):** 115 gap-up BUY trades ke −₹6,140 me se −₹5,108 risk-engine exits se aaya tha (jo Phase A me band hue). Asli nuksaan ka saboot MFE hai: gap-up entries ka average best move 0.66% vs normal 1.56%.

**Fix ho chuke (deliver kiye, upload baaki):**
- Phase A (2026-10-06): C1, C2, C3, C4, M1, M2, M10 + H2, H3, H4 (sticky break-even), H5
- Phase B2/C1 (2026-10-06): H1, M11, H12, M8, H6 (1% chase filter)

- Phase M9 (2026-10-06): backtest engine ab live flow replay karta hai — raat ka scan, agle din open pe entry (Morning Executor ke rules ke saath), roz ExitStrategyEngine se stop/target/risk exits, date se alignment, deterministic fills, optional cost. Pehle wala backtest signal ke close pe bharta tha aur usme stop/target/exit engine tha hi nahi — positions sirf ulte signal pe band hoti thi.

- 2026-10-06 batch: H13 (transaction cost model — paper trading + backtest, rates config.py me), M5 (cash/P&L accounting ledger se; purani state file load pe khud theek hoti hai), M6 (morning executor weekly/monthly/drawdown limit), M7 (diary/journal me asli probability/confidence/regime). M7 ka learning-engine wala hissa (trade ko entry-night scan row se match karna) abhi baaki — sirf reporting.

**Baaki:** C5 (1R target — ab naye backtest se tune hoga), H7–H10 (signal), H11 (cooldown — data se zyada support nahi: re-entries per trade first entries se bure nahi), H14 (SELL overnight), M3, M4, M7 (learning-engine matching), M12. Targets bhi abhi roz ke ATR se dobara bante hain — stop ki tarah inhe bhi entry pe fix karna baaki hai.

---

## 9. CRITICAL (2026-10-06) — raat ka scan 22 Sep se band tha

**Saboot:** "Daily scan update" commits 22 Sep ke baad sirf chhutti ke dino (26, 27 Sep, 2, 3, 4 Oct) pe hain, aur unme sirf `telegram_dedup.json` badla. `reports/candidates_order.json` aur `reports/full_report.csv` ka aakhri update 21 Sep ka hai. 22 Sep – 6 Oct ke **112 me se 112 entries** usi 21 Sep wali 30-symbol list se thi — executor roz purani list purane prev_close/stop/target ke saath dobara execute kar raha tha (STEELXIND 10 baar, H11 re-entry churn ki asli wajah).

**Wajah (strong hypothesis, Actions logs se confirm karna baaki):** watchlist `nifty500.json` me 2,395 symbols, har symbol pe 3 network calls; scan 4h40m–5h54m le raha tha (start 15:00 UTC, commit 19:42–20:54 UTC), GitHub Actions ki 6 ghante ki limit paar hone lagi.

**Fix (deliver):** liquid symbols pehle (NSE turnover history se), 300 minute ka time budget (budget khatam to jo scan hua uske candidates phir bhi likhe jaate hain), fundamentals 7 din tak cache (actions/cache). M8 (stale file guard) ab purani file execute nahi hone deta.

**Ek aur zaroori disclosure:** production scan (`generate_full_report.py`) har symbol ke liye `scan_symbol()` alag se chalata hai — `scan_symbols()` wala two-pass path use hi nahi karta. Isliye ye do fixes **live scan me kabhi active hi nahi hue**: (1) 2026-09-18 ka fundamental percentile-ranking ("STRUCTURAL BUY BIAS FIX"), (2) 2026-10-05 ka sector score + breadth blend. Ye sirf backtest/orchestrator path me chalte hain. Inhe live me chalu karna ek alag faisla hai (entry signals badlenge, aur Pass 1 ka fetch time scan budget me fit karna hoga).
