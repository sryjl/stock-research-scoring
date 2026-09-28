# Dragon Score V1 数据库与项目接入规范

版本：integration-contract-v1  
盘点时间：2026-09-18  
项目：Dragon Score V1 / 短线擒龙

## 1. 接入边界

本项目数据分为四类：

1. 可供产品展示的当前日线快照；
2. 可供研究使用的历史日线和回测数据；
3. 只能用于前瞻归档的分钟数据；
4. 必须隔离的未来结果数据。

其他项目接入时，不能把研究结果或未来结果当成实时信号。

## 2. 数据文件

主数据库：

data/dragon_score_v1_app/dragon_score_v1.sqlite

Outcome 数据库：

data/dragon_score_v1_outcome/outcome-v1.sqlite

日线 RAW 冷存储：

data/dragon_score_v1_outcome/raw/*.csv.gz

分钟 RAW 冷存储：

data/dragon_score_v1_intraday/raw/*.json.gz

建议外部项目使用 SQLite 只读连接，不直接修改以上数据库。

## 3. 当前数据状态

### 3.1 日线数据

| 项目 | 当前值 |
|---|---|
| 最新完整交易日 | 2026-09-17 |
| 覆盖区间 | 2021-01-04 ～ 2026-09-17 |
| Provider | BaoStock RAW adjustflag=3 |
| 数据同步状态 | SYNCED |
| 量能补齐状态 | SYNCED |
| 完成股票数 | 5,370 |
| 价格策略 | price-series-policy-v1 |
| PIT 策略 | pit-policy-v1 |

### 3.2 分钟数据

| 项目 | 当前值 |
|---|---|
| 粒度 | 5 分钟 |
| Provider | EastmoneyPublic5mBestEffort |
| 分钟线数量 | 131,920 |
| 股票数 | 87 |
| 股票日快照 | 2,783 |
| PIT 状态 | PROSPECTIVE_ARCHIVE_ONLY |

分钟数据目前只能作为前瞻归档和实验数据，不能回填 2021–2025 历史，也不能当成正式历史 PIT 数据。

## 4. 正式展示数据表

### stock_metadata

当前约 5,180 条。

字段：

- symbol：规范代码，例如 sh.600000
- display_name：股票名称
- board：所属板块
- updated_at：更新时间，UTC

### recent_bar

当前约 1,562,023 条。

字段：

- symbol
- trade_date
- open
- high
- low
- close
- preclose

价格全部为 RAW。

### potential_bar

当前约 771,262 条。

除日线 OHLC 和 preclose 外，还包括：

- volume：日成交量，原始单位为股
- amount：成交额，人民币元
- turnover：换手率百分比

### dragon_score_snapshot

当前约 10,291 条。

主要字段：

- trade_date
- symbol
- total_score
- dragon_rank
- board
- is_st
- family_scores_json
- feature_values_json
- explanation_json
- score_version
- created_at

### potential_snapshot

当前约 5,149 条。

主要字段：

- trade_date
- symbol
- readiness_score
- potential_rank
- state
- board
- is_st
- base_score
- participation_score
- trigger_score
- heat_risk
- component_json
- explanation_json
- exclusion_reason
- score_version
- created_at

状态语义包括：

临界启动、蓄势观察、已启动、过热、风险隔离、数据不足、数据待补。

## 5. 研究与回测表

| 表 | 用途 | 规模 |
|---|---|---:|
| potential_v2_observation | V2 每日候选因子和执行结果 | 2,411,468 |
| potential_v2_trade | V2 每日 Top 10 | 13,258 |
| potential_v3_trade | V3 健康蓄势过滤结果 | 13,131 |
| potential_v4_candidate | V4 蓄势结构候选池 | 2,177,316 |
| potential_v4_trade | V4 每日组合 | 26,424 |
| potential_v5_trade | V5 市场环境修正组合 | 13,197 |
| potential_backtest_trade | 早期潜龙回测 | 85,483 |
| potential_v2_run 至 potential_v5_run | 运行元数据、版本、指标和判定 | 多个 |
| potential_v6_diagnostic_run | 持有周期和成本诊断 | 1 |
| potential_v7_confirmation_run | 分钟确认试验 | 1 |

研究表必须使用 run_id 隔离不同运行。分数只能在同一个 run_id、同一个版本内比较。

常见执行状态：

VALID、DELAYED_EXIT、LIMITED_ENTRY、NO_ENTRY、NO_EXIT、NO_FUTURE_BAR。

## 6. Outcome 数据

数据库：

data/dragon_score_v1_outcome/outcome-v1.sqlite

表：

outcome_snapshot

规模约 5,909,957 条。

主要结果字段：

- open_ret
- high_ret
- low_ret
- close_ret
- open_to_high
- open_to_low
- open_to_close
- amplitude
- touch_limit
- close_limit
- limit_status
- outcome_status

该表是未来结果数据，严格禁止在实时评分、潜龙排序或 T 日特征计算中读取，只能用于回测和研究。

## 7. 分钟数据表

### intraday_bar_5m

字段：

- provider
- symbol
- bar_time
- open
- high
- low
- close
- volume
- amount
- turnover_pct
- retrieved_at
- semantic_available_at
- raw_sha256

分钟成交量单位为手，成交额单位为人民币元。VWAP 必须计算为：

VWAP = amount / (volume × 100)

### intraday_provider_manifest

记录请求编号、Provider、股票、粒度、请求地址、抓取时间、原始路径、SHA-256、返回根数、PIT 状态和错误信息。

### intraday_feature_snapshot

当前分钟代理因子：

- flow_imbalance_proxy
- absorption_proxy
- distribution_proxy
- above_vwap_ratio
- late_pressure_proxy
- late_return
- intraday_range_pct
- close_location_mean
- breakout_quality_proxy

这些是分钟 OHLCV 代理指标，不等同于真实主动买卖、逐笔资金流或盘口数据。

当前状态必须视为：

pit_status = PROSPECTIVE_ARCHIVE_ONLY

## 8. 时间、价格和 PIT 规范

### 标识符

股票代码统一使用交易所前缀：

sh.600000  
sz.000001  
sz.300001  
sh.688001

### 日期和时间

- trade_date：YYYY-MM-DD
- bar_time：YYYY-MM-DD HH:MM
- retrieved_at：UTC ISO 8601

trade_date 和 bar_time 的交易语义为 Asia/Shanghai。

### 价格策略

所有历史特征使用：

price_series_policy_version = price-series-policy-v1

当日报价收益定义为：

RAW close / RAW preclose - 1

禁止使用当前 Provider 下载的 QFQ 序列回填历史。

### PIT 策略

pit_policy_version = pit-policy-v1

历史 PIT 判断原则：

provider_available_at <= score_cutoff_time(as_of_date)

retrieved_at 只表示本次抓取时间，不能替代历史可用时间。

## 9. HTTP API

默认服务地址：

http://127.0.0.1:8765

当前服务只绑定本机，不应直接暴露到公网。

### 查询接口

- GET /api/status
- GET /api/top20
- GET /api/potential
- GET /api/search?q=关键词
- GET /api/detail?symbol=sh.600000
- GET /api/potential/detail?symbol=sh.600000
- GET /api/history
- GET /api/backtest/potential

### 操作接口

- POST /api/sync
- POST /api/enrich-participation
- POST /api/backtest/potential
- POST /api/draw

V2–V7 研究结果目前没有稳定 HTTP API，研究项目应使用只读 SQLite 或另行增加研究接口。

## 10. 推荐接入方式

### 产品展示项目

优先使用：

- /api/top20
- /api/potential
- /api/detail
- /api/potential/detail
- /api/status

### 本地研究项目

使用 SQLite 只读连接，并指定 run_id：

    import sqlite3

    db = r"C:\Users\明静流\.codex\.chatgpt-projects\g-p-6aa97eb562848191ae89ab3fd1c2c3dc\data\dragon_score_v1_app\dragon_score_v1.sqlite"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)

研究结果读取示例：

    SELECT
        signal_date,
        symbol,
        rank,
        score,
        status,
        entry_date,
        exit_date,
        net_return
    FROM potential_v4_trade
    WHERE run_id = :run_id
    ORDER BY signal_date, rank;

## 11. 禁止事项

接入项目不得：

- 把 outcome_snapshot 作为实时特征；
- 把 V2–V7 回测结果展示成已验证收益承诺；
- 把 PROSPECTIVE_ARCHIVE_ONLY 分钟数据当成历史 PIT 数据；
- 混用不同 run_id 的分数；
- 混用不同 price_series_policy_version 的价格；
- 把分钟 OHLCV 代理称为真实主动买卖；
- 把 F047/F048 恢复为历史有效值；
- 直接写入主数据库业务表；
- 在没有权限控制的情况下把本地 API 暴露到公网。

## 12. 当前能力结论

当前可供其他项目使用：

- 股票搜索；
- Dragon Score 当前快照；
- 潜龙状态快照；
- 日线 RAW 走势；
- 历史量能数据；
- 研究回测明细；
- 近期分钟前瞻归档；
- 数据血缘和版本信息。

当前不能宣称：

- 潜龙策略已经通过交易有效性验证；
- 分钟增强策略已经正式通过回测；
- 分钟数据覆盖完整历史；
- 分数是收益预测概率；
- 数据可以直接驱动自动交易。

