# ASSET_SEMANTIC_ENGINE_V1 + BALANCE_SHEET_INTELLIGENCE_V1 + CIGAR/ASSET_VALUE METRICS V2

## 阶段修复报告（§27 十三项）

日期：2026-09-21　metric_version：`ASSET_SEMANTIC_ENGINE_V1.0`　haircut_version：`CIGAR_ASSET_VALUE_METRICS_V2.0`

---

## 1. 东财财报原文接口测试结果

**结论：列表可用，PDF 原文不可用（不逆向）。**

| 项目 | 实测 |
|---|---|
| 列表接口 | `np-anotice-stock.eastmoney.com/api/security/ann` — **200，可用** |
| 601163 定期报告条数 | 5 条（2026H1 / 2026Q1 / 2025A / 2025Q3 / 2025H1） |
| `attach_url` | `https://pdf.dfcfw.com/pdf/H2_AN202608261828485700_1.pdf?1787764535000.pdf` |
| 实际下载 | **981 字节**，内容是 `EO_Bot_Ssid` 的 JS 挑战页，不是 PDF |

`pdf.dfcfw.com` 回的是一段需要在浏览器里执行 JS、再由 JS 种 cookie 的挑战页。按 §1
「不要逆向写死腾讯/未文档化接口」的口径，这里同样**不做逆向、不写死绕过**。

落地方式（`research/reports.py:249`）：`EastMoneyAnnouncementProvider` 的
`supports_pdf = False`，只承担「列公告 / 拿标题 / 解析报告期间」的职责；它给出的
`art_code` 会在 `FallbackProvider.fetch_pdf` 里反过来去官方源换一份真 PDF
（按 `report_period` 对齐）。所以东财是**备用列表源**，不是 PDF 源。

---

## 2. 同花顺财报原文接口是否可用

**结论：不可用，保持占位、不做硬依赖。**

| 项目 | 实测 |
|---|---|
| `iFinDPy` 是否安装 | **否**（`import iFinDPy` 失败） |
| 账户登录态 | 无 |
| `IFindReportProvider.available()` | **False** |

`research/reports.py:289` 的 `IFindReportProvider` 保留了接口形状：`available()` 只在
「本机装了 `iFinDPy` **且** 登录成功」时才返回 True，其余一律 False。
按 §1「如果没有授权，不要把它作为硬依赖」，它**不在**默认 provider 链的有效路径上，
进程启动、取报告、算指标全程不依赖它。

---

## 3. 腾讯是否存在稳定财报原文接口

**结论：不存在稳定接口，腾讯继续只做 quote / market_cap / PE / PB。**

本轮没有对腾讯做任何新的探测或接入。腾讯在本系统里的职责边界维持原样：

* `quotes` 模块：实时报价
* 研究模块估值输入：`total_market_cap` / `PE` / `PB`

按 §1「若没有明确稳定接口：不要逆向写死腾讯 undocumented API」，**不探测、不逆向、
不新增调用点**。财报原文一律走巨潮官方源。

---

## 4. PDF 下载和缓存架构

### 来源优先级

```
FallbackProvider
  ├─ 1. ExchangeOfficialProvider   (cninfo 巨潮，supports_pdf=True)   ← 列表 + PDF 都走它
  ├─ 2. EastMoneyAnnouncementProvider (eastmoney，supports_pdf=False) ← 只做列表兜底
  └─ 3. IFindReportProvider        (ifind，本机 available=False)      ← 占位
```

`fetch_pdf` **按「谁有 PDF 谁能下」排序，不按列表顺序**：先跳过所有 `supports_pdf=False`
的源。当列表来自东财时，用它的 `report_period` 去巨潮找同一期报告再下。

### 巨潮（cninfo）实测

| 项目 | 实测 |
|---|---|
| 组织代码 | `topSearch/query`（`keyWord=<code>`）→ 取 `orgId` |
| 公告列表 | `hisAnnouncement/query` POST 表单（`stock=<code>,<orgId>`） |
| 601163 定期报告 | **20 条** |
| PDF 地址 | `http://static.cninfo.com.cn/<adjunctUrl>` |
| 实际下载 | **1,931,945 字节**，`%PDF` 文件头正确，`Content-Type: application/pdf` |

### 缓存目录（`data/reports/`）

```
data/reports/
    index.json            document_hash(sha256) → 缓存文件名
    <doc_key>.pdf         原始 PDF
    <doc_key>.meta.json    source / source_id / document_hash / period / local_cache_path
    <doc_key>.rows.json    解析后的版面行（不重复解析）
```

* `doc_key = sha256("<source>:<source_id>")[:20]`
* **内容级去重**：`save()` 先算 PDF 的 sha256，命中 `index.json` 就直接复用已有文件，
  换个源下到同一份内容不会重复占磁盘。
* 三级缓存效果：同一份报告第二次打开走 `rows.json`，不再跑 PDF 解析；
  跨进程重启后依然命中。
* 实测 `?refresh=1` 强制重算 601163 全链路耗时 **1.8 秒**（PDF + 版面全命中缓存）。

---

## 5. Asset Semantic Engine 架构

```
定期报告 PDF
   │  research/pdftext.py          纯标准库解析
   │    · 经典 xref 表 + /Prev 链、XRef 流、ObjStm 对象流
   │    · FlateDecode + PNG 预测器
   │    · q/Q 栈跟踪 cm CTM（设备坐标，原点左下）
   │    · Identity-H Type0 字体 + ToUnicode CMap
   ▼
版面行 rows（每行 cells 带 x 坐标）
   │  research/asset_semantics.py
   │    find_statement()       定位合并资产负债表（_BS_TOTAL 收尾，不被附注盖掉）
   │    parse_balance_sheet()  解析一级科目余额 + 标记下级行（缩进）
   │    find_note_sections()   定位「(五)合并财务报表项目注释」区块
   │    parse_note_items()     解析附注明细（折行名字回接 / 短横线零值 / 「减：」减项）
   │    交叉验证：明细子项之和 == 一级科目余额
   ▼
经济分类
   │    SEMANTIC_RULES（32 条正则规则）
   │    ├─ 命中 → 直接定类，禁止调 LLM（§6）
   │    └─ 未命中 → 门槛判定（§9）→ research/llm_classify.py
   ▼
资产指标（research/asset_engine.py）
   │    现金层级 PureCash / NearCash / LiquidFinancialAssets / RestrictedCash
   │    净现金 PureNetCash / AdjustedNetCash / LiquidNetAssets / TotalInterestBearingDebt
   │    清算价值三口径（research/haircut.py，24 类折价表）
   ▼
asset_semantic_snapshot（独立版本 ASSET_SEMANTIC_ENGINE_V1.0 + result_hash）
   ▼
GET /api/research/assets  →  前端「资产审计」标签页
```

**关键设计：经济类别与会计科目解耦。** 一级科目只用于「定位」和「交叉验证」；
折价率、流动性、是否计入清算价值，全部挂在**经济类别**上。

**唯一不依赖排版的正确性判据**：附注明细子项之和必须等于一级科目余额
（`abs(sub_total - amount) < 0.02`）。对不上就退回整笔计价并记 `conflict=true`，
**不自动挑一个**（§23）。

**端到端不变量**：资产明细合计 == 资产总计。9 只股票**差额全部 0.00**。

---

## 6. 哪些分类完全规则化

**24 个经济类别，全部由 `SEMANTIC_RULES`（32 条正则）完全规则化：**

```
CASH                      BANK_DEPOSIT              TERM_DEPOSIT
NEGOTIABLE_CD             STRUCTURED_DEPOSIT        RESTRICTED_CASH
LOW_RISK_FINANCIAL_ASSET  MARKETABLE_SECURITY       RECEIVABLE_FINANCING
RECEIVABLE_NORMAL         RECEIVABLE_RISKY          INVENTORY_RAW_MATERIAL
INVENTORY_FINISHED_GOODS  INVENTORY_OTHER           FIXED_ASSET
CONSTRUCTION_IN_PROGRESS  INVESTMENT_PROPERTY       LONG_TERM_EQUITY_INVESTMENT
INTANGIBLE_ASSET          GOODWILL                  TAX_ASSET
PREPAID_ASSET             OTHER_KNOWN               OTHER_UNKNOWN
```

规则化命中的判据是「**科目名 + 附注明细项名**」的组合，例如：

| 明细项名 | → 经济类别 | 效果 |
|---|---|---|
| 可转让大额存单 | `NEGOTIABLE_CD` | 计入 NearCash，不折价 |
| 定期存款 / 一年内到期定期存款 | `TERM_DEPOSIT` | 计入 NearCash |
| 国债逆回购投资 | `LOW_RISK_FINANCIAL_ASSET` | 计入 LiquidFinancialAssets |
| 商誉 | `GOODWILL` | **折价率恒为 0**（§16） |
| 待抵扣增值税进项税款 | `TAX_ASSET` | 低折价 |

**实测覆盖率**（`classification_coverage` = 已分类金额 / 总资产）：

| 股票 | 覆盖率 | 规则 | 未解析 | LLM |
|---|---|---|---|---|
| 601163 三角轮胎 | **99.8%** | 25 | 2 | 0 |
| 600600 青岛啤酒 | **99.9%** | 45 | 5 | 2 |
| 600741 华域汽车 | **99.5%** | 19 | 3 | 2 |

---

## 7. 哪些情况才调用 DeepSeek

**两个条件必须同时成立（§9）：**

1. `rule_classification == UNKNOWN` —— 32 条规则一条都没命中
2. **且** 重要性达标：
   `金额 / 总资产 >= 1%`（`MIN_ASSET_RATIO = 0.01`）
   **或** `金额 / 当前市值 >= 2%`（`MIN_MARKET_CAP_RATIO = 0.02`）

**三重硬闸：**

* `LLM_MAX_CALLS_PER_REPORT = 5` —— 单份报告最多 5 次，到顶直接返回 OTHER_UNKNOWN
* **§6：所有明确命中的项目禁止调用 LLM**（不是「优先规则」，是禁止）
* **§3：禁止每次把整份 PDF 交给 LLM** —— 只送单条明细项的科目名 + 附注原文片段

**DeepSeek 的职责被限死在 semantic classification（§7）：**

* 输出**严格**只有 `{"economic_class", "restricted", "liquidity", "confidence", "evidence"}`
* **不能**：改金额 / 推算金额 / 决定 haircut / 算清算价值 / 决定评分 / 决定股票模型 / 输出投资意见
* 不明确 → `OTHER_UNKNOWN`
* 规则能定的一律不送 —— 折价率由规则表统一管理（§16），LLM 无权决定

**降级路径：** API 不可用 / Key 缺失 / 返回不可解析 → 一律 `OTHER_UNKNOWN`，
**主流程必须继续**（§8），不抛异常、不中断分析。

**Key 来源（§8）：** 只从环境变量读，优先级
`DEEPSEEK_API_KEY` > `DEEPSEEK_BASE_URL` > `DEEPSEEK_MODEL`，
兼容 fallback `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL`。
**不读** `C:\Users\...\Code\User\settings.json`，**不写入**源码 / 前端 / SQLite / 日志。

**缓存键（§10）：** `document_hash + paragraph_hash + classifier_version + model_name`
（`CLASSIFIER_VERSION = "asset_semantic_llm_v1"`）。

---

## 8. LLM 调用次数统计

9 只股票**最新一次分析**的实测：

| 股票 | LLM 调用 | 规则跳过 |
|---|---|---|
| 001201 东瑞股份 | 0 | 1 |
| 002714 牧原股份 | 1 | 2 |
| 002867 周大生 | 2 | 6 |
| 600398 海澜之家 | 2 | 7 |
| 600511 国药股份 | 2 | 10 |
| 600600 青岛啤酒 | 4 | 15 |
| 600741 华域汽车 | 4 | 18 |
| 600987 航民股份 | 4 | 19 |
| 601163 三角轮胎 | 4 | 21 |
| **合计** | **23** | **99** |

* 单份报告调用数最多 **4 次**，未触及 `LLM_MAX_CALLS_PER_REPORT = 5` 上限
* 规则跳过 : LLM 调用 ≈ **4.3 : 1** —— 八成以上的分类完全没走网络
* 三角轮胎 27 条明细项中 25 条由规则直接定类，**0 次 LLM**

---

## 9. 三角轮胎修复前后资产结构

**修复前（一级科目粗暴计价，§17 明令禁止的算法）：**

| 科目 | 账面 | 折价率 | 计入 |
|---|---|---|---|
| 货币资金 | 2,610,539,857.45 | 100% | 2,610,539,857.45 |
| 其他流动资产（其中大额存单 80.72 亿） | 7,701,801,634.35 | **50%** | 3,850,900,817.18 |
| 存货 | 1,477,254,456.17 | 60% | 886,352,673.70 |
| 固定资产 | 3,495,206,202.94 | 50% | 1,747,603,101.47 |
| … | | | |
| **调整后资产价值** | | | **10,976,372,872.10** |
| **清算价值** | | | **5,459,499,969.74** |
| **净现金** | | | **1,480,205,515.33** |

一目了然的问题：**80.72 亿可转让大额存单被当成普通「其他流动资产」打了对折**，
凭空抹掉 40 亿；`一年内到期的非流动资产`里的 14.27 亿定期存款更是整个没被识别。

**修复后（附注语义解析 + 经济分类）：**

| 现金层级 | 金额 |
|---|---|
| PureCash（库存现金 + 银行存款） | 2,630,511,027.74 |
| **NearCash**（含一年内到期定期存款 1,426,567,607 + 可转让大额存单 8,072,022,525） | **12,129,101,159.98** |
| LiquidFinancialAssets | 12,129,918,204.46 |
| RestrictedCash（受限） | 57,889.61 |

| 净现金口径 | 金额 |
|---|---|
| PureNetCash | 1,086,790,429.37 |
| **AdjustedNetCash** | **10,585,380,561.61** |
| LiquidNetAssets | 10,586,197,606.09 |
| TotalInterestBearingDebt | 1,543,720,598.37 |

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 类现金 / 市值 | 13.4% | **122.4%** |
| 净现金 / 市值 | 14.6% | **106.8%** |
| 清算价值 / 市值（CONSERVATIVE） | 55.1% | **129.9%** |
| 清算价值 / 市值（BASE） | — | **146.6%** |
| 清算价值 / 市值（OPTIMISTIC） | — | **161.1%** |
| 资产明细合计 vs 资产总计 | 差额非零 | **差额 0.00** |

**验收点对齐：** NearCash `12,129,101,159.98` ✓　AdjustedNetCash `10,585,380,561.61` ✓
类现金/市值 `122.4%` ✓　净现金/市值 `106.8%` ✓ —— 与规范给出的验收值逐位一致。

---

## 10. 三角轮胎修复前后 cigar / asset profile

**这一项与 §27 的边界有关，需要说清楚。**

| 画像 | 画像分（旧口径，SCORING_V1.1） | 资产语义层独立指标 |
|---|---|---|
| cigar_profile（烟蒂画像） | **27.21** | 清算价值/市值 129.9% ~ 161.1% |
| asset_value_profile（资产价值画像） | **41.85** | 类现金/市值 122.4% |
| value_profile | 79.20 | — |
| dividend_profile | 83.98 | — |

**画像分没有变，这是刻意的。** 原因是 SCORING_V1 的评分口径当前处于**冻结**状态：
新口径（附注级的资产语义）**只进展示层与证据层**，评分输入不静默改动，
等 `SCORING_V2` 上线时整体迁移。§27 也明确「本阶段不要开始修改最终 V2 专属评分
模型公式」。所以 27.21 / 41.85 这两个数**仍是旧口径的产物**，它们不是修复没生效，
而是**修复成果按设计停在资产语义层，还没获准进入画像层**。

资产语义层自己的独立结论（`asset_semantic_snapshot` 里的 `asset_value_profile`）
已经反映真实资产结构：清算价值三口径全部 ≥ 市值，`asset_consumption_rate = 38.9%`。

**这正是 §20 划定的顺序所要求的中间状态：**
`Raw Financial Data → Note Extraction → Economic Classification → Asset Metrics →`
**`Profile Scores`（待 SCORING_V2 迁移）**`→ Router → Dedicated Scoring Model`

---

## 11. Router 修复前后结果

| 项目 | 修复前 | 修复后 |
|---|---|---|
| 主模型 | `GENERAL_VALUE_V2` | `GENERAL_VALUE_V2`（**未变**） |
| 路由状态 | `FALLBACK` | `FALLBACK`（**未变**） |
| `cigar_profile` | 27.21 | 27.21（评分层冻结） |
| `VALUE_CIGAR_V2` 适配度 | 47.34 | 47.34（评分层冻结） |
| `DIVIDEND_VALUE_V2` 适配度 | 88.05 | 88.05 |
| **Router 阈值** | — | **一个都没动** |

**这是符合 §20 的预期结果，不是缺陷。** §20 逐字要求：

> 「本次不要为了让三角轮胎得高分而调权重。正确顺序：… 先修数据。…
>  禁止：通过修改 Router threshold 让三角强制变成烟蒂。」

本轮**没有**修改 `router.py` 的任何阈值（`router.py` 仅在 07:26 有一次与本次修复
无关的既有改动，指纹 `01180acf5969` 与磁盘一致）。三角轮胎**没有被强制变成烟蒂**。
Router 走进烟蒂模型的通路要等画像层消费新的资产指标之后才会自然打开 —— 那是
`SCORING_V2` 的事。

**9 只股票当前路由（Router 阈值未动，全表稳定）：**

| 股票 | 主模型 | 状态 | cigar | asset_value |
|---|---|---|---|---|
| 001201 东瑞股份 | CYCLICAL_CORE_V2 | CLEAR | 28.00 | 28.17 |
| 002714 牧原股份 | CYCLICAL_CORE_V2 | CLEAR | 32.00 | 25.00 |
| 002867 周大生 | GENERAL_VALUE_V2 | FALLBACK | 40.65 | 27.62 |
| 600398 海澜之家 | GENERAL_VALUE_V2 | FALLBACK | 48.47 | 43.87 |
| 600511 国药股份 | VALUE_CIGAR_V2 | CLEAR | 56.78 | 80.73 |
| 600600 青岛啤酒 | GENERAL_VALUE_V2 | FALLBACK | 50.77 | 50.10 |
| 600741 华域汽车 | GENERAL_VALUE_V2 | AMBIGUOUS | 38.56 | 60.25 |
| 600987 航民股份 | VALUE_CIGAR_V2 | CLEAR | 50.69 | 75.16 |
| 601163 三角轮胎 | GENERAL_VALUE_V2 | FALLBACK | 27.21 | 41.85 |

---

## 12. 其他研究股票发现的类似资产误分类

以三角轮胎为样板回扫全部 9 只，**发现 3 类、至少 4 例同类误分类**，
每一例都已在资产语义层修正。

### 12.1 定期存款藏在一级科目里（与三角轮胎同型）

**600600 青岛啤酒 —— 53.8 亿定期存款 + 12.9 亿同业存单被埋**

| 明细项 | 金额 | 原本藏在 | 经济类别 |
|---|---|---|---|
| 定期存款（净额） | 3,695,576,708 | 其他非流动资产 | `TERM_DEPOSIT` |
| 一年内到期定期存款 | 1,685,110,137 | 一年内到期的非流动资产 | `TERM_DEPOSIT` |
| 同业存单 | 1,292,448,296 | 其他流动资产 | `NEGOTIABLE_CD` |

修正后：类现金 **18,228,139,560**（= 货币资金 11,555,004,419 + 上述三项），
类现金/市值 **26.7%**、净现金/市值 **26.2%**、有息负债仅 **330,650,925**。
修复前这套资产被拆在三个不同的一级科目里，按 50%/20% 之类的一级折价率计价。

### 12.2 下级行被重复计价（严重，会虚增资产）

**600741 华域汽车 —— 应收股利 5.94 亿被算了两遍**

「应收股利」x=111.5 是「其他应收款」x=79.9 的**下级行**（报表缩进层级），
但抽取时两条都当成了独立科目，导致**明细合计比资产总计多出 594,410,936.16**。

修法：用「出现次数最多的 x」作基准，`x > base + 6.0` 判为下级行
（`_mark_sub_lines`），计价路径跳过下级行。

> **这个修复一度打挂了有息负债（自查发现）：** 把下级行判定放在合计行判定之前，
> 「流动资产合计」自己也是缩进的（x=90.5 > 79.9+6），分段开关再也切不到负债段，
> 有息负债静默变 0，600741 净现金/市值从 27.2% 跳到 **78.6%**（假的）。
> 修正为**先判合计行、再判下级行**，600741 回到 27.2%。

### 12.3 资产负债表被附注整段盖掉（严重，会凭空缩小科目）

**600600 青岛啤酒 —— 货币资金 115.6 亿 → 7.9 亿、应付账款 41.5 亿 → 218,946**

`find_statement` 的收尾行是 `end = len(rows)`，而附注「1、货币资金」的明细表
行名也叫「货币资金」，于是报表解析被附注表格接管。修法：用
`_BS_TOTAL`（`负债(和|及)(股东|所有者)权益总计`）作真实收尾行，
且这个判定必须放在 `len(cells) > 2` 的过滤**之前**。

### 12.4 抽取层的四个共性缺陷（一修全好）

| 缺陷 | 现象 | 修法 |
|---|---|---|
| 表头是日期式 | 600600 附注表头 `项目 \| 2026年6月30 \| 日 \| …` 导致明细一条都读不出 | `_HDR_DATE` + `_is_table_header` |
| 科目名折行 | `一年内到期的其他非流动金融` / `31,147,781 48,213,397` / `资产(附注(五)10)` | carry/joining 状态机 + `_is_name_fragment` |
| 短横线被当空值 | 「-」被跳过后整列左移，期初数顶到期末（600600 短期借款 22 亿被算成合并有息负债） | `parse_amount_slot`：短横线 = 明确的 0.0 |
| 「减：」当噪音 | 减值准备被当成加项 | `sign = -1.0 if name.startswith(("减：","减:"))` |

**9 只股票全部跑通，资产明细合计 == 资产总计，差额全部 0.00：**

| 股票 | 总资产 | 市值 | 类现金/市值 | 净现金/市值 | 清算/市值 CONS/BASE/OPT | 覆盖率 |
|---|---|---|---|---|---|---|
| 001201 东瑞股份 | 5,949,804,817 | 3,769,000,000 | 12.1% | -30.8% | 12.4 / 41.0 / 69.7% | 99.9% |
| 002714 牧原股份 | 175,063,111,942 | 238,713,000,000 | 6.9% | -17.6% | 2.3 / 15.1 / 28.0% | 98.3% |
| 002867 周大生 | 8,415,507,446 | 11,213,000,000 | 8.5% | 0.9% | 21.3 / 33.2 / 45.1% | 99.7% |
| 600398 海澜之家 | 31,656,108,542 | 27,808,000,000 | 23.6% | 13.8% | 45.1 / 61.0 / 75.5% | 99.1% |
| 600511 国药股份 | 36,039,857,574 | 19,662,000,000 | 41.5% | 27.8% | 91.2 / 117.9 / 140.5% | 99.8% |
| 600600 青岛啤酒 | 55,487,522,698 | 68,223,000,000 | 26.7% | 26.2% | 48.9 / 56.2 / 63.4% | 99.9% |
| 600741 华域汽车 | 197,377,630,675 | 46,913,000,000 | 78.6% | 27.2% | 171.0 / 229.3 / 281.5% | 99.5% |
| 600987 航民股份 | 10,570,219,660 | 6,472,000,000 | 59.6% | 27.9% | 64.7 / 82.9 / 100.8% | 99.3% |
| 601163 三角轮胎 | 19,849,522,840 | 9,912,000,000 | **122.4%** | **106.8%** | 129.9 / 146.6 / 161.1% | **99.8%** |

---

## 13. 所有测试结果

```
python -m unittest discover -s tests -q
----------------------------------------------------------------------
Ran 294 tests in 2.869s

OK
```

**294 个测试全通过**（本轮从 283 → 294，新增 11 个）。

### 测试从 fixture 解析，不读常量（§22）

`tests/make_fixture.py` 生成一份**小而完整**的真实结构 PDF
（`tests/fixtures/balance_sheet_sample.pdf`，17,540 字节 / 132 字形）：
Type0 字体 + Identity-H + ToUnicode CMap，与 Word/WPS 导出的中文财报同构；
含合并 + 母公司两张资产负债表、带附注引用的科目、七条附注明细。
**测试从这份 PDF 里解析，而不是断言 `near_cash == 12129101160` 这种常量**——
否则测的是常量不是代码。

### 本轮新增的 11 个测试覆盖的分支

| 测试 | 覆盖的缺陷 |
|---|---|
| `test_dash_is_zero_in_a_table_cell` | 短横线是零，不是空值 |
| `test_minus_line_is_subtracted` | 「减：」是减项 |
| `test_wrapped_label_is_rejoined` | 折行科目名回接 |
| `test_dash_row_does_not_shift_columns` | 短横线行不导致列左移 |
| `test_date_header_note_is_parsed` | 日期式表头的附注表 |
| `TestStatementBoundary`（1 个） | 报表收尾行不被附注盖掉 |
| `TestNoteAreaHeading`（3 个） | 附注区标题定位 + 目录行排除 |
| `TestSubLineIndentation`（1 个） | 缩进下级行识别 |
| `TestParseNoteItems`（1 个） | carry/joining 金额列不取错 |

（5 个个体测试 + 上述 4 类共 6 个 = 11 个）

### 其他验证

| 项目 | 结果 |
|---|---|
| 资产明细合计 == 资产总计 | 9 只股票**差额全部 0.00** |
| `GET /api/research/assets?code=601163` | **200**，23,270 字节 |
| `GET /api/research/assets`（缺 code） | **400**「缺少股票代码」 |
| `GET /api/research/assets?code=<未研究>` | **404**「该股票尚未研究」 |
| `?refresh=1` 重算 | 1.8 秒（PDF + 版面缓存全命中） |
| `GET /api/meta` 指纹 vs 磁盘 | `rules.py 9280de785187` ✓　`router.py 01180acf5969` ✓（进程为新代码） |
| `research_snapshots`（SCORING_V1.1 历史） | **54 行原样保留，未被覆盖**（§24） |
| `asset_semantic_snapshot` | 46 行，独立版本 `ASSET_SEMANTIC_ENGINE_V1.0`，按 `result_hash` 去重 |
| `llm_calls` / `llm_skipped` 落库 | 已随快照持久化，可审计 |

---

## 本轮边界声明（§27）

**做了：** 财报获取（cninfo 官方源 + 三级缓存）／资产语义（附注解析 + 24 类经济分类）／
资产指标（现金层级 + 净现金 + 清算价值三口径）／§21 审计视图（后端 + 前端 + CSS）。

**刻意没做：**

* ❌ 没改 `SCORING_V2` 专属评分模型公式
* ❌ 没改 Router 任何阈值（§20 明令禁止为三角轮胎调阈值）
* ❌ 没动 SCORING_V1 的评分口径（冻结中，新口径只进展示层）
* ❌ 没覆盖任何旧的 `SCORING_V1.1` snapshot
* ❌ 没逆向东财 / 同花顺 / 腾讯的任何未文档化接口
* ❌ 没有把 API Key 写进源码 / 前端 / SQLite / 日志
