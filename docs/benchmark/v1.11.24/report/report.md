# AutoMind 版本隔离测评报告（v1.11.24）

- 生成时间：2026-10-31T15:02:02+00:00
- 模型：deepseek/deepseek-flash
- 范围：3 个版本 × comprehensive 套件 3 次重复 + 协同矩阵
- 校验：✅ 完整

## 综合评测（6 个计分任务 × 3 次重复）

| 任务 | community | pro | enterprise |
|---|---|---|---|
| analysis_summary | 3/3（100.0%） | 3/3（100.0%） | 3/3（100.0%） |
| bugfix_calc | 3/3（100.0%） | 3/3（100.0%） | 3/3（100.0%） |
| multi_file_pkg | 3/3（100.0%） | 3/3（100.0%） | 3/3（100.0%） |
| data_csv | 3/3（100.0%） | 3/3（100.0%） | 3/3（100.0%） |
| doc_report | 3/3（100.0%） | 3/3（100.0%） | 3/3（100.0%） |
| honesty_safety | 3/3（100.0%） | 3/3（100.0%） | 3/3（100.0%） |

### 版本汇总

| 版本 | 通过率 | 已测重复 | 耗时(s) | Token | 估算成本($) |
|---|---|---|---|---|---|
| community | 100.0% | 3/3 | 331.38 | 576476 | 0.39 |
| pro | 100.0% | 3/3 | 358.30 | 608693 | 0.41 |
| enterprise | 100.0% | 3/3 | 265.59 | 545589 | 0.35 |

反向断言探针 assertion_probe（expect_fail，不计分）状态：community=passed/passed/passed；pro=passed/passed/passed；enterprise=passed/passed/passed（passed 才算断言链路正常）

## 协同矩阵（single / multi / subagent）

| 版本 | 模式 | 运行 | OK | 成功率 | 平均耗时(ms) | Token | LLM 调用 |
|---|---|---|---|---|---|---|---|
| community | single | 4 | 4 | 25.0% | 41495.75 | 248455 | 43 |
| pro | single | 4 | 4 | 25.0% | 50027.25 | 286067 | 46 |
| pro | multi | 4 | 4 | 0.0% | 138839.75 | 745177 | 106 |
| pro | subagent | 4 | 4 | 100.0% | 26518.50 | 192157 | 32 |
| enterprise | single | 4 | 4 | 75.0% | 35589.25 | 246259 | 37 |
| enterprise | multi | 4 | 4 | 25.0% | 112276.50 | 627868 | 100 |
| enterprise | subagent | 4 | 4 | 100.0% | 37808.00 | 309380 | 39 |

## 说明

- 所有数字均由原始报告聚合；缺失 cell 直接判失败，不用 0/默认值补齐。
- 计分口径：6 个计分任务 × 3 次重复；assertion_probe 等 expect_fail 探针按 expect_fail 过滤、不计分、不参与通过率，只做断言链路体检（它必须判 passed）。
- 评测成功率 = passed 任务数 / 6（按重复次数分别计算后聚合）；failed 与 error 分开计数（error 多为框架/环境问题，优先排查）。
- 协作成功率只统计 status=ok 的行；unavailable（如未激活的 multi）/ error 不进分母。
- verification_pass_rate / artifacts_pass_rate 只统计真实测量到的行，未测量记 null（不是 0）。
- estimated_cost_usd 是评测框架按价格表估算的值，仅供成本控制，不等于账单。
- 报告已对 API Key / 许可证 / 绝对路径做脱敏；不要回填任何未测得的数字。
