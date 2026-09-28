# -*- coding: utf-8 -*-
"""research/industry — **行业专属数据层**。

## 为什么另开一层

canonical factor 层（``research/factors.py``）只认「一个 factor 的一个读数」，
它不该知道「生猪完全成本」这种东西是怎么从一份 PDF 的分产品表里核出来的。
而猪企的逻辑一旦散进 ``factors.py`` / ``rules.py`` / ``dimensions.py``，
下一次要接一个别的行业（比如水泥、航运）时就没有地方可放——三份文件里
各会多出一段 ``if industry == ...``。

所以每个行业一个适配器，形态统一：

    ``IndustryAdapter.state(code, ...) -> dict``

返回的东西**就是** canonical factor 层 ``context`` 里的那一段
（猪是 ``context["pig"]``），额外带上这一行行业的**指标记录表**。
适配器自己的纪律：

* **不联网**（联网取数属于证据层，如 ``pig_sales``）；
* **永不抛异常**（取不到就是 missing，并且给出**哪一种** missing）；
* **不判分**（分数在 canonical factor 层算）；
* **不手填**（没有可审计来源就是 missing，宁可缺，不要伪精确）。

见 :mod:`research.industry.pig`。
"""

from research.industry import pig      # noqa: F401  （包内导出，便于 `from research.industry import pig`）

__all__ = ["pig"]
