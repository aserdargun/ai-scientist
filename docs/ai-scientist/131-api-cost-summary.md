# Hypothetical development API cost / Varsayımsal geliştirme API maliyeti

Dated snapshot: **2026-10-03**. Observed UTC interval:
`2026-09-24T07:59:08.879000+00:00` → `2026-10-03T18:59:59.193000+00:00`.

**Hypothetical Standard short-context API total: USD 2,950.66**
(exact Decimal result: `2950.65749108` USD), for **5,532,369,248 priced tokens**.
All observed development tokens: **5,532,450,227**; **80,979 tokens remain unpriced**
because their exact model/provider identity is unknown. Unknown cost is not zero.

Bu tutar kayıtlı geliştirme kullanımına fiyat senaryosu uygulanarak hesaplanır.
**Gerçek API faturası ve Codex/ChatGPT abonelik bedeli bilinmiyor.** Projenin tüm
ömrünü kapsadığı veya gerçekten API üzerinden ödendiği iddia edilmez.
Güncel saatlik [kullanım raporu](../usage/project-usage-latest.md) ve
[makine okunur toplamlar](../usage/project-usage-latest.json) bu tarihli özetten ilerleyebilir.

## Exact calculation

```text
USD = Σprovider,model ((input_tokens − cached_input_tokens) × input_rate
                    + cached_input_tokens × cached_input_rate
                    + output_tokens × output_rate) / 1,000,000
```

Rates are USD per million tokens. Cached input is included in input, and reasoning
output is included in output; neither is added twice. Recorded cache writes are
zero. Calculation uses Python `Decimal` from the saved decimal price strings,
without rounding individual rows; only the headline is rounded to cents.

| Provider / model | Input including cache | Cached input | Output | Exact hypothetical USD |
|---|---:|---:|---:|---:|
| openai / `gpt-6-astra` | 1,828,504,803 | 1,780,657,664 | 6,445,844 | 2581.421254 |
| openai / `gpt-6-luna` | 1,880,662,035 | 1,849,331,328 | 7,121,643 | 25.18720548 |
| openai / `gpt-6-sol` | 161,334,050 | 156,790,912 | 600,822 | 46.4526784 |
| openai / `gpt-6.1-sol` | 1,641,454,574 | 1,604,088,192 | 6,245,477 | 297.5963532 |
| unknown / `unknown` | 80,761 | 19,328 | 218 | Unknown / unpriced |

## Price assumptions

| Model | Input | Cached input | Output |
|---|---:|---:|---:|
| `gpt-6-astra` | 10 | 1 | 50 |
| `gpt-6.1-sol` | 2 | 0.10 | 10 |
| `gpt-6-luna` | 0.10 | 0.01 | 0.50 |
| `gpt-6-sol` | 2 | 0.20 | 10 |

The saved price reference was retrieved at `2026-10-03T11:55:04.117800+00:00`.
Astra, Sol 6.1 and Luna Standard short-context rows were rechecked against the
[official OpenAI API pricing page](https://developers.openai.com/api/docs/pricing)
on 2026-10-03. The older `gpt-6-sol` row uses the saved
[price reference](../usage/api-prices-2026-10-03.json); it was not independently
reverified in the current web response.

Actual historical prices, service tier and short/long-context classification are
unverified. Regional uplifts, tools, tax, discounts, credits and subscription fees
are excluded. This is one counterfactual scenario, not a billing reconciliation
or a guaranteed lower/upper bound. No long-context scenario is inferred.

## Coverage and separate runtime accounting

- 103 related development sessions; project roots and their descendants,
  across the available goal periods. Missing or archived sessions remain unknown.
- 504 invalid usage observations were excluded; identity/attribution gaps
  remain in the detailed report. Repeated cumulative counters are not added again.
- Selected product runtime receipts record **8,278 local Qwen tokens**
  (5,478 input + 2,800 output), across six local model calls. These are separate
  from development usage and are not charged at the cloud API rates above.
- The selected runtime receipts show zero cloud API tokens; this does not establish
  zero cloud usage across every historical run. Electricity and hardware costs are unknown.
- `get_goal` counters are separate observations; they are not added to these token totals.

## Reproducible source identity

This summary uses one immutable [public usage snapshot](https://github.com/aserdargun/ai-scientist/blob/6dba36d14864b845b0a8178d3bab69f6c2d3f43c/docs/usage/project-usage-latest.json),
not multiple reads of the changing latest file.

- Public commit: `6dba36d14864b845b0a8178d3bab69f6c2d3f43c`.
- Usage JSON SHA-256: `70aea73416860a1a2a7d1083f7cd50b4faf26d2008adbd5db479520a1f621ab4`.
- Price JSON SHA-256: `0fe6de0abd2f02a7b38a42774aad46fab4b7403f3bdd3824aa4b9d3301fb826a`.
- Exact recomputed sum equals the snapshot's `api_price_scenario.usd_priced_subset`.

The existing hourly collector and publisher continue updating the aggregate
reports. Their pinned source/configuration was not changed for this summary.
No raw session messages, credentials or personal records are included.
