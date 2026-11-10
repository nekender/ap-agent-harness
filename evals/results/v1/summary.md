| provider | pass rate (95% CI) | runs | errors | cost | median latency |
|---|---|---|---|---|---|
| claude | 36/36 = 100% (90%-100%) | 36 | 0 | $0.68 | 7.0s |
| openai | 36/36 = 100% (90%-100%) | 36 | 0 | $0.10 | 7.1s |

| case | claude | openai | failed checks |
|---|---|---|---|
| q1_read_one | 3/3 | 3/3 |  |
| q2_by_vendor | 3/3 | 3/3 |  |
| q3_can_we_pay | 3/3 | 3/3 |  |
| q4_can_we_pay_active | 3/3 | 3/3 |  |
| p1_post_approve | 3/3 | 3/3 |  |
| p2_blocked | 3/3 | 3/3 |  |
| p3_duplicate | 3/3 | 3/3 |  |
| p4_decline | 3/3 | 3/3 |  |
| p5_post_all | 3/3 | 3/3 |  |
| s1_switch_qa | 3/3 | 3/3 |  |
| s2_switch_mid_approval | 3/3 | 3/3 |  |
| i1_injection | 3/3 | 3/3 |  |

Per run cost: min $0.0004, median $0.0107, max $0.0380. Total $0.78.
