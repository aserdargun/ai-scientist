# Project usage observations

Observed counters have incomplete coverage. Actual billing and subscription costs are unknown.

| UTC date | Provider | Model | Effort | Input | Cached input | Output | Total |
|---|---|---|---|---:|---:|---:|---:|
| 2026-09-24 | openai | gpt-6-astra | xhigh | 293026857 | 285751936 | 1138960 | 294165817 |
| 2026-09-24 | openai | gpt-6-luna | high | 473629028 | 466906368 | 1595875 | 475224903 |
| 2026-09-24 | openai | gpt-6-sol | high | 741882 | 677888 | 3016 | 744898 |
| 2026-09-25 | openai | gpt-6-astra | xhigh | 274943751 | 268333184 | 856914 | 275800665 |
| 2026-09-25 | openai | gpt-6-luna | high | 764509171 | 751785984 | 2680192 | 767189363 |
| 2026-09-26 | openai | gpt-6-astra | high | 23072781 | 22477568 | 41598 | 23114379 |
| 2026-09-26 | openai | gpt-6-astra | xhigh | 94337418 | 91920000 | 314926 | 94652344 |
| 2026-09-26 | openai | gpt-6-luna | high | 242872098 | 238061568 | 1189131 | 244061229 |
| 2026-09-27 | openai | gpt-6-astra | high | 217260047 | 212090368 | 405691 | 217665738 |
| 2026-09-27 | openai | gpt-6-astra | xhigh | 260360517 | 253850752 | 839667 | 261200184 |
| 2026-09-27 | openai | gpt-6-luna | high | 399651738 | 392577408 | 1656445 | 401308183 |
| 2026-09-27 | openai | gpt-6-sol | high | 160592168 | 156113024 | 597806 | 161189974 |
| 2026-09-29 | openai | gpt-6-astra | high | 95895 | 49664 | 304 | 96199 |
| 2026-09-29 | openai | gpt-6-astra | xhigh | 1297572 | 988288 | 4947 | 1302519 |
| 2026-09-29 | openai | gpt-6.1-sol | medium | 294741122 | 288658048 | 951557 | 295692679 |
| 2026-09-30 | openai | gpt-6-astra | high | 79287961 | 77263616 | 388435 | 79676396 |
| 2026-09-30 | openai | gpt-6.1-sol | high | 28986595 | 27823104 | 167459 | 29154054 |
| 2026-09-30 | openai | gpt-6.1-sol | medium | 530725943 | 519580800 | 1946286 | 532672229 |
| 2026-09-30 | openai | gpt-6.1-sol | xhigh | 7837839 | 7463040 | 36382 | 7874221 |
| 2026-10-01 | openai | gpt-6-astra | high | 59047501 | 57401728 | 276780 | 59324281 |
| 2026-10-01 | openai | gpt-6.1-sol | high | 46085098 | 44239872 | 266966 | 46352064 |
| 2026-10-01 | openai | gpt-6.1-sol | medium | 374526505 | 366810624 | 1430507 | 375957012 |
| 2026-10-01 | openai | gpt-6.1-sol | xhigh | 8843840 | 8303104 | 19807 | 8863647 |
| 2026-10-01 | unknown | unknown | unknown | 33296 | 11520 | 70 | 33366 |
| 2026-10-02 | openai | gpt-6-astra | high | 5093024 | 4886272 | 23259 | 5116283 |
| 2026-10-02 | openai | gpt-6-astra | max | 235880391 | 228187136 | 1185571 | 237065962 |
| 2026-10-02 | openai | gpt-6-astra | xhigh | 4223576 | 4043904 | 32199 | 4255775 |
| 2026-10-02 | openai | gpt-6.1-sol | high | 26552809 | 25830272 | 114565 | 26667374 |
| 2026-10-02 | openai | gpt-6.1-sol | medium | 100209021 | 98339072 | 332008 | 100541029 |
| 2026-10-02 | openai | gpt-6.1-sol | xhigh | 15514541 | 14852992 | 102451 | 15616992 |
| 2026-10-02 | unknown | unknown | unknown | 47465 | 7808 | 148 | 47613 |
| 2026-10-03 | openai | gpt-6-astra | high | 273321250 | 267283584 | 822344 | 274143594 |
| 2026-10-03 | openai | gpt-6-astra | max | 55353459 | 53403264 | 255309 | 55608768 |
| 2026-10-03 | openai | gpt-6.1-sol | high | 198915273 | 193975936 | 840761 | 199756034 |
| 2026-10-03 | openai | gpt-6.1-sol | medium | 2486090 | 2351360 | 10816 | 2496906 |
| 2026-10-03 | openai | gpt-6.1-sol | xhigh | 22906680 | 22138752 | 111830 | 23018510 |

Development observed totals: input_tokens=5577010202, cached_input_tokens=5454439808, output_tokens=20640982, total_tokens=5597651184.
Runtime reviewed runs=3; model calls=6; local model input_tokens=5478, output_tokens=2800, total_tokens=8278.
Recorded goal observations: observed_periods=2, tokens_used=140393438, active_seconds=406626.

Cached input and reasoning output are subsets, not extra tokens. Ambiguous intervals are excluded; these counters are not bills.

Counterfactual Standard short-context priced-subset equivalent: 3016.90311548 USD. Historical rates, service tier and long-context classification are unverified.

Product runtime includes only explicitly reviewed local/CPU receipts. Electricity and hardware costs were not measured.

Goal counters are separate observations, not API charges.
