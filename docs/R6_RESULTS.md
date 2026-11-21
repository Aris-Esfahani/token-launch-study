# R6 results: backtest as analysis (spec 10, 15.3)

Written by `agent backtest` from the saved reports. Analysis only: nothing here was traded. Every number is profit or loss in SOL per signal on a 1 SOL buy (so -0.050 is a loss of 5% of the stake), after all costs; in brackets, 95% intervals from resampling whole launch hours.

**How a trade is simulated** (`config/padre_presets.yaml`, `src/agent/sim/backtest.py`):
- Buy 1 SOL (pump.fun's fees included) 5 s after the decision time (also 2, 5, 10 s), on the reserves after the last trade at or before that second: exact bonding-curve math, or PumpSwap pool math for a coin that already graduated.
- Fees are read from the chain data, per trade: the rates the coin's last trade by a person paid (in a pool, the most common rates of its last trades).
- Each transaction that reaches the chain also pays 0.000505 SOL (priority fee 0.0005 + Solana's base fee of 5,000 lamports, read from the data), failed or not.
- An order fails if the price moved more than 20% against it since the quote 2 s earlier, and 5% of orders fail by chance (also 0%, 10%). A failed buy is not retried; a failed sell is retried one delay later.
- The exit is triggered by the price after each later trade against the price paid; the sell lands one delay after the trigger (a time stop at its planned time).
- **Headline setting: our own buy leaves no trace in the market** (strict: the sale is priced on reserves that never held our SOL, which on a new curve understates it by about 6% of the stake). "Our SOL stays in the reserves" is the generous view: exact when nobody else trades, too kind when others sell after us. The truth is between the two.
- Padre's own fee is counted as 1% of the SOL bought and of the SOL sold. Padre's real fee has not been checked: this is a test setting the user chose, not a measured cost, and 0% is shown as a what-if.
- Not counted: the rent of a token account. A buy that would take every token left on the curve stops there, and what follows is priced on the market as recorded.

## The cuts, chosen on the validation day

The validation day has 22.9 hours of launches (coins launched in its last 65 minutes are left out, as in R5). A pick signal buys at or above its cut (a raw model score); the stage 1 filter buys at or below its cut. In brackets: rows bought on the validation day.

| Signal | T+30 s | T+60 s | T+120 s | T+300 s |
|---|---|---|---|---|
| `lightgbm_not_bad` | 1.7672 (8,055) | 1.7741 (7,503) | 1.7461 (7,201) | 1.8180 (7,143) |
| `logistic_2x_5ph` | -0.2837 (115) | -0.0613 (115) | -0.2370 (115) | -0.4190 (115) |
| `logistic_2x_20ph` | -0.7060 (458) | -0.6851 (458) | -0.9363 (458) | -1.4151 (458) |
| `lightgbm_2x_5ph` | -0.6910 (116) | -0.6915 (115) | -0.7921 (115) | -0.7897 (117) |
| `lightgbm_2x_20ph` | -1.0467 (458) | -1.1178 (458) | -1.2184 (458) | -1.2883 (458) |
| `lightgbm_grad_5ph` | -0.5862 (115) | -0.4186 (115) | -0.4715 (115) | -0.6501 (115) |
| `lightgbm_grad_20ph` | -1.5002 (458) | -1.3570 (458) | -1.2821 (458) | -2.0235 (458) |

**The test days were simulated once.** This report was built again from the same saved trades (no trade was simulated again) after Padre's fee in the headline was changed from 0% to 1%. The fee changes no simulated trade, only what is taken off each result; the earlier headline is the what-if column, and the earlier report is kept beside this one (`test_report_before_20261121T111958.json`).

PumpSwap pool math against the recorded pool trades of the test days: buy, exact tokens: 2,275,540 trades, amounts exact 100.0000%, fees exact 99.9579%, starts where the previous trade ended 99.9996%; buy with a budget: 5,459,900 trades, amounts exact 97.6216%, fees exact 98.6638%, starts where the previous trade ended 99.9998%; routed through several pools: 295,742 trades, amounts exact 0.0000%, fees exact 99.5148%, starts where the previous trade ended 99.9993%; sell: 6,190,718 trades, amounts exact 100.0000%, fees exact 99.9425%, starts where the previous trade ended 99.9997%.

(For a buy with a budget, "amounts exact" also asks that the buyer's whole budget equals what the pool priced plus its three fees. Where it does not, the budget was 0.5 to 1.6% larger: an extra charge outside the pool's fees, most likely a trading app's own fee. The simulated buys pay no such charge unless the Padre what-if is on. Trades routed through several pools are charged once for the whole route and are not used for fees.)

## Test days 2026-11-18, 2026-11-19, simulated once

65,865 coins (30,486 ordinary, 35,379 mayhem), 263,460 coin and decision-time rows, 2,371,140 simulated trades. Signals (rows bought): Every coin 263,460; Rules: pass 8,297; Rules: pass or caution 107,496; LightGBM stage 1: not blocked 47,825; Logistic regression 2x, about 5 per hour 771; Logistic regression 2x, about 20 per hour 3,142; LightGBM 2x, about 5 per hour 758; LightGBM 2x, about 20 per hour 2,842; LightGBM graduation, about 5 per hour 807; LightGBM graduation, about 20 per hour 3,011.

### Exit plan `double_or_half` (take-profit +100%, stop-loss -50%, time stop 30 min)

**By decision time** (5 s delay, headline setting). Mean profit or loss per signal in SOL:

| Signal | T+30 s | T+60 s | T+120 s | T+300 s | All four times | First signal per coin |
|---|---|---|---|---|---|---|
| Every coin | -0.198 (-0.203 to -0.192) | -0.202 (-0.207 to -0.196) | -0.200 (-0.205 to -0.194) | -0.198 (-0.203 to -0.193) | -0.199 (-0.204 to -0.194) | -0.198 (-0.203 to -0.192) |
| Rules: pass | -0.109 (-0.112 to -0.106) | -0.117 (-0.164 to -0.068) | -0.100 (-0.141 to -0.053) | -0.092 (-0.132 to -0.049) | -0.108 (-0.112 to -0.104) | -0.109 (-0.112 to -0.106) |
| Rules: pass or caution | -0.198 (-0.206 to -0.190) | -0.177 (-0.184 to -0.170) | -0.162 (-0.167 to -0.156) | -0.147 (-0.153 to -0.142) | -0.176 (-0.182 to -0.169) | -0.199 (-0.207 to -0.191) |
| LightGBM stage 1: not blocked | -0.139 (-0.149 to -0.126) | -0.128 (-0.138 to -0.116) | -0.132 (-0.136 to -0.126) | -0.123 (-0.128 to -0.116) | -0.131 (-0.137 to -0.123) | -0.132 (-0.140 to -0.122) |
| Logistic regression 2x, about 5 per hour | -0.108 (-0.325 to +0.222) | -0.165 (-0.268 to -0.036) | -0.116 (-0.240 to +0.020) | -0.106 (-0.210 to +0.010) | -0.122 (-0.217 to -0.005) | -0.083 (-0.198 to +0.072) |
| Logistic regression 2x, about 20 per hour | -0.085 (-0.163 to +0.014) | -0.016 (-0.109 to +0.104) | -0.056 (-0.152 to +0.061) | -0.183 (-0.245 to -0.115) | -0.083 (-0.151 to +0.003) | -0.066 (-0.130 to +0.012) |
| LightGBM 2x, about 5 per hour | -0.173 (-0.274 to -0.059) | -0.063 (-0.197 to +0.081) | -0.109 (-0.241 to +0.025) | -0.220 (-0.326 to -0.094) | -0.140 (-0.238 to -0.045) | -0.130 (-0.224 to -0.034) |
| LightGBM 2x, about 20 per hour | -0.162 (-0.241 to -0.056) | -0.121 (-0.177 to -0.064) | -0.086 (-0.173 to +0.019) | -0.161 (-0.223 to -0.084) | -0.131 (-0.184 to -0.067) | -0.093 (-0.150 to -0.024) |
| LightGBM graduation, about 5 per hour | -0.073 (-0.272 to +0.203) | -0.118 (-0.238 to +0.002) | -0.181 (-0.284 to -0.086) | -0.216 (-0.303 to -0.120) | -0.145 (-0.231 to -0.049) | -0.128 (-0.219 to -0.014) |
| LightGBM graduation, about 20 per hour | -0.056 (-0.160 to +0.072) | -0.084 (-0.176 to +0.025) | -0.124 (-0.200 to -0.025) | -0.174 (-0.250 to -0.079) | -0.106 (-0.182 to -0.013) | -0.155 (-0.207 to -0.093) |

What happened to those buys (all four decision times):

- Every coin: 263,460 signals; bought 93%; of those in profit 3.1%; exits: take-profit 3.0%, stop-loss 13%, time 77%
- Rules: pass: 8,297 signals; bought 95%; of those in profit 3.0%; exits: take-profit 0.9%, stop-loss 0.2%, time 94%
- Rules: pass or caution: 107,496 signals; bought 92%; of those in profit 4.2%; exits: take-profit 3.9%, stop-loss 13%, time 75%
- LightGBM stage 1: not blocked: 47,825 signals; bought 95%; of those in profit 4.7%; exits: take-profit 2.8%, stop-loss 5.9%, time 86%
- Logistic regression 2x, about 5 per hour: 771 signals; bought 94%; of those in profit 33%; exits: take-profit 25%, stop-loss 54%, time 15%
- Logistic regression 2x, about 20 per hour: 3,142 signals; bought 93%; of those in profit 29%; exits: take-profit 23%, stop-loss 57%, time 13%
- LightGBM 2x, about 5 per hour: 758 signals; bought 95%; of those in profit 36%; exits: take-profit 31%, stop-loss 55%, time 8.6%
- LightGBM 2x, about 20 per hour: 2,842 signals; bought 93%; of those in profit 31%; exits: take-profit 25%, stop-loss 56%, time 12%
- LightGBM graduation, about 5 per hour: 807 signals; bought 92%; of those in profit 28%; exits: take-profit 25%, stop-loss 62%, time 6.1%
- LightGBM graduation, about 20 per hour: 3,011 signals; bought 91%; of those in profit 21%; exits: take-profit 19%, stop-loss 65%, time 7.3%

**Delay sensitivity** (all four decision times):

| Signal | 2 s | 5 s | 10 s |
|---|---|---|---|
| Every coin | -0.197 (-0.201 to -0.191) | -0.199 (-0.204 to -0.194) | -0.203 (-0.208 to -0.197) |
| Rules: pass | -0.108 (-0.112 to -0.105) | -0.108 (-0.112 to -0.104) | -0.108 (-0.112 to -0.105) |
| Rules: pass or caution | -0.171 (-0.177 to -0.165) | -0.176 (-0.182 to -0.169) | -0.181 (-0.187 to -0.174) |
| LightGBM stage 1: not blocked | -0.132 (-0.138 to -0.125) | -0.131 (-0.137 to -0.123) | -0.130 (-0.137 to -0.122) |
| Logistic regression 2x, about 5 per hour | -0.137 (-0.218 to -0.041) | -0.122 (-0.217 to -0.005) | -0.114 (-0.217 to +0.023) |
| Logistic regression 2x, about 20 per hour | -0.088 (-0.152 to -0.016) | -0.083 (-0.151 to +0.003) | -0.097 (-0.173 to +0.007) |
| LightGBM 2x, about 5 per hour | -0.139 (-0.242 to -0.036) | -0.140 (-0.238 to -0.045) | -0.139 (-0.226 to -0.050) |
| LightGBM 2x, about 20 per hour | -0.142 (-0.190 to -0.089) | -0.131 (-0.184 to -0.067) | -0.132 (-0.194 to -0.054) |
| LightGBM graduation, about 5 per hour | -0.162 (-0.238 to -0.087) | -0.145 (-0.231 to -0.049) | -0.168 (-0.253 to -0.070) |
| LightGBM graduation, about 20 per hour | -0.137 (-0.192 to -0.074) | -0.106 (-0.182 to -0.013) | -0.112 (-0.205 to +0.013) |

**Per day** (all four decision times, 5 s delay; signals in square brackets):

| Signal | 2026-11-18 | 2026-11-19 |
|---|---|---|
| Every coin | -0.199 (-0.206 to -0.192) [139,672] | -0.200 (-0.208 to -0.193) [123,788] |
| Rules: pass | -0.109 (-0.116 to -0.103) [4,549] | -0.107 (-0.112 to -0.101) [3,748] |
| Rules: pass or caution | -0.175 (-0.185 to -0.166) [56,902] | -0.177 (-0.185 to -0.169) [50,594] |
| LightGBM stage 1: not blocked | -0.134 (-0.140 to -0.128) [26,009] | -0.126 (-0.138 to -0.111) [21,816] |
| Logistic regression 2x, about 5 per hour | -0.131 (-0.241 to +0.001) [384] | -0.114 (-0.268 to +0.080) [387] |
| Logistic regression 2x, about 20 per hour | -0.077 (-0.159 to +0.016) [1,511] | -0.088 (-0.198 to +0.048) [1,631] |
| LightGBM 2x, about 5 per hour | -0.179 (-0.310 to -0.036) [373] | -0.102 (-0.228 to +0.034) [385] |
| LightGBM 2x, about 20 per hour | -0.170 (-0.226 to -0.113) [1,400] | -0.092 (-0.186 to +0.016) [1,442] |
| LightGBM graduation, about 5 per hour | -0.230 (-0.321 to -0.133) [383] | -0.068 (-0.191 to +0.067) [424] |
| LightGBM graduation, about 20 per hour | -0.135 (-0.198 to -0.068) [1,481] | -0.078 (-0.208 to +0.083) [1,530] |

**Ordinary vs. mayhem coins** (all four decision times, 5 s delay; signals in square brackets), under the headline setting and with our SOL staying in the reserves:

| Signal | Ordinary | Mayhem | Ordinary, our SOL stays | Mayhem, our SOL stays |
|---|---|---|---|---|
| Every coin | -0.111 (-0.116 to -0.105) [121,944] | -0.276 (-0.280 to -0.271) [141,516] | -0.058 (-0.062 to -0.052) [121,944] | -0.078 (-0.079 to -0.076) [141,516] |
| Rules: pass | -0.108 (-0.112 to -0.104) [8,297] | - [0] | -0.053 (-0.057 to -0.049) [8,297] | - [0] |
| Rules: pass or caution | -0.111 (-0.115 to -0.108) [78,374] | -0.348 (-0.355 to -0.342) [29,122] | -0.057 (-0.060 to -0.053) [78,374] | -0.140 (-0.145 to -0.135) [29,122] |
| LightGBM stage 1: not blocked | -0.129 (-0.136 to -0.122) [47,556] | -0.335 (-0.406 to -0.253) [269] | -0.079 (-0.085 to -0.071) [47,556] | -0.076 (-0.111 to -0.028) [269] |
| Logistic regression 2x, about 5 per hour | -0.124 (-0.220 to -0.002) [751] | -0.063 (-0.354 to +0.321) [20] | -0.104 (-0.201 to +0.018) [751] | -0.016 (-0.308 to +0.377) [20] |
| Logistic regression 2x, about 20 per hour | -0.069 (-0.140 to +0.024) [2,908] | -0.259 (-0.334 to -0.178) [234] | -0.045 (-0.117 to +0.048) [2,908] | -0.165 (-0.233 to -0.088) [234] |
| LightGBM 2x, about 5 per hour | -0.140 (-0.238 to -0.045) [758] | - [0] | -0.120 (-0.220 to -0.025) [758] | - [0] |
| LightGBM 2x, about 20 per hour | -0.117 (-0.176 to -0.046) [2,597] | -0.280 (-0.347 to -0.206) [245] | -0.095 (-0.156 to -0.025) [2,597] | -0.193 (-0.251 to -0.127) [245] |
| LightGBM graduation, about 5 per hour | -0.130 (-0.233 to -0.017) [674] | -0.221 (-0.270 to -0.175) [133] | -0.108 (-0.212 to +0.007) [674] | -0.040 (-0.073 to +0.002) [133] |
| LightGBM graduation, about 20 per hour | -0.036 (-0.136 to +0.089) [2,261] | -0.316 (-0.341 to -0.289) [750] | -0.010 (-0.111 to +0.117) [2,261] | -0.039 (-0.056 to -0.022) [750] |

Mayhem buys that would have taken every token left on the curve (share of signals): Every coin 6.6%; Rules: pass -; Rules: pass or caution 4.0%; LightGBM stage 1: not blocked 19%; Logistic regression 2x, about 5 per hour 0%; Logistic regression 2x, about 20 per hour 0%; LightGBM 2x, about 5 per hour -; LightGBM 2x, about 20 per hour 0%; LightGBM graduation, about 5 per hour 66%; LightGBM graduation, about 20 per hour 51%.

**Other assumptions** (all four decision times, 5 s delay, all coins; one change at a time):

| Signal | Headline | 0% of orders fail | 10% of orders fail | Our SOL stays in the reserves | Padre fee 0% |
|---|---|---|---|---|---|
| Every coin | -0.199 (-0.204 to -0.194) | -0.210 (-0.215 to -0.204) | -0.189 (-0.194 to -0.184) | -0.068 (-0.071 to -0.065) | -0.183 (-0.188 to -0.177) |
| Rules: pass | -0.108 (-0.112 to -0.104) | -0.114 (-0.118 to -0.110) | -0.103 (-0.107 to -0.099) | -0.053 (-0.057 to -0.049) | -0.090 (-0.094 to -0.086) |
| Rules: pass or caution | -0.176 (-0.182 to -0.169) | -0.184 (-0.191 to -0.178) | -0.167 (-0.173 to -0.161) | -0.079 (-0.083 to -0.075) | -0.159 (-0.165 to -0.152) |
| LightGBM stage 1: not blocked | -0.131 (-0.137 to -0.123) | -0.138 (-0.144 to -0.130) | -0.123 (-0.129 to -0.116) | -0.079 (-0.085 to -0.071) | -0.113 (-0.119 to -0.105) |
| Logistic regression 2x, about 5 per hour | -0.122 (-0.217 to -0.005) | -0.136 (-0.231 to -0.021) | -0.108 (-0.202 to +0.007) | -0.102 (-0.196 to +0.016) | -0.105 (-0.200 to +0.013) |
| Logistic regression 2x, about 20 per hour | -0.083 (-0.151 to +0.003) | -0.092 (-0.165 to -0.001) | -0.067 (-0.133 to +0.016) | -0.054 (-0.122 to +0.033) | -0.065 (-0.134 to +0.021) |
| LightGBM 2x, about 5 per hour | -0.140 (-0.238 to -0.045) | -0.141 (-0.242 to -0.045) | -0.134 (-0.225 to -0.046) | -0.120 (-0.220 to -0.025) | -0.122 (-0.221 to -0.026) |
| LightGBM 2x, about 20 per hour | -0.131 (-0.184 to -0.067) | -0.138 (-0.192 to -0.073) | -0.114 (-0.171 to -0.045) | -0.104 (-0.159 to -0.040) | -0.113 (-0.167 to -0.049) |
| LightGBM graduation, about 5 per hour | -0.145 (-0.231 to -0.049) | -0.157 (-0.244 to -0.062) | -0.131 (-0.212 to -0.041) | -0.096 (-0.184 to +0.000) | -0.129 (-0.216 to -0.032) |
| LightGBM graduation, about 20 per hour | -0.106 (-0.182 to -0.013) | -0.119 (-0.197 to -0.024) | -0.088 (-0.166 to +0.007) | -0.017 (-0.095 to +0.079) | -0.090 (-0.167 to +0.003) |

### Exit plan `quick_50_25` (take-profit +50%, stop-loss -25%, time stop 15 min)

**By decision time** (5 s delay, headline setting). Mean profit or loss per signal in SOL:

| Signal | T+30 s | T+60 s | T+120 s | T+300 s | All four times | First signal per coin |
|---|---|---|---|---|---|---|
| Every coin | -0.179 (-0.184 to -0.174) | -0.191 (-0.196 to -0.186) | -0.195 (-0.200 to -0.189) | -0.196 (-0.201 to -0.191) | -0.190 (-0.195 to -0.185) | -0.179 (-0.184 to -0.174) |
| Rules: pass | -0.107 (-0.110 to -0.105) | -0.105 (-0.146 to -0.061) | -0.098 (-0.124 to -0.069) | -0.084 (-0.112 to -0.054) | -0.106 (-0.110 to -0.103) | -0.108 (-0.110 to -0.105) |
| Rules: pass or caution | -0.177 (-0.183 to -0.170) | -0.164 (-0.170 to -0.158) | -0.154 (-0.159 to -0.149) | -0.142 (-0.148 to -0.137) | -0.162 (-0.168 to -0.157) | -0.178 (-0.184 to -0.171) |
| LightGBM stage 1: not blocked | -0.125 (-0.134 to -0.114) | -0.119 (-0.128 to -0.106) | -0.124 (-0.127 to -0.121) | -0.116 (-0.120 to -0.113) | -0.121 (-0.126 to -0.115) | -0.122 (-0.129 to -0.112) |
| Logistic regression 2x, about 5 per hour | -0.056 (-0.214 to +0.177) | -0.157 (-0.242 to -0.060) | -0.061 (-0.136 to +0.016) | -0.117 (-0.180 to -0.048) | -0.094 (-0.156 to -0.012) | -0.051 (-0.132 to +0.062) |
| Logistic regression 2x, about 20 per hour | -0.040 (-0.115 to +0.049) | -0.012 (-0.092 to +0.092) | -0.037 (-0.099 to +0.048) | -0.111 (-0.160 to -0.057) | -0.048 (-0.101 to +0.021) | -0.036 (-0.090 to +0.025) |
| LightGBM 2x, about 5 per hour | -0.128 (-0.207 to -0.043) | -0.060 (-0.148 to +0.034) | -0.124 (-0.191 to -0.054) | -0.132 (-0.223 to -0.022) | -0.111 (-0.170 to -0.047) | -0.108 (-0.168 to -0.041) |
| LightGBM 2x, about 20 per hour | -0.089 (-0.152 to -0.006) | -0.078 (-0.124 to -0.034) | -0.053 (-0.108 to +0.015) | -0.103 (-0.153 to -0.050) | -0.080 (-0.114 to -0.037) | -0.064 (-0.105 to -0.019) |
| LightGBM graduation, about 5 per hour | -0.024 (-0.194 to +0.225) | -0.131 (-0.230 to -0.034) | -0.146 (-0.221 to -0.078) | -0.197 (-0.267 to -0.124) | -0.123 (-0.191 to -0.044) | -0.113 (-0.185 to -0.012) |
| LightGBM graduation, about 20 per hour | -0.033 (-0.124 to +0.071) | -0.099 (-0.174 to +0.001) | -0.119 (-0.176 to -0.046) | -0.152 (-0.208 to -0.089) | -0.097 (-0.154 to -0.026) | -0.134 (-0.175 to -0.085) |

What happened to those buys (all four decision times):

- Every coin: 263,460 signals; bought 93%; of those in profit 3.7%; exits: take-profit 3.5%, stop-loss 16%, time 74%
- Rules: pass: 8,297 signals; bought 95%; of those in profit 3.0%; exits: take-profit 1.9%, stop-loss 2.7%, time 91%
- Rules: pass or caution: 107,496 signals; bought 92%; of those in profit 5.1%; exits: take-profit 4.5%, stop-loss 17%, time 71%
- LightGBM stage 1: not blocked: 47,825 signals; bought 95%; of those in profit 5.7%; exits: take-profit 4.3%, stop-loss 12%, time 79%
- Logistic regression 2x, about 5 per hour: 771 signals; bought 94%; of those in profit 38%; exits: take-profit 31%, stop-loss 51%, time 12%
- Logistic regression 2x, about 20 per hour: 3,142 signals; bought 93%; of those in profit 34%; exits: take-profit 28%, stop-loss 54%, time 10%
- LightGBM 2x, about 5 per hour: 758 signals; bought 95%; of those in profit 43%; exits: take-profit 39%, stop-loss 47%, time 8.6%
- LightGBM 2x, about 20 per hour: 2,842 signals; bought 93%; of those in profit 38%; exits: take-profit 32%, stop-loss 51%, time 10%
- LightGBM graduation, about 5 per hour: 807 signals; bought 92%; of those in profit 33%; exits: take-profit 30%, stop-loss 58%, time 5.2%
- LightGBM graduation, about 20 per hour: 3,011 signals; bought 91%; of those in profit 25%; exits: take-profit 22%, stop-loss 62%, time 7.9%

**Delay sensitivity** (all four decision times):

| Signal | 2 s | 5 s | 10 s |
|---|---|---|---|
| Every coin | -0.186 (-0.190 to -0.181) | -0.190 (-0.195 to -0.185) | -0.195 (-0.200 to -0.190) |
| Rules: pass | -0.106 (-0.109 to -0.102) | -0.106 (-0.110 to -0.103) | -0.106 (-0.109 to -0.103) |
| Rules: pass or caution | -0.156 (-0.161 to -0.151) | -0.162 (-0.168 to -0.157) | -0.169 (-0.175 to -0.163) |
| LightGBM stage 1: not blocked | -0.121 (-0.127 to -0.115) | -0.121 (-0.126 to -0.115) | -0.122 (-0.127 to -0.115) |
| Logistic regression 2x, about 5 per hour | -0.108 (-0.159 to -0.054) | -0.094 (-0.156 to -0.012) | -0.083 (-0.159 to +0.021) |
| Logistic regression 2x, about 20 per hour | -0.049 (-0.094 to +0.004) | -0.048 (-0.101 to +0.021) | -0.054 (-0.121 to +0.039) |
| LightGBM 2x, about 5 per hour | -0.120 (-0.176 to -0.060) | -0.111 (-0.170 to -0.047) | -0.109 (-0.165 to -0.052) |
| LightGBM 2x, about 20 per hour | -0.087 (-0.116 to -0.057) | -0.080 (-0.114 to -0.037) | -0.074 (-0.119 to -0.015) |
| LightGBM graduation, about 5 per hour | -0.134 (-0.183 to -0.083) | -0.123 (-0.191 to -0.044) | -0.130 (-0.202 to -0.032) |
| LightGBM graduation, about 20 per hour | -0.111 (-0.155 to -0.062) | -0.097 (-0.154 to -0.026) | -0.100 (-0.173 to -0.001) |

**Per day** (all four decision times, 5 s delay; signals in square brackets):

| Signal | 2026-11-18 | 2026-11-19 |
|---|---|---|
| Every coin | -0.189 (-0.195 to -0.182) [139,672] | -0.192 (-0.199 to -0.185) [123,788] |
| Rules: pass | -0.106 (-0.111 to -0.102) [4,549] | -0.106 (-0.111 to -0.101) [3,748] |
| Rules: pass or caution | -0.161 (-0.169 to -0.154) [56,902] | -0.164 (-0.171 to -0.157) [50,594] |
| LightGBM stage 1: not blocked | -0.121 (-0.126 to -0.116) [26,009] | -0.121 (-0.130 to -0.107) [21,816] |
| Logistic regression 2x, about 5 per hour | -0.095 (-0.163 to -0.022) [384] | -0.093 (-0.191 to +0.040) [387] |
| Logistic regression 2x, about 20 per hour | -0.051 (-0.118 to +0.028) [1,511] | -0.045 (-0.121 to +0.063) [1,631] |
| LightGBM 2x, about 5 per hour | -0.163 (-0.230 to -0.091) [373] | -0.060 (-0.145 to +0.033) [385] |
| LightGBM 2x, about 20 per hour | -0.108 (-0.145 to -0.069) [1,400] | -0.053 (-0.106 to +0.014) [1,442] |
| LightGBM graduation, about 5 per hour | -0.174 (-0.238 to -0.103) [383] | -0.077 (-0.181 to +0.042) [424] |
| LightGBM graduation, about 20 per hour | -0.098 (-0.160 to -0.036) [1,481] | -0.096 (-0.187 to +0.018) [1,530] |

**Ordinary vs. mayhem coins** (all four decision times, 5 s delay; signals in square brackets), under the headline setting and with our SOL staying in the reserves:

| Signal | Ordinary | Mayhem | Ordinary, our SOL stays | Mayhem, our SOL stays |
|---|---|---|---|---|
| Every coin | -0.107 (-0.110 to -0.102) [121,944] | -0.262 (-0.266 to -0.258) [141,516] | -0.053 (-0.057 to -0.048) [121,944] | -0.064 (-0.065 to -0.063) [141,516] |
| Rules: pass | -0.106 (-0.110 to -0.103) [8,297] | - [0] | -0.051 (-0.055 to -0.048) [8,297] | - [0] |
| Rules: pass or caution | -0.108 (-0.111 to -0.104) [78,374] | -0.308 (-0.313 to -0.303) [29,122] | -0.053 (-0.056 to -0.050) [78,374] | -0.100 (-0.104 to -0.097) [29,122] |
| LightGBM stage 1: not blocked | -0.120 (-0.125 to -0.114) [47,556] | -0.297 (-0.371 to -0.222) [269] | -0.069 (-0.074 to -0.063) [47,556] | -0.041 (-0.070 to -0.003) [269] |
| Logistic regression 2x, about 5 per hour | -0.095 (-0.157 to -0.013) [751] | -0.070 (-0.305 to +0.332) [20] | -0.073 (-0.137 to +0.009) [751] | -0.019 (-0.255 to +0.394) [20] |
| Logistic regression 2x, about 20 per hour | -0.037 (-0.094 to +0.034) [2,908] | -0.184 (-0.236 to -0.118) [234] | -0.012 (-0.070 to +0.059) [2,908] | -0.092 (-0.139 to -0.033) [234] |
| LightGBM 2x, about 5 per hour | -0.111 (-0.170 to -0.047) [758] | - [0] | -0.090 (-0.151 to -0.026) [758] | - [0] |
| LightGBM 2x, about 20 per hour | -0.068 (-0.104 to -0.022) [2,597] | -0.213 (-0.286 to -0.140) [245] | -0.045 (-0.081 to +0.001) [2,597] | -0.126 (-0.183 to -0.062) [245] |
| LightGBM graduation, about 5 per hour | -0.104 (-0.182 to -0.012) [674] | -0.222 (-0.264 to -0.182) [133] | -0.082 (-0.161 to +0.011) [674] | -0.035 (-0.062 to -0.004) [133] |
| LightGBM graduation, about 20 per hour | -0.027 (-0.102 to +0.065) [2,261] | -0.309 (-0.326 to -0.290) [750] | -0.000 (-0.076 to +0.092) [2,261] | -0.031 (-0.043 to -0.019) [750] |

Mayhem buys that would have taken every token left on the curve (share of signals): Every coin 6.6%; Rules: pass -; Rules: pass or caution 4.0%; LightGBM stage 1: not blocked 19%; Logistic regression 2x, about 5 per hour 0%; Logistic regression 2x, about 20 per hour 0%; LightGBM 2x, about 5 per hour -; LightGBM 2x, about 20 per hour 0%; LightGBM graduation, about 5 per hour 66%; LightGBM graduation, about 20 per hour 51%.

**Other assumptions** (all four decision times, 5 s delay, all coins; one change at a time):

| Signal | Headline | 0% of orders fail | 10% of orders fail | Our SOL stays in the reserves | Padre fee 0% |
|---|---|---|---|---|---|
| Every coin | -0.190 (-0.195 to -0.185) | -0.200 (-0.205 to -0.195) | -0.180 (-0.185 to -0.176) | -0.059 (-0.061 to -0.057) | -0.174 (-0.178 to -0.169) |
| Rules: pass | -0.106 (-0.110 to -0.103) | -0.111 (-0.115 to -0.107) | -0.101 (-0.104 to -0.098) | -0.051 (-0.055 to -0.048) | -0.088 (-0.092 to -0.085) |
| Rules: pass or caution | -0.162 (-0.168 to -0.157) | -0.170 (-0.176 to -0.165) | -0.154 (-0.159 to -0.149) | -0.066 (-0.069 to -0.063) | -0.145 (-0.151 to -0.140) |
| LightGBM stage 1: not blocked | -0.121 (-0.126 to -0.115) | -0.128 (-0.133 to -0.121) | -0.114 (-0.119 to -0.108) | -0.069 (-0.074 to -0.063) | -0.103 (-0.108 to -0.097) |
| Logistic regression 2x, about 5 per hour | -0.094 (-0.156 to -0.012) | -0.103 (-0.163 to -0.028) | -0.085 (-0.146 to -0.002) | -0.072 (-0.134 to +0.011) | -0.076 (-0.139 to +0.006) |
| Logistic regression 2x, about 20 per hour | -0.048 (-0.101 to +0.021) | -0.053 (-0.108 to +0.020) | -0.037 (-0.089 to +0.029) | -0.018 (-0.072 to +0.050) | -0.030 (-0.084 to +0.039) |
| LightGBM 2x, about 5 per hour | -0.111 (-0.170 to -0.047) | -0.118 (-0.180 to -0.051) | -0.112 (-0.167 to -0.053) | -0.090 (-0.151 to -0.026) | -0.093 (-0.152 to -0.028) |
| LightGBM 2x, about 20 per hour | -0.080 (-0.114 to -0.037) | -0.087 (-0.121 to -0.044) | -0.077 (-0.107 to -0.036) | -0.052 (-0.085 to -0.010) | -0.062 (-0.096 to -0.019) |
| LightGBM graduation, about 5 per hour | -0.123 (-0.191 to -0.044) | -0.134 (-0.200 to -0.059) | -0.114 (-0.178 to -0.037) | -0.074 (-0.140 to +0.008) | -0.107 (-0.175 to -0.027) |
| LightGBM graduation, about 20 per hour | -0.097 (-0.154 to -0.026) | -0.109 (-0.167 to -0.038) | -0.086 (-0.139 to -0.021) | -0.008 (-0.066 to +0.062) | -0.081 (-0.138 to -0.010) |

### Exit plan `hold_5_min` (time stop 5 min)

**By decision time** (5 s delay, headline setting). Mean profit or loss per signal in SOL:

| Signal | T+30 s | T+60 s | T+120 s | T+300 s | All four times | First signal per coin |
|---|---|---|---|---|---|---|
| Every coin | -0.250 (-0.257 to -0.242) | -0.229 (-0.236 to -0.223) | -0.211 (-0.217 to -0.206) | -0.199 (-0.204 to -0.193) | -0.222 (-0.228 to -0.216) | -0.250 (-0.257 to -0.242) |
| Rules: pass | -0.106 (-0.109 to -0.104) | -0.116 (-0.160 to -0.070) | -0.120 (-0.139 to -0.100) | -0.093 (-0.116 to -0.070) | -0.106 (-0.109 to -0.103) | -0.106 (-0.109 to -0.104) |
| Rules: pass or caution | -0.261 (-0.271 to -0.249) | -0.212 (-0.221 to -0.203) | -0.172 (-0.179 to -0.165) | -0.146 (-0.152 to -0.140) | -0.208 (-0.217 to -0.199) | -0.261 (-0.272 to -0.250) |
| LightGBM stage 1: not blocked | -0.134 (-0.142 to -0.127) | -0.123 (-0.130 to -0.116) | -0.122 (-0.126 to -0.117) | -0.115 (-0.119 to -0.110) | -0.124 (-0.128 to -0.120) | -0.130 (-0.137 to -0.123) |
| Logistic regression 2x, about 5 per hour | -0.210 (-0.367 to +0.031) | -0.184 (-0.309 to -0.039) | -0.132 (-0.227 to -0.032) | -0.165 (-0.220 to -0.105) | -0.173 (-0.257 to -0.071) | -0.155 (-0.250 to -0.031) |
| Logistic regression 2x, about 20 per hour | -0.099 (-0.183 to -0.007) | -0.019 (-0.118 to +0.102) | -0.100 (-0.179 to +0.003) | -0.212 (-0.250 to -0.169) | -0.104 (-0.164 to -0.034) | -0.099 (-0.161 to -0.031) |
| LightGBM 2x, about 5 per hour | -0.091 (-0.230 to +0.051) | -0.027 (-0.187 to +0.140) | -0.158 (-0.277 to -0.050) | -0.215 (-0.317 to -0.106) | -0.126 (-0.237 to -0.017) | -0.102 (-0.198 to -0.007) |
| LightGBM 2x, about 20 per hour | -0.169 (-0.264 to -0.062) | -0.183 (-0.262 to -0.106) | -0.120 (-0.209 to -0.014) | -0.116 (-0.193 to -0.013) | -0.146 (-0.202 to -0.080) | -0.142 (-0.204 to -0.077) |
| LightGBM graduation, about 5 per hour | -0.117 (-0.323 to +0.136) | -0.066 (-0.232 to +0.090) | -0.205 (-0.318 to -0.095) | -0.243 (-0.325 to -0.156) | -0.155 (-0.260 to -0.036) | -0.152 (-0.258 to -0.035) |
| LightGBM graduation, about 20 per hour | -0.086 (-0.187 to +0.032) | -0.140 (-0.247 to -0.022) | -0.183 (-0.260 to -0.090) | -0.172 (-0.262 to -0.053) | -0.142 (-0.213 to -0.060) | -0.215 (-0.266 to -0.157) |

What happened to those buys (all four decision times):

- Every coin: 263,460 signals; bought 93%; of those in profit 1.6%; exits: take-profit 0%, stop-loss 0%, time 93%
- Rules: pass: 8,297 signals; bought 95%; of those in profit 2.6%; exits: take-profit 0%, stop-loss 0%, time 95%
- Rules: pass or caution: 107,496 signals; bought 92%; of those in profit 2.0%; exits: take-profit 0%, stop-loss 0%, time 92%
- LightGBM stage 1: not blocked: 47,825 signals; bought 95%; of those in profit 4.8%; exits: take-profit 0%, stop-loss 0%, time 95%
- Logistic regression 2x, about 5 per hour: 771 signals; bought 94%; of those in profit 33%; exits: take-profit 0%, stop-loss 0%, time 94%
- Logistic regression 2x, about 20 per hour: 3,142 signals; bought 93%; of those in profit 29%; exits: take-profit 0%, stop-loss 0%, time 93%
- LightGBM 2x, about 5 per hour: 758 signals; bought 95%; of those in profit 38%; exits: take-profit 0%, stop-loss 0%, time 95%
- LightGBM 2x, about 20 per hour: 2,842 signals; bought 93%; of those in profit 29%; exits: take-profit 0%, stop-loss 0%, time 93%
- LightGBM graduation, about 5 per hour: 807 signals; bought 92%; of those in profit 25%; exits: take-profit 0%, stop-loss 0%, time 92%
- LightGBM graduation, about 20 per hour: 3,011 signals; bought 91%; of those in profit 19%; exits: take-profit 0%, stop-loss 0%, time 91%

**Delay sensitivity** (all four decision times):

| Signal | 2 s | 5 s | 10 s |
|---|---|---|---|
| Every coin | -0.223 (-0.229 to -0.217) | -0.222 (-0.228 to -0.216) | -0.220 (-0.226 to -0.214) |
| Rules: pass | -0.106 (-0.109 to -0.103) | -0.106 (-0.109 to -0.103) | -0.106 (-0.109 to -0.103) |
| Rules: pass or caution | -0.208 (-0.217 to -0.199) | -0.208 (-0.217 to -0.199) | -0.205 (-0.214 to -0.197) |
| LightGBM stage 1: not blocked | -0.124 (-0.129 to -0.120) | -0.124 (-0.128 to -0.120) | -0.123 (-0.127 to -0.119) |
| Logistic regression 2x, about 5 per hour | -0.174 (-0.262 to -0.078) | -0.173 (-0.257 to -0.071) | -0.148 (-0.240 to -0.031) |
| Logistic regression 2x, about 20 per hour | -0.103 (-0.165 to -0.030) | -0.104 (-0.164 to -0.034) | -0.107 (-0.170 to -0.027) |
| LightGBM 2x, about 5 per hour | -0.142 (-0.254 to -0.032) | -0.126 (-0.237 to -0.017) | -0.123 (-0.224 to -0.026) |
| LightGBM 2x, about 20 per hour | -0.159 (-0.218 to -0.094) | -0.146 (-0.202 to -0.080) | -0.145 (-0.199 to -0.082) |
| LightGBM graduation, about 5 per hour | -0.175 (-0.277 to -0.059) | -0.155 (-0.260 to -0.036) | -0.168 (-0.271 to -0.050) |
| LightGBM graduation, about 20 per hour | -0.150 (-0.220 to -0.068) | -0.142 (-0.213 to -0.060) | -0.151 (-0.225 to -0.059) |

**Per day** (all four decision times, 5 s delay; signals in square brackets):

| Signal | 2026-11-18 | 2026-11-19 |
|---|---|---|
| Every coin | -0.222 (-0.230 to -0.213) [139,672] | -0.223 (-0.232 to -0.216) [123,788] |
| Rules: pass | -0.107 (-0.113 to -0.103) [4,549] | -0.105 (-0.109 to -0.101) [3,748] |
| Rules: pass or caution | -0.205 (-0.218 to -0.191) [56,902] | -0.211 (-0.223 to -0.202) [50,594] |
| LightGBM stage 1: not blocked | -0.124 (-0.130 to -0.119) [26,009] | -0.124 (-0.130 to -0.117) [21,816] |
| Logistic regression 2x, about 5 per hour | -0.195 (-0.294 to -0.086) [384] | -0.152 (-0.276 to +0.006) [387] |
| Logistic regression 2x, about 20 per hour | -0.139 (-0.215 to -0.061) [1,511] | -0.071 (-0.155 to +0.033) [1,631] |
| LightGBM 2x, about 5 per hour | -0.174 (-0.296 to -0.045) [373] | -0.079 (-0.242 to +0.069) [385] |
| LightGBM 2x, about 20 per hour | -0.210 (-0.269 to -0.144) [1,400] | -0.084 (-0.171 to +0.015) [1,442] |
| LightGBM graduation, about 5 per hour | -0.269 (-0.371 to -0.158) [383] | -0.051 (-0.211 to +0.105) [424] |
| LightGBM graduation, about 20 per hour | -0.190 (-0.267 to -0.110) [1,481] | -0.097 (-0.207 to +0.038) [1,530] |

**Ordinary vs. mayhem coins** (all four decision times, 5 s delay; signals in square brackets), under the headline setting and with our SOL staying in the reserves:

| Signal | Ordinary | Mayhem | Ordinary, our SOL stays | Mayhem, our SOL stays |
|---|---|---|---|---|
| Every coin | -0.108 (-0.111 to -0.104) [121,944] | -0.321 (-0.326 to -0.316) [141,516] | -0.054 (-0.058 to -0.051) [121,944] | -0.110 (-0.112 to -0.107) [141,516] |
| Rules: pass | -0.106 (-0.109 to -0.103) [8,297] | - [0] | -0.051 (-0.054 to -0.048) [8,297] | - [0] |
| Rules: pass or caution | -0.108 (-0.110 to -0.105) [78,374] | -0.477 (-0.485 to -0.470) [29,122] | -0.053 (-0.055 to -0.051) [78,374] | -0.230 (-0.235 to -0.225) [29,122] |
| LightGBM stage 1: not blocked | -0.122 (-0.127 to -0.118) [47,556] | -0.416 (-0.488 to -0.344) [269] | -0.071 (-0.076 to -0.067) [47,556] | -0.141 (-0.173 to -0.107) [269] |
| Logistic regression 2x, about 5 per hour | -0.168 (-0.254 to -0.063) [751] | -0.388 (-0.579 to -0.205) [20] | -0.148 (-0.235 to -0.042) [751] | -0.337 (-0.520 to -0.167) [20] |
| Logistic regression 2x, about 20 per hour | -0.073 (-0.137 to +0.001) [2,908] | -0.484 (-0.545 to -0.423) [234] | -0.049 (-0.113 to +0.025) [2,908] | -0.344 (-0.400 to -0.292) [234] |
| LightGBM 2x, about 5 per hour | -0.126 (-0.237 to -0.017) [758] | - [0] | -0.106 (-0.218 to +0.005) [758] | - [0] |
| LightGBM 2x, about 20 per hour | -0.103 (-0.164 to -0.032) [2,597] | -0.601 (-0.659 to -0.543) [245] | -0.081 (-0.143 to -0.010) [2,597] | -0.452 (-0.501 to -0.404) [245] |
| LightGBM graduation, about 5 per hour | -0.123 (-0.244 to +0.002) [674] | -0.316 (-0.375 to -0.259) [133] | -0.101 (-0.223 to +0.025) [674] | -0.115 (-0.144 to -0.088) [133] |
| LightGBM graduation, about 20 per hour | -0.039 (-0.131 to +0.074) [2,261] | -0.454 (-0.481 to -0.425) [750] | -0.013 (-0.105 to +0.101) [2,261] | -0.123 (-0.137 to -0.108) [750] |

Mayhem buys that would have taken every token left on the curve (share of signals): Every coin 6.6%; Rules: pass -; Rules: pass or caution 4.0%; LightGBM stage 1: not blocked 19%; Logistic regression 2x, about 5 per hour 0%; Logistic regression 2x, about 20 per hour 0%; LightGBM 2x, about 5 per hour -; LightGBM 2x, about 20 per hour 0%; LightGBM graduation, about 5 per hour 66%; LightGBM graduation, about 20 per hour 51%.

**Other assumptions** (all four decision times, 5 s delay, all coins; one change at a time):

| Signal | Headline | 0% of orders fail | 10% of orders fail | Our SOL stays in the reserves | Padre fee 0% |
|---|---|---|---|---|---|
| Every coin | -0.222 (-0.228 to -0.216) | -0.234 (-0.240 to -0.227) | -0.211 (-0.216 to -0.205) | -0.084 (-0.086 to -0.082) | -0.206 (-0.212 to -0.200) |
| Rules: pass | -0.106 (-0.109 to -0.103) | -0.111 (-0.114 to -0.108) | -0.101 (-0.104 to -0.098) | -0.051 (-0.054 to -0.048) | -0.088 (-0.091 to -0.085) |
| Rules: pass or caution | -0.208 (-0.217 to -0.199) | -0.218 (-0.227 to -0.209) | -0.197 (-0.206 to -0.189) | -0.101 (-0.106 to -0.096) | -0.191 (-0.200 to -0.182) |
| LightGBM stage 1: not blocked | -0.124 (-0.128 to -0.120) | -0.130 (-0.135 to -0.125) | -0.117 (-0.121 to -0.113) | -0.072 (-0.076 to -0.067) | -0.106 (-0.110 to -0.102) |
| Logistic regression 2x, about 5 per hour | -0.173 (-0.257 to -0.071) | -0.190 (-0.277 to -0.088) | -0.158 (-0.239 to -0.059) | -0.152 (-0.238 to -0.049) | -0.156 (-0.241 to -0.052) |
| Logistic regression 2x, about 20 per hour | -0.104 (-0.164 to -0.034) | -0.109 (-0.172 to -0.034) | -0.089 (-0.147 to -0.022) | -0.071 (-0.131 to -0.002) | -0.086 (-0.146 to -0.016) |
| LightGBM 2x, about 5 per hour | -0.126 (-0.237 to -0.017) | -0.127 (-0.244 to -0.010) | -0.117 (-0.220 to -0.016) | -0.106 (-0.218 to +0.005) | -0.108 (-0.220 to +0.002) |
| LightGBM 2x, about 20 per hour | -0.146 (-0.202 to -0.080) | -0.156 (-0.215 to -0.089) | -0.135 (-0.189 to -0.070) | -0.113 (-0.169 to -0.047) | -0.128 (-0.185 to -0.063) |
| LightGBM graduation, about 5 per hour | -0.155 (-0.260 to -0.036) | -0.171 (-0.278 to -0.050) | -0.143 (-0.240 to -0.036) | -0.103 (-0.207 to +0.014) | -0.139 (-0.245 to -0.020) |
| LightGBM graduation, about 20 per hour | -0.142 (-0.213 to -0.060) | -0.160 (-0.232 to -0.074) | -0.125 (-0.192 to -0.046) | -0.040 (-0.110 to +0.044) | -0.127 (-0.198 to -0.044) |

## Validation day 2026-11-17

The cuts were chosen on this day and the models were tuned and calibrated on it, so the model signals are flattered here. The unfitted signals (every coin, the rules) are not.

37,999 coins (16,163 ordinary, 21,836 mayhem), 151,996 coin and decision-time rows, 1,367,964 simulated trades. Signals (rows bought): Every coin 151,996; Rules: pass 4,228; Rules: pass or caution 57,362; LightGBM stage 1: not blocked 29,902; Logistic regression 2x, about 5 per hour 460; Logistic regression 2x, about 20 per hour 1,832; LightGBM 2x, about 5 per hour 463; LightGBM 2x, about 20 per hour 1,832; LightGBM graduation, about 5 per hour 460; LightGBM graduation, about 20 per hour 1,832.

### Exit plan `double_or_half` (take-profit +100%, stop-loss -50%, time stop 30 min)

**By decision time** (5 s delay, headline setting). Mean profit or loss per signal in SOL:

| Signal | T+30 s | T+60 s | T+120 s | T+300 s | All four times | First signal per coin |
|---|---|---|---|---|---|---|
| Every coin | -0.208 (-0.214 to -0.201) | -0.208 (-0.215 to -0.203) | -0.206 (-0.213 to -0.199) | -0.204 (-0.210 to -0.199) | -0.207 (-0.212 to -0.201) | -0.208 (-0.214 to -0.201) |
| Rules: pass | -0.113 (-0.118 to -0.107) | -0.135 (-0.198 to -0.068) | -0.125 (-0.162 to -0.099) | -0.136 (-0.160 to -0.119) | -0.115 (-0.121 to -0.109) | -0.113 (-0.118 to -0.107) |
| Rules: pass or caution | -0.210 (-0.220 to -0.199) | -0.190 (-0.200 to -0.178) | -0.166 (-0.175 to -0.156) | -0.153 (-0.159 to -0.148) | -0.185 (-0.193 to -0.176) | -0.208 (-0.219 to -0.195) |
| LightGBM stage 1: not blocked | -0.145 (-0.158 to -0.127) | -0.140 (-0.152 to -0.122) | -0.133 (-0.143 to -0.122) | -0.125 (-0.133 to -0.116) | -0.136 (-0.145 to -0.126) | -0.135 (-0.147 to -0.120) |
| Logistic regression 2x, about 5 per hour | -0.094 (-0.208 to +0.020) | +0.113 (-0.031 to +0.274) | -0.112 (-0.218 to +0.014) | -0.229 (-0.379 to -0.070) | -0.081 (-0.172 to +0.011) | -0.058 (-0.165 to +0.043) |
| Logistic regression 2x, about 20 per hour | -0.131 (-0.200 to -0.063) | -0.087 (-0.169 to -0.004) | -0.098 (-0.184 to -0.004) | -0.222 (-0.281 to -0.164) | -0.134 (-0.188 to -0.080) | -0.118 (-0.175 to -0.060) |
| LightGBM 2x, about 5 per hour | -0.030 (-0.124 to +0.085) | +0.054 (-0.029 to +0.162) | -0.171 (-0.279 to -0.059) | -0.216 (-0.357 to -0.061) | -0.091 (-0.162 to -0.016) | -0.097 (-0.181 to +0.007) |
| LightGBM 2x, about 20 per hour | -0.153 (-0.211 to -0.085) | -0.136 (-0.178 to -0.093) | -0.073 (-0.156 to +0.039) | -0.133 (-0.198 to -0.065) | -0.124 (-0.161 to -0.082) | -0.100 (-0.152 to -0.044) |
| LightGBM graduation, about 5 per hour | -0.100 (-0.219 to +0.039) | -0.078 (-0.169 to +0.007) | -0.112 (-0.200 to -0.011) | -0.086 (-0.209 to +0.051) | -0.094 (-0.154 to -0.025) | -0.115 (-0.182 to -0.036) |
| LightGBM graduation, about 20 per hour | -0.049 (-0.163 to +0.060) | -0.110 (-0.197 to -0.020) | -0.068 (-0.160 to +0.036) | -0.143 (-0.224 to -0.057) | -0.093 (-0.161 to -0.017) | -0.159 (-0.219 to -0.102) |

What happened to those buys (all four decision times):

- Every coin: 151,996 signals; bought 93%; of those in profit 3.3%; exits: take-profit 3.2%, stop-loss 13%, time 76%
- Rules: pass: 4,228 signals; bought 95%; of those in profit 2.4%; exits: take-profit 1.0%, stop-loss 0.2%, time 94%
- Rules: pass or caution: 57,362 signals; bought 92%; of those in profit 4.8%; exits: take-profit 4.4%, stop-loss 14%, time 74%
- LightGBM stage 1: not blocked: 29,902 signals; bought 95%; of those in profit 5.6%; exits: take-profit 3.7%, stop-loss 7.1%, time 84%
- Logistic regression 2x, about 5 per hour: 460 signals; bought 94%; of those in profit 36%; exits: take-profit 34%, stop-loss 52%, time 8.3%
- Logistic regression 2x, about 20 per hour: 1,832 signals; bought 93%; of those in profit 30%; exits: take-profit 26%, stop-loss 56%, time 11%
- LightGBM 2x, about 5 per hour: 463 signals; bought 94%; of those in profit 40%; exits: take-profit 36%, stop-loss 54%, time 4.5%
- LightGBM 2x, about 20 per hour: 1,832 signals; bought 91%; of those in profit 32%; exits: take-profit 28%, stop-loss 56%, time 7.6%
- LightGBM graduation, about 5 per hour: 460 signals; bought 93%; of those in profit 34%; exits: take-profit 31%, stop-loss 57%, time 4.6%
- LightGBM graduation, about 20 per hour: 1,832 signals; bought 91%; of those in profit 25%; exits: take-profit 22%, stop-loss 64%, time 5.6%

**Delay sensitivity** (all four decision times):

| Signal | 2 s | 5 s | 10 s |
|---|---|---|---|
| Every coin | -0.204 (-0.209 to -0.199) | -0.207 (-0.212 to -0.201) | -0.210 (-0.216 to -0.204) |
| Rules: pass | -0.116 (-0.122 to -0.109) | -0.115 (-0.121 to -0.109) | -0.116 (-0.121 to -0.110) |
| Rules: pass or caution | -0.181 (-0.189 to -0.172) | -0.185 (-0.193 to -0.176) | -0.191 (-0.198 to -0.183) |
| LightGBM stage 1: not blocked | -0.138 (-0.147 to -0.128) | -0.136 (-0.145 to -0.126) | -0.136 (-0.145 to -0.125) |
| Logistic regression 2x, about 5 per hour | -0.088 (-0.178 to +0.007) | -0.081 (-0.172 to +0.011) | -0.111 (-0.199 to -0.027) |
| Logistic regression 2x, about 20 per hour | -0.122 (-0.174 to -0.069) | -0.134 (-0.188 to -0.080) | -0.145 (-0.198 to -0.091) |
| LightGBM 2x, about 5 per hour | -0.080 (-0.154 to +0.004) | -0.091 (-0.162 to -0.016) | -0.104 (-0.182 to -0.019) |
| LightGBM 2x, about 20 per hour | -0.126 (-0.164 to -0.083) | -0.124 (-0.161 to -0.082) | -0.130 (-0.170 to -0.080) |
| LightGBM graduation, about 5 per hour | -0.074 (-0.131 to -0.005) | -0.094 (-0.154 to -0.025) | -0.102 (-0.163 to -0.029) |
| LightGBM graduation, about 20 per hour | -0.095 (-0.166 to -0.018) | -0.093 (-0.161 to -0.017) | -0.089 (-0.156 to -0.021) |

**Ordinary vs. mayhem coins** (all four decision times, 5 s delay; signals in square brackets), under the headline setting and with our SOL staying in the reserves:

| Signal | Ordinary | Mayhem | Ordinary, our SOL stays | Mayhem, our SOL stays |
|---|---|---|---|---|
| Every coin | -0.117 (-0.123 to -0.111) [64,652] | -0.273 (-0.282 to -0.265) [87,344] | -0.065 (-0.071 to -0.058) [64,652] | -0.074 (-0.076 to -0.073) [87,344] |
| Rules: pass | -0.115 (-0.121 to -0.109) [4,228] | - [0] | -0.061 (-0.066 to -0.054) [4,228] | - [0] |
| Rules: pass or caution | -0.113 (-0.119 to -0.105) [39,705] | -0.347 (-0.358 to -0.337) [17,657] | -0.059 (-0.066 to -0.051) [39,705] | -0.129 (-0.136 to -0.123) [17,657] |
| LightGBM stage 1: not blocked | -0.135 (-0.144 to -0.125) [29,684] | -0.245 (-0.299 to -0.195) [218] | -0.085 (-0.094 to -0.075) [29,684] | -0.039 (-0.082 to -0.003) [218] |
| Logistic regression 2x, about 5 per hour | -0.070 (-0.161 to +0.022) [446] | -0.432 (-0.628 to -0.291) [14] | -0.046 (-0.139 to +0.047) [446] | -0.357 (-0.586 to -0.207) [14] |
| Logistic regression 2x, about 20 per hour | -0.121 (-0.179 to -0.060) [1,681] | -0.277 (-0.377 to -0.176) [151] | -0.097 (-0.156 to -0.035) [1,681] | -0.170 (-0.264 to -0.072) [151] |
| LightGBM 2x, about 5 per hour | -0.089 (-0.160 to -0.016) [462] | -1.007 (-1.007 to -1.007) [1] | -0.070 (-0.142 to +0.004) [462] | -0.046 (-0.046 to -0.046) [1] |
| LightGBM 2x, about 20 per hour | -0.104 (-0.145 to -0.058) [1,569] | -0.241 (-0.316 to -0.161) [263] | -0.082 (-0.123 to -0.035) [1,569] | -0.181 (-0.248 to -0.111) [263] |
| LightGBM graduation, about 5 per hour | -0.039 (-0.115 to +0.067) [371] | -0.322 (-0.385 to -0.261) [89] | -0.016 (-0.093 to +0.092) [371] | -0.090 (-0.126 to -0.054) [89] |
| LightGBM graduation, about 20 per hour | -0.022 (-0.115 to +0.074) [1,396] | -0.317 (-0.345 to -0.288) [436] | +0.004 (-0.090 to +0.102) [1,396] | -0.060 (-0.080 to -0.041) [436] |

Mayhem buys that would have taken every token left on the curve (share of signals): Every coin 7.4%; Rules: pass -; Rules: pass or caution 5.1%; LightGBM stage 1: not blocked 22%; Logistic regression 2x, about 5 per hour 0%; Logistic regression 2x, about 20 per hour 0%; LightGBM 2x, about 5 per hour 0%; LightGBM 2x, about 20 per hour 0%; LightGBM graduation, about 5 per hour 61%; LightGBM graduation, about 20 per hour 52%.

**Other assumptions** (all four decision times, 5 s delay, all coins; one change at a time):

| Signal | Headline | 0% of orders fail | 10% of orders fail | Our SOL stays in the reserves | Padre fee 0% |
|---|---|---|---|---|---|
| Every coin | -0.207 (-0.212 to -0.201) | -0.218 (-0.224 to -0.212) | -0.196 (-0.201 to -0.190) | -0.070 (-0.073 to -0.067) | -0.190 (-0.196 to -0.185) |
| Rules: pass | -0.115 (-0.121 to -0.109) | -0.122 (-0.127 to -0.115) | -0.109 (-0.114 to -0.103) | -0.061 (-0.066 to -0.054) | -0.097 (-0.103 to -0.091) |
| Rules: pass or caution | -0.185 (-0.193 to -0.176) | -0.195 (-0.204 to -0.186) | -0.175 (-0.183 to -0.166) | -0.080 (-0.087 to -0.074) | -0.168 (-0.177 to -0.159) |
| LightGBM stage 1: not blocked | -0.136 (-0.145 to -0.126) | -0.144 (-0.153 to -0.133) | -0.128 (-0.137 to -0.116) | -0.085 (-0.094 to -0.074) | -0.118 (-0.127 to -0.108) |
| Logistic regression 2x, about 5 per hour | -0.081 (-0.172 to +0.011) | -0.106 (-0.201 to -0.009) | -0.063 (-0.151 to +0.025) | -0.056 (-0.148 to +0.037) | -0.062 (-0.155 to +0.031) |
| Logistic regression 2x, about 20 per hour | -0.134 (-0.188 to -0.080) | -0.149 (-0.203 to -0.093) | -0.117 (-0.168 to -0.066) | -0.103 (-0.159 to -0.046) | -0.117 (-0.171 to -0.062) |
| LightGBM 2x, about 5 per hour | -0.091 (-0.162 to -0.016) | -0.100 (-0.183 to -0.012) | -0.082 (-0.150 to -0.008) | -0.070 (-0.141 to +0.004) | -0.073 (-0.145 to +0.002) |
| LightGBM 2x, about 20 per hour | -0.124 (-0.161 to -0.082) | -0.136 (-0.174 to -0.093) | -0.119 (-0.158 to -0.079) | -0.096 (-0.134 to -0.054) | -0.107 (-0.145 to -0.065) |
| LightGBM graduation, about 5 per hour | -0.094 (-0.154 to -0.025) | -0.104 (-0.162 to -0.035) | -0.086 (-0.146 to -0.018) | -0.030 (-0.095 to +0.050) | -0.078 (-0.139 to -0.009) |
| LightGBM graduation, about 20 per hour | -0.093 (-0.161 to -0.017) | -0.105 (-0.172 to -0.033) | -0.088 (-0.153 to -0.023) | -0.011 (-0.084 to +0.066) | -0.077 (-0.145 to -0.000) |

### Exit plan `quick_50_25` (take-profit +50%, stop-loss -25%, time stop 15 min)

**By decision time** (5 s delay, headline setting). Mean profit or loss per signal in SOL:

| Signal | T+30 s | T+60 s | T+120 s | T+300 s | All four times | First signal per coin |
|---|---|---|---|---|---|---|
| Every coin | -0.187 (-0.192 to -0.181) | -0.198 (-0.204 to -0.192) | -0.201 (-0.207 to -0.195) | -0.201 (-0.207 to -0.196) | -0.197 (-0.202 to -0.192) | -0.187 (-0.192 to -0.181) |
| Rules: pass | -0.110 (-0.116 to -0.104) | -0.132 (-0.176 to -0.084) | -0.131 (-0.164 to -0.107) | -0.100 (-0.126 to -0.076) | -0.111 (-0.117 to -0.105) | -0.110 (-0.115 to -0.104) |
| Rules: pass or caution | -0.185 (-0.193 to -0.175) | -0.177 (-0.186 to -0.166) | -0.161 (-0.169 to -0.153) | -0.151 (-0.157 to -0.145) | -0.171 (-0.178 to -0.164) | -0.185 (-0.194 to -0.174) |
| LightGBM stage 1: not blocked | -0.127 (-0.139 to -0.108) | -0.125 (-0.136 to -0.107) | -0.124 (-0.132 to -0.116) | -0.117 (-0.124 to -0.109) | -0.123 (-0.131 to -0.113) | -0.122 (-0.133 to -0.106) |
| Logistic regression 2x, about 5 per hour | -0.042 (-0.124 to +0.042) | -0.007 (-0.095 to +0.071) | -0.081 (-0.182 to +0.019) | -0.158 (-0.251 to -0.064) | -0.072 (-0.120 to -0.027) | -0.082 (-0.143 to -0.023) |
| Logistic regression 2x, about 20 per hour | -0.123 (-0.173 to -0.077) | -0.128 (-0.193 to -0.064) | -0.081 (-0.142 to -0.020) | -0.179 (-0.224 to -0.131) | -0.127 (-0.166 to -0.094) | -0.127 (-0.171 to -0.084) |
| LightGBM 2x, about 5 per hour | -0.096 (-0.170 to +0.003) | -0.029 (-0.138 to +0.055) | -0.101 (-0.221 to +0.003) | -0.186 (-0.308 to -0.063) | -0.103 (-0.157 to -0.055) | -0.122 (-0.190 to -0.041) |
| LightGBM 2x, about 20 per hour | -0.134 (-0.185 to -0.079) | -0.117 (-0.173 to -0.064) | -0.093 (-0.155 to -0.016) | -0.114 (-0.165 to -0.062) | -0.115 (-0.146 to -0.084) | -0.117 (-0.164 to -0.072) |
| LightGBM graduation, about 5 per hour | -0.182 (-0.284 to -0.060) | -0.112 (-0.230 to -0.011) | -0.125 (-0.193 to -0.043) | -0.158 (-0.226 to -0.088) | -0.144 (-0.199 to -0.091) | -0.161 (-0.213 to -0.108) |
| LightGBM graduation, about 20 per hour | -0.094 (-0.206 to +0.037) | -0.139 (-0.228 to -0.041) | -0.106 (-0.174 to -0.021) | -0.151 (-0.234 to -0.066) | -0.123 (-0.188 to -0.041) | -0.165 (-0.220 to -0.104) |

What happened to those buys (all four decision times):

- Every coin: 151,996 signals; bought 93%; of those in profit 4.0%; exits: take-profit 3.7%, stop-loss 17%, time 73%
- Rules: pass: 4,228 signals; bought 95%; of those in profit 3.2%; exits: take-profit 2.0%, stop-loss 4.2%, time 89%
- Rules: pass or caution: 57,362 signals; bought 92%; of those in profit 5.8%; exits: take-profit 5.1%, stop-loss 19%, time 68%
- LightGBM stage 1: not blocked: 29,902 signals; bought 95%; of those in profit 6.8%; exits: take-profit 5.2%, stop-loss 14%, time 75%
- Logistic regression 2x, about 5 per hour: 460 signals; bought 94%; of those in profit 43%; exits: take-profit 40%, stop-loss 51%, time 4.1%
- Logistic regression 2x, about 20 per hour: 1,832 signals; bought 93%; of those in profit 33%; exits: take-profit 29%, stop-loss 58%, time 5.7%
- LightGBM 2x, about 5 per hour: 463 signals; bought 94%; of those in profit 48%; exits: take-profit 44%, stop-loss 47%, time 3.2%
- LightGBM 2x, about 20 per hour: 1,832 signals; bought 91%; of those in profit 36%; exits: take-profit 32%, stop-loss 55%, time 4.6%
- LightGBM graduation, about 5 per hour: 460 signals; bought 93%; of those in profit 37%; exits: take-profit 32%, stop-loss 56%, time 4.1%
- LightGBM graduation, about 20 per hour: 1,832 signals; bought 91%; of those in profit 26%; exits: take-profit 23%, stop-loss 63%, time 5.5%

**Delay sensitivity** (all four decision times):

| Signal | 2 s | 5 s | 10 s |
|---|---|---|---|
| Every coin | -0.193 (-0.198 to -0.188) | -0.197 (-0.202 to -0.192) | -0.202 (-0.208 to -0.196) |
| Rules: pass | -0.112 (-0.118 to -0.106) | -0.111 (-0.117 to -0.105) | -0.110 (-0.115 to -0.105) |
| Rules: pass or caution | -0.165 (-0.172 to -0.158) | -0.171 (-0.178 to -0.164) | -0.180 (-0.187 to -0.173) |
| LightGBM stage 1: not blocked | -0.124 (-0.132 to -0.115) | -0.123 (-0.131 to -0.113) | -0.123 (-0.131 to -0.113) |
| Logistic regression 2x, about 5 per hour | -0.094 (-0.145 to -0.041) | -0.072 (-0.120 to -0.027) | -0.080 (-0.132 to -0.038) |
| Logistic regression 2x, about 20 per hour | -0.127 (-0.164 to -0.094) | -0.127 (-0.166 to -0.094) | -0.128 (-0.165 to -0.095) |
| LightGBM 2x, about 5 per hour | -0.115 (-0.168 to -0.060) | -0.103 (-0.157 to -0.055) | -0.099 (-0.156 to -0.046) |
| LightGBM 2x, about 20 per hour | -0.112 (-0.145 to -0.080) | -0.115 (-0.146 to -0.084) | -0.112 (-0.145 to -0.070) |
| LightGBM graduation, about 5 per hour | -0.154 (-0.200 to -0.110) | -0.144 (-0.199 to -0.091) | -0.128 (-0.174 to -0.082) |
| LightGBM graduation, about 20 per hour | -0.123 (-0.189 to -0.041) | -0.123 (-0.188 to -0.041) | -0.117 (-0.180 to -0.049) |

**Ordinary vs. mayhem coins** (all four decision times, 5 s delay; signals in square brackets), under the headline setting and with our SOL staying in the reserves:

| Signal | Ordinary | Mayhem | Ordinary, our SOL stays | Mayhem, our SOL stays |
|---|---|---|---|---|
| Every coin | -0.112 (-0.117 to -0.106) [64,652] | -0.260 (-0.268 to -0.253) [87,344] | -0.059 (-0.064 to -0.053) [64,652] | -0.062 (-0.063 to -0.060) [87,344] |
| Rules: pass | -0.111 (-0.117 to -0.105) [4,228] | - [0] | -0.056 (-0.062 to -0.050) [4,228] | - [0] |
| Rules: pass or caution | -0.109 (-0.115 to -0.101) [39,705] | -0.311 (-0.319 to -0.304) [17,657] | -0.055 (-0.061 to -0.047) [39,705] | -0.094 (-0.097 to -0.091) [17,657] |
| LightGBM stage 1: not blocked | -0.123 (-0.130 to -0.113) [29,684] | -0.245 (-0.285 to -0.204) [218] | -0.072 (-0.080 to -0.062) [29,684] | -0.039 (-0.070 to -0.011) [218] |
| Logistic regression 2x, about 5 per hour | -0.066 (-0.116 to -0.018) [446] | -0.270 (-0.512 to -0.095) [14] | -0.042 (-0.093 to +0.006) [446] | -0.192 (-0.459 to -0.008) [14] |
| Logistic regression 2x, about 20 per hour | -0.120 (-0.160 to -0.084) [1,681] | -0.212 (-0.308 to -0.129) [151] | -0.095 (-0.136 to -0.059) [1,681] | -0.101 (-0.191 to -0.022) [151] |
| LightGBM 2x, about 5 per hour | -0.101 (-0.154 to -0.053) [462] | -1.007 (-1.007 to -1.007) [1] | -0.082 (-0.135 to -0.032) [462] | -0.046 (-0.046 to -0.046) [1] |
| LightGBM 2x, about 20 per hour | -0.101 (-0.134 to -0.067) [1,569] | -0.196 (-0.252 to -0.151) [263] | -0.078 (-0.112 to -0.043) [1,569] | -0.135 (-0.177 to -0.099) [263] |
| LightGBM graduation, about 5 per hour | -0.113 (-0.183 to -0.039) [371] | -0.275 (-0.333 to -0.219) [89] | -0.091 (-0.162 to -0.017) [371] | -0.049 (-0.083 to -0.010) [89] |
| LightGBM graduation, about 20 per hour | -0.064 (-0.151 to +0.039) [1,396] | -0.309 (-0.335 to -0.280) [436] | -0.038 (-0.127 to +0.067) [1,396] | -0.050 (-0.068 to -0.031) [436] |

Mayhem buys that would have taken every token left on the curve (share of signals): Every coin 7.4%; Rules: pass -; Rules: pass or caution 5.1%; LightGBM stage 1: not blocked 22%; Logistic regression 2x, about 5 per hour 0%; Logistic regression 2x, about 20 per hour 0%; LightGBM 2x, about 5 per hour 0%; LightGBM 2x, about 20 per hour 0%; LightGBM graduation, about 5 per hour 61%; LightGBM graduation, about 20 per hour 52%.

**Other assumptions** (all four decision times, 5 s delay, all coins; one change at a time):

| Signal | Headline | 0% of orders fail | 10% of orders fail | Our SOL stays in the reserves | Padre fee 0% |
|---|---|---|---|---|---|
| Every coin | -0.197 (-0.202 to -0.192) | -0.207 (-0.213 to -0.202) | -0.187 (-0.191 to -0.182) | -0.060 (-0.063 to -0.058) | -0.181 (-0.186 to -0.175) |
| Rules: pass | -0.111 (-0.117 to -0.105) | -0.117 (-0.123 to -0.110) | -0.104 (-0.110 to -0.099) | -0.056 (-0.062 to -0.050) | -0.093 (-0.099 to -0.087) |
| Rules: pass or caution | -0.171 (-0.178 to -0.164) | -0.180 (-0.188 to -0.173) | -0.162 (-0.169 to -0.155) | -0.067 (-0.072 to -0.061) | -0.155 (-0.162 to -0.147) |
| LightGBM stage 1: not blocked | -0.123 (-0.131 to -0.113) | -0.130 (-0.138 to -0.121) | -0.116 (-0.124 to -0.106) | -0.072 (-0.080 to -0.062) | -0.106 (-0.113 to -0.095) |
| Logistic regression 2x, about 5 per hour | -0.072 (-0.120 to -0.027) | -0.087 (-0.139 to -0.037) | -0.067 (-0.113 to -0.024) | -0.047 (-0.094 to -0.001) | -0.054 (-0.102 to -0.008) |
| Logistic regression 2x, about 20 per hour | -0.127 (-0.166 to -0.094) | -0.140 (-0.178 to -0.107) | -0.120 (-0.157 to -0.086) | -0.096 (-0.135 to -0.062) | -0.110 (-0.148 to -0.076) |
| LightGBM 2x, about 5 per hour | -0.103 (-0.157 to -0.055) | -0.112 (-0.170 to -0.055) | -0.105 (-0.154 to -0.059) | -0.082 (-0.135 to -0.032) | -0.085 (-0.140 to -0.036) |
| LightGBM 2x, about 20 per hour | -0.115 (-0.146 to -0.084) | -0.122 (-0.154 to -0.091) | -0.115 (-0.146 to -0.083) | -0.086 (-0.117 to -0.056) | -0.097 (-0.129 to -0.067) |
| LightGBM graduation, about 5 per hour | -0.144 (-0.199 to -0.091) | -0.158 (-0.213 to -0.103) | -0.144 (-0.195 to -0.095) | -0.083 (-0.140 to -0.023) | -0.129 (-0.184 to -0.075) |
| LightGBM graduation, about 20 per hour | -0.123 (-0.188 to -0.041) | -0.136 (-0.202 to -0.055) | -0.120 (-0.181 to -0.049) | -0.041 (-0.110 to +0.041) | -0.107 (-0.173 to -0.024) |

### Exit plan `hold_5_min` (time stop 5 min)

**By decision time** (5 s delay, headline setting). Mean profit or loss per signal in SOL:

| Signal | T+30 s | T+60 s | T+120 s | T+300 s | All four times | First signal per coin |
|---|---|---|---|---|---|---|
| Every coin | -0.261 (-0.270 to -0.252) | -0.238 (-0.246 to -0.230) | -0.215 (-0.223 to -0.207) | -0.203 (-0.209 to -0.196) | -0.229 (-0.237 to -0.222) | -0.261 (-0.270 to -0.252) |
| Rules: pass | -0.109 (-0.113 to -0.105) | -0.099 (-0.164 to -0.023) | -0.105 (-0.148 to -0.061) | -0.113 (-0.132 to -0.099) | -0.109 (-0.115 to -0.103) | -0.109 (-0.113 to -0.105) |
| Rules: pass or caution | -0.272 (-0.285 to -0.258) | -0.221 (-0.235 to -0.207) | -0.181 (-0.190 to -0.172) | -0.154 (-0.160 to -0.148) | -0.218 (-0.228 to -0.207) | -0.271 (-0.285 to -0.256) |
| LightGBM stage 1: not blocked | -0.137 (-0.151 to -0.118) | -0.129 (-0.141 to -0.111) | -0.117 (-0.135 to -0.088) | -0.108 (-0.122 to -0.085) | -0.123 (-0.136 to -0.106) | -0.133 (-0.145 to -0.119) |
| Logistic regression 2x, about 5 per hour | -0.130 (-0.212 to -0.056) | +0.072 (-0.105 to +0.279) | -0.043 (-0.148 to +0.063) | -0.180 (-0.285 to -0.080) | -0.070 (-0.151 to +0.011) | -0.074 (-0.172 to +0.023) |
| Logistic regression 2x, about 20 per hour | -0.223 (-0.295 to -0.151) | -0.105 (-0.183 to -0.028) | -0.123 (-0.190 to -0.054) | -0.175 (-0.221 to -0.127) | -0.157 (-0.207 to -0.108) | -0.193 (-0.235 to -0.149) |
| LightGBM 2x, about 5 per hour | -0.009 (-0.174 to +0.195) | +0.069 (-0.047 to +0.218) | -0.175 (-0.308 to -0.062) | -0.202 (-0.328 to -0.095) | -0.080 (-0.182 to +0.011) | -0.074 (-0.171 to +0.009) |
| LightGBM 2x, about 20 per hour | -0.236 (-0.316 to -0.159) | -0.214 (-0.275 to -0.164) | -0.002 (-0.217 to +0.429) | -0.150 (-0.206 to -0.094) | -0.150 (-0.219 to -0.045) | -0.166 (-0.267 to +0.020) |
| LightGBM graduation, about 5 per hour | -0.164 (-0.286 to -0.037) | -0.052 (-0.157 to +0.036) | -0.117 (-0.208 to -0.014) | -0.152 (-0.263 to -0.036) | -0.121 (-0.175 to -0.070) | -0.160 (-0.212 to -0.104) |
| LightGBM graduation, about 20 per hour | -0.124 (-0.218 to -0.028) | -0.160 (-0.241 to -0.072) | -0.192 (-0.258 to -0.115) | -0.065 (-0.264 to +0.258) | -0.135 (-0.198 to -0.055) | -0.168 (-0.255 to -0.042) |

What happened to those buys (all four decision times):

- Every coin: 151,996 signals; bought 93%; of those in profit 1.8%; exits: take-profit 0%, stop-loss 0%, time 93%
- Rules: pass: 4,228 signals; bought 95%; of those in profit 2.1%; exits: take-profit 0%, stop-loss 0%, time 95%
- Rules: pass or caution: 57,362 signals; bought 92%; of those in profit 2.6%; exits: take-profit 0%, stop-loss 0%, time 92%
- LightGBM stage 1: not blocked: 29,902 signals; bought 95%; of those in profit 5.5%; exits: take-profit 0%, stop-loss 0%, time 95%
- Logistic regression 2x, about 5 per hour: 460 signals; bought 94%; of those in profit 39%; exits: take-profit 0%, stop-loss 0%, time 94%
- Logistic regression 2x, about 20 per hour: 1,832 signals; bought 93%; of those in profit 29%; exits: take-profit 0%, stop-loss 0%, time 93%
- LightGBM 2x, about 5 per hour: 463 signals; bought 94%; of those in profit 40%; exits: take-profit 0%, stop-loss 0%, time 94%
- LightGBM 2x, about 20 per hour: 1,832 signals; bought 91%; of those in profit 28%; exits: take-profit 0%, stop-loss 0%, time 91%
- LightGBM graduation, about 5 per hour: 460 signals; bought 93%; of those in profit 33%; exits: take-profit 0%, stop-loss 0%, time 93%
- LightGBM graduation, about 20 per hour: 1,832 signals; bought 91%; of those in profit 23%; exits: take-profit 0%, stop-loss 0%, time 91%

**Delay sensitivity** (all four decision times):

| Signal | 2 s | 5 s | 10 s |
|---|---|---|---|
| Every coin | -0.230 (-0.238 to -0.224) | -0.229 (-0.237 to -0.222) | -0.227 (-0.235 to -0.220) |
| Rules: pass | -0.111 (-0.117 to -0.104) | -0.109 (-0.115 to -0.103) | -0.108 (-0.114 to -0.102) |
| Rules: pass or caution | -0.218 (-0.228 to -0.208) | -0.218 (-0.228 to -0.207) | -0.216 (-0.226 to -0.207) |
| LightGBM stage 1: not blocked | -0.127 (-0.138 to -0.115) | -0.123 (-0.136 to -0.106) | -0.123 (-0.134 to -0.107) |
| Logistic regression 2x, about 5 per hour | -0.069 (-0.149 to +0.013) | -0.070 (-0.151 to +0.011) | -0.067 (-0.143 to +0.013) |
| Logistic regression 2x, about 20 per hour | -0.159 (-0.208 to -0.111) | -0.157 (-0.207 to -0.108) | -0.144 (-0.189 to -0.101) |
| LightGBM 2x, about 5 per hour | -0.070 (-0.167 to +0.021) | -0.080 (-0.182 to +0.011) | -0.077 (-0.180 to +0.013) |
| LightGBM 2x, about 20 per hour | -0.203 (-0.245 to -0.165) | -0.150 (-0.219 to -0.045) | -0.137 (-0.207 to -0.027) |
| LightGBM graduation, about 5 per hour | -0.099 (-0.151 to -0.047) | -0.121 (-0.175 to -0.070) | -0.110 (-0.161 to -0.060) |
| LightGBM graduation, about 20 per hour | -0.136 (-0.201 to -0.059) | -0.135 (-0.198 to -0.055) | -0.127 (-0.189 to -0.051) |

**Ordinary vs. mayhem coins** (all four decision times, 5 s delay; signals in square brackets), under the headline setting and with our SOL staying in the reserves:

| Signal | Ordinary | Mayhem | Ordinary, our SOL stays | Mayhem, our SOL stays |
|---|---|---|---|---|
| Every coin | -0.111 (-0.117 to -0.103) [64,652] | -0.317 (-0.328 to -0.307) [87,344] | -0.058 (-0.065 to -0.050) [64,652] | -0.104 (-0.107 to -0.101) [87,344] |
| Rules: pass | -0.109 (-0.115 to -0.103) [4,228] | - [0] | -0.054 (-0.060 to -0.047) [4,228] | - [0] |
| Rules: pass or caution | -0.106 (-0.113 to -0.098) [39,705] | -0.468 (-0.483 to -0.452) [17,657] | -0.052 (-0.059 to -0.044) [39,705] | -0.211 (-0.221 to -0.202) [17,657] |
| LightGBM stage 1: not blocked | -0.121 (-0.134 to -0.104) [29,684] | -0.371 (-0.427 to -0.317) [218] | -0.071 (-0.084 to -0.054) [29,684] | -0.129 (-0.166 to -0.099) [218] |
| Logistic regression 2x, about 5 per hour | -0.053 (-0.133 to +0.031) [446] | -0.629 (-0.906 to -0.443) [14] | -0.029 (-0.111 to +0.055) [446] | -0.491 (-0.784 to -0.288) [14] |
| Logistic regression 2x, about 20 per hour | -0.118 (-0.172 to -0.066) [1,681] | -0.580 (-0.672 to -0.508) [151] | -0.094 (-0.148 to -0.041) [1,681] | -0.388 (-0.470 to -0.325) [151] |
| LightGBM 2x, about 5 per hour | -0.078 (-0.177 to +0.013) [462] | -1.008 (-1.008 to -1.008) [1] | -0.057 (-0.158 to +0.035) [462] | -0.046 (-0.046 to -0.046) [1] |
| LightGBM 2x, about 20 per hour | -0.076 (-0.148 to +0.041) [1,569] | -0.596 (-0.656 to -0.536) [263] | -0.053 (-0.126 to +0.066) [1,569] | -0.457 (-0.506 to -0.407) [263] |
| LightGBM graduation, about 5 per hour | -0.053 (-0.113 to +0.017) [371] | -0.406 (-0.487 to -0.329) [89] | -0.030 (-0.091 to +0.042) [371] | -0.154 (-0.194 to -0.113) [89] |
| LightGBM graduation, about 20 per hour | -0.042 (-0.126 to +0.064) [1,396] | -0.433 (-0.466 to -0.403) [436] | -0.016 (-0.101 to +0.092) [1,396] | -0.127 (-0.148 to -0.108) [436] |

Mayhem buys that would have taken every token left on the curve (share of signals): Every coin 7.4%; Rules: pass -; Rules: pass or caution 5.1%; LightGBM stage 1: not blocked 22%; Logistic regression 2x, about 5 per hour 0%; Logistic regression 2x, about 20 per hour 0%; LightGBM 2x, about 5 per hour 0%; LightGBM 2x, about 20 per hour 0%; LightGBM graduation, about 5 per hour 61%; LightGBM graduation, about 20 per hour 52%.

**Other assumptions** (all four decision times, 5 s delay, all coins; one change at a time):

| Signal | Headline | 0% of orders fail | 10% of orders fail | Our SOL stays in the reserves | Padre fee 0% |
|---|---|---|---|---|---|
| Every coin | -0.229 (-0.237 to -0.222) | -0.241 (-0.249 to -0.234) | -0.217 (-0.224 to -0.210) | -0.084 (-0.088 to -0.080) | -0.213 (-0.221 to -0.206) |
| Rules: pass | -0.109 (-0.115 to -0.103) | -0.115 (-0.121 to -0.108) | -0.102 (-0.108 to -0.096) | -0.054 (-0.060 to -0.047) | -0.091 (-0.097 to -0.084) |
| Rules: pass or caution | -0.218 (-0.228 to -0.207) | -0.229 (-0.241 to -0.218) | -0.205 (-0.216 to -0.195) | -0.101 (-0.109 to -0.093) | -0.201 (-0.212 to -0.190) |
| LightGBM stage 1: not blocked | -0.123 (-0.136 to -0.106) | -0.130 (-0.143 to -0.113) | -0.115 (-0.128 to -0.098) | -0.072 (-0.084 to -0.055) | -0.105 (-0.118 to -0.088) |
| Logistic regression 2x, about 5 per hour | -0.070 (-0.151 to +0.011) | -0.088 (-0.166 to -0.008) | -0.074 (-0.148 to +0.006) | -0.043 (-0.122 to +0.039) | -0.052 (-0.133 to +0.030) |
| Logistic regression 2x, about 20 per hour | -0.157 (-0.207 to -0.108) | -0.172 (-0.222 to -0.123) | -0.148 (-0.196 to -0.104) | -0.118 (-0.169 to -0.069) | -0.139 (-0.190 to -0.090) |
| LightGBM 2x, about 5 per hour | -0.080 (-0.182 to +0.011) | -0.092 (-0.200 to +0.003) | -0.091 (-0.186 to -0.003) | -0.057 (-0.158 to +0.035) | -0.061 (-0.164 to +0.030) |
| LightGBM 2x, about 20 per hour | -0.150 (-0.219 to -0.045) | -0.167 (-0.238 to -0.062) | -0.147 (-0.214 to -0.040) | -0.111 (-0.179 to -0.005) | -0.133 (-0.203 to -0.026) |
| LightGBM graduation, about 5 per hour | -0.121 (-0.175 to -0.070) | -0.137 (-0.187 to -0.086) | -0.131 (-0.184 to -0.081) | -0.054 (-0.104 to +0.001) | -0.105 (-0.160 to -0.054) |
| LightGBM graduation, about 20 per hour | -0.135 (-0.198 to -0.055) | -0.152 (-0.216 to -0.075) | -0.133 (-0.195 to -0.057) | -0.042 (-0.107 to +0.037) | -0.120 (-0.183 to -0.039) |

## Training days 2026-11-12 to 2026-11-16

Only the signals that were never fitted: the models learned from these days. For stability across days.

165,664 coins (75,261 ordinary, 90,403 mayhem), 662,656 coin and decision-time rows, 5,963,904 simulated trades. Signals (rows bought): Every coin 662,656; Rules: pass 23,240; Rules: pass or caution 272,135.

### Exit plan `double_or_half` (take-profit +100%, stop-loss -50%, time stop 30 min)

**By decision time** (5 s delay, headline setting). Mean profit or loss per signal in SOL:

| Signal | T+30 s | T+60 s | T+120 s | T+300 s | All four times | First signal per coin |
|---|---|---|---|---|---|---|
| Every coin | -0.199 (-0.202 to -0.196) | -0.204 (-0.207 to -0.201) | -0.202 (-0.205 to -0.199) | -0.200 (-0.203 to -0.197) | -0.201 (-0.204 to -0.199) | -0.199 (-0.202 to -0.196) |
| Rules: pass | -0.105 (-0.109 to -0.100) | -0.128 (-0.161 to -0.096) | -0.131 (-0.154 to -0.105) | -0.127 (-0.149 to -0.104) | -0.106 (-0.111 to -0.101) | -0.105 (-0.109 to -0.100) |
| Rules: pass or caution | -0.205 (-0.209 to -0.201) | -0.194 (-0.198 to -0.190) | -0.173 (-0.177 to -0.168) | -0.156 (-0.161 to -0.152) | -0.186 (-0.190 to -0.182) | -0.205 (-0.209 to -0.201) |

What happened to those buys (all four decision times):

- Every coin: 662,656 signals; bought 93%; of those in profit 3.4%; exits: take-profit 3.3%, stop-loss 13%, time 76%
- Rules: pass: 23,240 signals; bought 95%; of those in profit 2.3%; exits: take-profit 1.2%, stop-loss 0.2%, time 94%
- Rules: pass or caution: 272,135 signals; bought 92%; of those in profit 4.6%; exits: take-profit 4.4%, stop-loss 14%, time 74%

**Delay sensitivity** (all four decision times):

| Signal | 2 s | 5 s | 10 s |
|---|---|---|---|
| Every coin | -0.198 (-0.201 to -0.196) | -0.201 (-0.204 to -0.199) | -0.205 (-0.208 to -0.202) |
| Rules: pass | -0.106 (-0.111 to -0.101) | -0.106 (-0.111 to -0.101) | -0.107 (-0.111 to -0.102) |
| Rules: pass or caution | -0.182 (-0.185 to -0.178) | -0.186 (-0.190 to -0.182) | -0.191 (-0.195 to -0.186) |

**Per day** (all four decision times, 5 s delay; signals in square brackets):

| Signal | 2026-11-12 | 2026-11-13 | 2026-11-14 | 2026-11-15 | 2026-11-16 |
|---|---|---|---|---|---|
| Every coin | -0.195 (-0.201 to -0.189) [118,832] | -0.202 (-0.207 to -0.197) [143,592] | -0.209 (-0.214 to -0.204) [137,536] | -0.202 (-0.209 to -0.197) [129,300] | -0.198 (-0.204 to -0.192) [133,396] |
| Rules: pass | -0.106 (-0.111 to -0.101) [5,431] | -0.091 (-0.109 to -0.067) [3,999] | -0.106 (-0.114 to -0.093) [4,623] | -0.114 (-0.118 to -0.110) [4,376] | -0.114 (-0.118 to -0.109) [4,811] |
| Rules: pass or caution | -0.176 (-0.184 to -0.168) [54,290] | -0.191 (-0.200 to -0.184) [52,511] | -0.199 (-0.206 to -0.192) [54,671] | -0.186 (-0.196 to -0.178) [52,719] | -0.178 (-0.186 to -0.172) [57,944] |

**Ordinary vs. mayhem coins** (all four decision times, 5 s delay; signals in square brackets), under the headline setting and with our SOL staying in the reserves:

| Signal | Ordinary | Mayhem | Ordinary, our SOL stays | Mayhem, our SOL stays |
|---|---|---|---|---|
| Every coin | -0.118 (-0.120 to -0.116) [301,044] | -0.271 (-0.274 to -0.268) [361,612] | -0.065 (-0.067 to -0.063) [301,044] | -0.078 (-0.079 to -0.077) [361,612] |
| Rules: pass | -0.106 (-0.111 to -0.101) [23,240] | - [0] | -0.051 (-0.056 to -0.046) [23,240] | - [0] |
| Rules: pass or caution | -0.119 (-0.120 to -0.117) [193,980] | -0.354 (-0.358 to -0.349) [78,155] | -0.065 (-0.067 to -0.063) [193,980] | -0.136 (-0.138 to -0.133) [78,155] |

Mayhem buys that would have taken every token left on the curve (share of signals): Every coin 6.9%; Rules: pass -; Rules: pass or caution 4.4%.

**Other assumptions** (all four decision times, 5 s delay, all coins; one change at a time):

| Signal | Headline | 0% of orders fail | 10% of orders fail | Our SOL stays in the reserves | Padre fee 0% |
|---|---|---|---|---|---|
| Every coin | -0.201 (-0.204 to -0.199) | -0.212 (-0.214 to -0.209) | -0.191 (-0.194 to -0.189) | -0.072 (-0.073 to -0.071) | -0.185 (-0.188 to -0.182) |
| Rules: pass | -0.106 (-0.111 to -0.101) | -0.112 (-0.117 to -0.107) | -0.101 (-0.105 to -0.095) | -0.051 (-0.056 to -0.046) | -0.088 (-0.093 to -0.083) |
| Rules: pass or caution | -0.186 (-0.190 to -0.182) | -0.195 (-0.200 to -0.191) | -0.177 (-0.180 to -0.173) | -0.085 (-0.087 to -0.083) | -0.169 (-0.173 to -0.165) |

### Exit plan `quick_50_25` (take-profit +50%, stop-loss -25%, time stop 15 min)

**By decision time** (5 s delay, headline setting). Mean profit or loss per signal in SOL:

| Signal | T+30 s | T+60 s | T+120 s | T+300 s | All four times | First signal per coin |
|---|---|---|---|---|---|---|
| Every coin | -0.180 (-0.182 to -0.177) | -0.193 (-0.195 to -0.190) | -0.194 (-0.197 to -0.192) | -0.197 (-0.200 to -0.194) | -0.191 (-0.193 to -0.188) | -0.180 (-0.182 to -0.177) |
| Rules: pass | -0.103 (-0.107 to -0.098) | -0.120 (-0.144 to -0.096) | -0.097 (-0.120 to -0.072) | -0.100 (-0.116 to -0.083) | -0.103 (-0.107 to -0.098) | -0.103 (-0.107 to -0.098) |
| Rules: pass or caution | -0.181 (-0.184 to -0.177) | -0.177 (-0.181 to -0.173) | -0.162 (-0.167 to -0.158) | -0.151 (-0.155 to -0.147) | -0.170 (-0.174 to -0.167) | -0.181 (-0.185 to -0.178) |

What happened to those buys (all four decision times):

- Every coin: 662,656 signals; bought 93%; of those in profit 4.2%; exits: take-profit 3.9%, stop-loss 16%, time 73%
- Rules: pass: 23,240 signals; bought 95%; of those in profit 3.1%; exits: take-profit 2.2%, stop-loss 2.9%, time 90%
- Rules: pass or caution: 272,135 signals; bought 92%; of those in profit 5.8%; exits: take-profit 5.1%, stop-loss 19%, time 68%

**Delay sensitivity** (all four decision times):

| Signal | 2 s | 5 s | 10 s |
|---|---|---|---|
| Every coin | -0.187 (-0.189 to -0.184) | -0.191 (-0.193 to -0.188) | -0.196 (-0.198 to -0.193) |
| Rules: pass | -0.103 (-0.106 to -0.099) | -0.103 (-0.107 to -0.098) | -0.103 (-0.107 to -0.098) |
| Rules: pass or caution | -0.165 (-0.168 to -0.161) | -0.170 (-0.174 to -0.167) | -0.177 (-0.181 to -0.173) |

**Per day** (all four decision times, 5 s delay; signals in square brackets):

| Signal | 2026-11-12 | 2026-11-13 | 2026-11-14 | 2026-11-15 | 2026-11-16 |
|---|---|---|---|---|---|
| Every coin | -0.184 (-0.189 to -0.178) [118,832] | -0.191 (-0.196 to -0.187) [143,592] | -0.198 (-0.202 to -0.194) [137,536] | -0.193 (-0.199 to -0.188) [129,300] | -0.188 (-0.194 to -0.183) [133,396] |
| Rules: pass | -0.102 (-0.106 to -0.098) [5,431] | -0.094 (-0.108 to -0.073) [3,999] | -0.100 (-0.108 to -0.087) [4,623] | -0.109 (-0.112 to -0.106) [4,376] | -0.109 (-0.112 to -0.106) [4,811] |
| Rules: pass or caution | -0.160 (-0.167 to -0.153) [54,290] | -0.175 (-0.183 to -0.168) [52,511] | -0.182 (-0.189 to -0.176) [54,671] | -0.171 (-0.180 to -0.164) [52,719] | -0.164 (-0.171 to -0.158) [57,944] |

**Ordinary vs. mayhem coins** (all four decision times, 5 s delay; signals in square brackets), under the headline setting and with our SOL staying in the reserves:

| Signal | Ordinary | Mayhem | Ordinary, our SOL stays | Mayhem, our SOL stays |
|---|---|---|---|---|
| Every coin | -0.112 (-0.113 to -0.110) [301,044] | -0.257 (-0.260 to -0.254) [361,612] | -0.059 (-0.060 to -0.057) [301,044] | -0.064 (-0.065 to -0.063) [361,612] |
| Rules: pass | -0.103 (-0.107 to -0.098) [23,240] | - [0] | -0.048 (-0.052 to -0.043) [23,240] | - [0] |
| Rules: pass or caution | -0.112 (-0.113 to -0.110) [193,980] | -0.315 (-0.319 to -0.311) [78,155] | -0.058 (-0.059 to -0.056) [193,980] | -0.098 (-0.100 to -0.096) [78,155] |

Mayhem buys that would have taken every token left on the curve (share of signals): Every coin 6.9%; Rules: pass -; Rules: pass or caution 4.4%.

**Other assumptions** (all four decision times, 5 s delay, all coins; one change at a time):

| Signal | Headline | 0% of orders fail | 10% of orders fail | Our SOL stays in the reserves | Padre fee 0% |
|---|---|---|---|---|---|
| Every coin | -0.191 (-0.193 to -0.188) | -0.201 (-0.203 to -0.198) | -0.181 (-0.184 to -0.179) | -0.062 (-0.062 to -0.061) | -0.174 (-0.177 to -0.172) |
| Rules: pass | -0.103 (-0.107 to -0.098) | -0.109 (-0.112 to -0.104) | -0.098 (-0.101 to -0.093) | -0.048 (-0.052 to -0.043) | -0.085 (-0.089 to -0.080) |
| Rules: pass or caution | -0.170 (-0.174 to -0.167) | -0.179 (-0.182 to -0.175) | -0.162 (-0.165 to -0.158) | -0.069 (-0.071 to -0.068) | -0.153 (-0.157 to -0.150) |

### Exit plan `hold_5_min` (time stop 5 min)

**By decision time** (5 s delay, headline setting). Mean profit or loss per signal in SOL:

| Signal | T+30 s | T+60 s | T+120 s | T+300 s | All four times | First signal per coin |
|---|---|---|---|---|---|---|
| Every coin | -0.253 (-0.258 to -0.249) | -0.235 (-0.239 to -0.231) | -0.214 (-0.217 to -0.211) | -0.201 (-0.204 to -0.198) | -0.226 (-0.229 to -0.222) | -0.253 (-0.258 to -0.249) |
| Rules: pass | -0.103 (-0.106 to -0.100) | -0.124 (-0.151 to -0.098) | -0.104 (-0.124 to -0.081) | -0.104 (-0.120 to -0.085) | -0.104 (-0.107 to -0.100) | -0.103 (-0.106 to -0.100) |
| Rules: pass or caution | -0.268 (-0.275 to -0.262) | -0.227 (-0.234 to -0.221) | -0.185 (-0.190 to -0.179) | -0.157 (-0.161 to -0.152) | -0.219 (-0.225 to -0.213) | -0.270 (-0.276 to -0.264) |

What happened to those buys (all four decision times):

- Every coin: 662,656 signals; bought 93%; of those in profit 1.8%; exits: take-profit 0%, stop-loss 0%, time 93%
- Rules: pass: 23,240 signals; bought 95%; of those in profit 2.5%; exits: take-profit 0%, stop-loss 0%, time 95%
- Rules: pass or caution: 272,135 signals; bought 92%; of those in profit 2.5%; exits: take-profit 0%, stop-loss 0%, time 92%

**Delay sensitivity** (all four decision times):

| Signal | 2 s | 5 s | 10 s |
|---|---|---|---|
| Every coin | -0.227 (-0.230 to -0.223) | -0.226 (-0.229 to -0.222) | -0.224 (-0.227 to -0.220) |
| Rules: pass | -0.104 (-0.107 to -0.100) | -0.104 (-0.107 to -0.100) | -0.103 (-0.107 to -0.100) |
| Rules: pass or caution | -0.219 (-0.225 to -0.213) | -0.219 (-0.225 to -0.213) | -0.216 (-0.222 to -0.210) |

**Per day** (all four decision times, 5 s delay; signals in square brackets):

| Signal | 2026-11-12 | 2026-11-13 | 2026-11-14 | 2026-11-15 | 2026-11-16 |
|---|---|---|---|---|---|
| Every coin | -0.217 (-0.225 to -0.209) [118,832] | -0.228 (-0.236 to -0.222) [143,592] | -0.235 (-0.241 to -0.229) [137,536] | -0.230 (-0.237 to -0.224) [129,300] | -0.218 (-0.226 to -0.211) [133,396] |
| Rules: pass | -0.101 (-0.105 to -0.097) [5,431] | -0.100 (-0.110 to -0.086) [3,999] | -0.100 (-0.108 to -0.087) [4,623] | -0.110 (-0.113 to -0.107) [4,376] | -0.108 (-0.112 to -0.105) [4,811] |
| Rules: pass or caution | -0.203 (-0.215 to -0.191) [54,290] | -0.228 (-0.242 to -0.216) [52,511] | -0.238 (-0.249 to -0.229) [54,671] | -0.224 (-0.236 to -0.213) [52,719] | -0.202 (-0.212 to -0.194) [57,944] |

**Ordinary vs. mayhem coins** (all four decision times, 5 s delay; signals in square brackets), under the headline setting and with our SOL staying in the reserves:

| Signal | Ordinary | Mayhem | Ordinary, our SOL stays | Mayhem, our SOL stays |
|---|---|---|---|---|
| Every coin | -0.114 (-0.116 to -0.112) [301,044] | -0.319 (-0.324 to -0.314) [361,612] | -0.061 (-0.063 to -0.059) [301,044] | -0.112 (-0.114 to -0.109) [361,612] |
| Rules: pass | -0.104 (-0.107 to -0.100) [23,240] | - [0] | -0.049 (-0.052 to -0.045) [23,240] | - [0] |
| Rules: pass or caution | -0.112 (-0.114 to -0.111) [193,980] | -0.484 (-0.490 to -0.477) [78,155] | -0.058 (-0.060 to -0.057) [193,980] | -0.225 (-0.230 to -0.220) [78,155] |

Mayhem buys that would have taken every token left on the curve (share of signals): Every coin 6.9%; Rules: pass -; Rules: pass or caution 4.4%.

**Other assumptions** (all four decision times, 5 s delay, all coins; one change at a time):

| Signal | Headline | 0% of orders fail | 10% of orders fail | Our SOL stays in the reserves | Padre fee 0% |
|---|---|---|---|---|---|
| Every coin | -0.226 (-0.229 to -0.222) | -0.238 (-0.241 to -0.234) | -0.214 (-0.217 to -0.211) | -0.089 (-0.090 to -0.087) | -0.210 (-0.213 to -0.206) |
| Rules: pass | -0.104 (-0.107 to -0.100) | -0.109 (-0.112 to -0.106) | -0.098 (-0.101 to -0.094) | -0.049 (-0.052 to -0.045) | -0.086 (-0.089 to -0.082) |
| Rules: pass or caution | -0.219 (-0.225 to -0.213) | -0.230 (-0.237 to -0.224) | -0.208 (-0.213 to -0.202) | -0.106 (-0.109 to -0.103) | -0.203 (-0.209 to -0.197) |

