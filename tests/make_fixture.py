# -*- coding: utf-8 -*-
"""生成 tests/fixtures/balance_sheet_sample.pdf。

**为什么要专门造一份 fixture，而不是拿真实财报当测试依据：**

真实财报有 1.9 MB、113 页，塞进测试目录既笨重又无法审查——没人看得出
「这份 PDF 里到底该有什么」。而测试如果直接断言 ``near_cash == 12129101160``，
那测的是常量而不是代码：抽取错了、折价表改错了，只要数字凑巧没变就照样通过。

这份 fixture 是一份**小而完整**的财报：合并 + 母公司两张资产负债表、带附注
引用的科目、以及七条附注明细。每个分支都有对应数据——

* 可转让大额存单藏在「其他流动资产」里（本次要修的核心场景）
* 受限现金写在附注正文而不是表格里
* 存货拆成原材料 / 库存商品
* 商誉存在但必须折价为零
* 母公司报表的科目名与合并报表重名，用来验证选表选对了

生成出来的 PDF 用系统中立的方式描述：Type0 字体 + Identity-H + ToUnicode
CMap，与 Word/WPS 导出的中文财报同构。测试从这份 PDF 里解析，而不是读常量。

用法（改动 fixture 内容后需重新生成）::

    python tests/make_fixture.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fixtures", "balance_sheet_sample.pdf")

FONT_SIZE = 10.0
COL_NAME, COL_NOTE, COL_CUR, COL_PRIOR = 60.0, 250.0, 380.0, 500.0

# --------------------------------------------------------------------------- #
# 文档内容
# --------------------------------------------------------------------------- #
#: 每页是一串 (x, 文本) 的单元格，按行给出；行在页面上从上往下排。
PAGE1 = [
    [(COL_NAME, "合并资产负债表")],
    [(COL_NAME, "编制单位：样本股份有限公司"), (COL_CUR, "单位：元 币种：人民币")],
    [(COL_NAME, "项目"), (COL_NOTE, "附注"), (COL_CUR, "期末余额"), (COL_PRIOR, "期初余额")],
    [(COL_NAME, "货币资金"), (COL_NOTE, "七、1"), (COL_CUR, "2,631,000,000.00"), (COL_PRIOR, "2,610,000,000.00")],
    [(COL_NAME, "交易性金融资产"), (COL_NOTE, "七、2"), (COL_CUR, "10,000,000.00"), (COL_PRIOR, "9,000,000.00")],
    [(COL_NAME, "应收账款"), (COL_NOTE, "七、3"), (COL_CUR, "500,000,000.00"), (COL_PRIOR, "480,000,000.00")],
    [(COL_NAME, "存货"), (COL_NOTE, "七、4"), (COL_CUR, "400,000,000.00"), (COL_PRIOR, "420,000,000.00")],
    [(COL_NAME, "其他流动资产"), (COL_NOTE, "七、5"), (COL_CUR, "900,000,000.00"), (COL_PRIOR, "800,000,000.00")],
    [(COL_NAME, "流动资产合计"), (COL_CUR, "4,441,000,000.00"), (COL_PRIOR, "4,319,000,000.00")],
    [(COL_NAME, "固定资产"), (COL_NOTE, "七、6"), (COL_CUR, "1,200,000,000.00"), (COL_PRIOR, "1,250,000,000.00")],
    [(COL_NAME, "商誉"), (COL_NOTE, "七、7"), (COL_CUR, "300,000,000.00"), (COL_PRIOR, "300,000,000.00")],
    [(COL_NAME, "资产总计"), (COL_CUR, "5,941,000,000.00"), (COL_PRIOR, "5,869,000,000.00")],

    # ---- 流动负债：完整一段，含各类有息/非有息科目 ----
    [(COL_NAME, "流动负债：")],
    [(COL_NAME, "短期借款"), (COL_NOTE, "七、8"), (COL_CUR, "800,000,000.00"), (COL_PRIOR, "850,000,000.00")],
    [(COL_NAME, "应付票据"), (COL_CUR, "50,000,000.00"), (COL_PRIOR, "40,000,000.00")],
    # 完全没有金额的科目：不能因为它是空的就把整段当成缺失
    [(COL_NAME, "持有待售负债")],
    [(COL_NAME, "应付账款"), (COL_CUR, "600,000,000.00"), (COL_PRIOR, "590,000,000.00")],
    # 「其中：」是下级行，已经含在「其他应付款」里了，不许再加一遍
    [(COL_NAME, "其他应付款"), (COL_CUR, "30,000,000.00"), (COL_PRIOR, "29,000,000.00")],
    [(COL_NAME, "其中：应付利息"), (COL_CUR, "5,000,000.00"), (COL_PRIOR, "4,000,000.00")],
    [(COL_NAME, "合同负债"), (COL_CUR, "120,000,000.00"), (COL_PRIOR, "110,000,000.00")],
    [(COL_NAME, "应付职工薪酬"), (COL_CUR, "30,000,000.00"), (COL_PRIOR, "28,000,000.00")],
    [(COL_NAME, "应交税费"), (COL_CUR, "20,000,000.00"), (COL_PRIOR, "19,000,000.00")],
    [(COL_NAME, "一年内到期的非流动负债"), (COL_CUR, "50,000,000.00"), (COL_PRIOR, "45,000,000.00")],
    # 期末那一栏**整格没印**（不是「-」）——只剩期初一个金额。按位置认栏位：
    # 它印在期初栏，所以期末是 0，期初才是这个数。认错的话这两笔会加进
    # 流动负债，流动负债合计就永远对不平。
    [(COL_NAME, "预收款项"), (COL_PRIOR, "4,242,100.00")],
    [(COL_NAME, "其他流动负债"), (COL_PRIOR, "360,234.26")],
    [(COL_NAME, "流动负债合计"), (COL_CUR, "1,700,000,000.00"), (COL_PRIOR, "1,715,602,334.26")],

    # ---- 非流动负债：整段。以前的分段开关会在这里把整段漏掉 ----
    [(COL_NAME, "非流动负债：")],
    [(COL_NAME, "长期借款"), (COL_CUR, "400,000,000.00"), (COL_PRIOR, "430,000,000.00")],
    [(COL_NAME, "应付债券"), (COL_CUR, "200,000,000.00"), (COL_PRIOR, "200,000,000.00")],
    [(COL_NAME, "租赁负债"), (COL_CUR, "100,000,000.00"), (COL_PRIOR, "90,000,000.00")],
    [(COL_NAME, "递延收益"), (COL_CUR, "20,000,000.00"), (COL_PRIOR, "18,000,000.00")],
    [(COL_NAME, "预计负债"), (COL_CUR, "30,000,000.00"), (COL_PRIOR, "27,000,000.00")],
    [(COL_NAME, "递延所得税负债"), (COL_CUR, "50,000,000.00"), (COL_PRIOR, "45,000,000.00")],
    [(COL_NAME, "非流动负债合计"), (COL_CUR, "800,000,000.00"), (COL_PRIOR, "810,000,000.00")],
    [(COL_NAME, "负债合计"), (COL_CUR, "2,500,000,000.00"), (COL_PRIOR, "2,525,602,334.26")],

    [(COL_NAME, "实收资本"), (COL_CUR, "3,441,000,000.00"), (COL_PRIOR, "3,343,397,665.74")],
    [(COL_NAME, "负债和所有者权益总计"), (COL_CUR, "5,941,000,000.00"), (COL_PRIOR, "5,869,000,000.00")],
]

PAGE2 = [
    [(COL_NAME, "母公司资产负债表")],
    [(COL_NAME, "编制单位：样本股份有限公司"), (COL_CUR, "单位：元 币种：人民币")],
    [(COL_NAME, "项目"), (COL_NOTE, "附注"), (COL_CUR, "期末余额"), (COL_PRIOR, "期初余额")],
    [(COL_NAME, "货币资金"), (COL_CUR, "1,000,000,000.00"), (COL_PRIOR, "990,000,000.00")],
    [(COL_NAME, "资产总计"), (COL_CUR, "3,000,000,000.00"), (COL_PRIOR, "2,980,000,000.00")],
    [(COL_NAME, "负债和所有者权益总计"), (COL_CUR, "3,000,000,000.00"), (COL_PRIOR, "2,980,000,000.00")],
]

PAGE3 = [
    [(COL_NAME, "七、合并财务报表项目注释")],
    [(COL_NAME, "1、货币资金")],
    [(COL_NAME, "项目"), (COL_CUR, "期末余额"), (COL_PRIOR, "期初余额")],
    [(COL_NAME, "库存现金"), (COL_CUR, "100,000.00"), (COL_PRIOR, "90,000.00")],
    [(COL_NAME, "银行存款"), (COL_CUR, "2,630,000,000.00"), (COL_PRIOR, "2,609,000,000.00")],
    [(COL_NAME, "其他货币资金"), (COL_CUR, "900,000.00"), (COL_PRIOR, "910,000.00")],
    [(COL_NAME, "合计"), (COL_CUR, "2,631,000,000.00"), (COL_PRIOR, "2,610,000,000.00")],
    [(COL_NAME, "（1）截至2026年6月30日，货币资金中使用受限的其他货币资金合计50,000.00元。")],

    [(COL_NAME, "2、交易性金融资产")],
    [(COL_NAME, "项目"), (COL_CUR, "期末余额"), (COL_PRIOR, "期初余额")],
    [(COL_NAME, "理财产品"), (COL_CUR, "10,000,000.00"), (COL_PRIOR, "9,000,000.00")],
    [(COL_NAME, "合计"), (COL_CUR, "10,000,000.00"), (COL_PRIOR, "9,000,000.00")],

    [(COL_NAME, "3、应收账款")],
    [(COL_NAME, "项目"), (COL_CUR, "期末余额"), (COL_PRIOR, "期初余额")],
    [(COL_NAME, "应收账款"), (COL_CUR, "500,000,000.00"), (COL_PRIOR, "480,000,000.00")],
    [(COL_NAME, "合计"), (COL_CUR, "500,000,000.00"), (COL_PRIOR, "480,000,000.00")],

    [(COL_NAME, "4、存货")],
    [(COL_NAME, "项目"), (COL_CUR, "期末余额"), (COL_PRIOR, "期初余额")],
    [(COL_NAME, "原材料"), (COL_CUR, "150,000,000.00"), (COL_PRIOR, "160,000,000.00")],
    [(COL_NAME, "库存商品"), (COL_CUR, "250,000,000.00"), (COL_PRIOR, "260,000,000.00")],
    [(COL_NAME, "合计"), (COL_CUR, "400,000,000.00"), (COL_PRIOR, "420,000,000.00")],

    [(COL_NAME, "5、其他流动资产")],
    [(COL_NAME, "项目"), (COL_CUR, "期末余额"), (COL_PRIOR, "期初余额")],
    # 折行的科目名：名字太长时排版会折行，金额印在中间那一行。接不回去，
    # 这一项就丢了，明细合计对不上，整条附注作废。
    [(COL_NAME, "待抵扣增值税进项")],
    [(COL_CUR, "100,000,000.00"), (COL_PRIOR, "90,000,000.00")],
    [(COL_NAME, "税额")],
    [(COL_NAME, "可转让大额存单"), (COL_CUR, "700,000,000.00"), (COL_PRIOR, "600,000,000.00")],
    # 短横线是「这一栏是零」，不是「没印」。当成空值丢掉，后面的列会整体
    # 左移一格，期初的 5,000 万会顶到期末。
    [(COL_NAME, "国债逆回购投资"), (COL_CUR, "-"), (COL_PRIOR, "50,000,000.00")],
    # 「减：」是减项，财务上的写法，不是排版噪音
    [(COL_NAME, "减：其他流动资产减值准备"), (COL_CUR, "10,000.00"), (COL_PRIOR, "10,000.00")],
    [(COL_NAME, "其他"), (COL_CUR, "100,010,000.00"), (COL_PRIOR, "60,000,000.00")],
    [(COL_NAME, "合计"), (COL_CUR, "900,000,000.00"), (COL_PRIOR, "800,000,000.00")],

    [(COL_NAME, "6、固定资产")],
    # 表头不写「期末余额」，直接把报表日印上去（青岛啤酒就是这么印的）
    [(COL_NAME, "项目"), (COL_CUR, "2026年6月30"), (COL_PRIOR, "日 2025年12月31 日")],
    [(COL_NAME, "房屋建筑物"), (COL_CUR, "1,200,000,000.00"), (COL_PRIOR, "1,250,000,000.00")],
    [(COL_NAME, "合计"), (COL_CUR, "1,200,000,000.00"), (COL_PRIOR, "1,250,000,000.00")],

    [(COL_NAME, "7、商誉")],
    [(COL_NAME, "项目"), (COL_CUR, "期末余额"), (COL_PRIOR, "期初余额")],
    [(COL_NAME, "商誉"), (COL_CUR, "300,000,000.00"), (COL_PRIOR, "300,000,000.00")],
    [(COL_NAME, "合计"), (COL_CUR, "300,000,000.00"), (COL_PRIOR, "300,000,000.00")],
]


# --------------------------------------------------------------------------- #
# PDF 生成
# --------------------------------------------------------------------------- #
def _layout(page, top=800.0, leading=22.0):
    """把单元格按行铺到页面上，返回 (x, y, text) 列表。

    y 从大到小——PDF 用户空间原点在左下角，第一行必须 y 最大。
    """
    out = []
    y = top
    for row in page:
        for x, text in row:
            out.append((x, y, text))
        y -= leading
    return out


def _content(pages_positions, gid_of):
    parts = []
    for positions in pages_positions:
        for x, y, text in positions:
            hexed = "".join("%04X" % gid_of[ch] for ch in text)
            parts.append(
                f"BT /F1 {FONT_SIZE} Tf 1 0 0 1 {x} {y} Tm <{hexed}> Tj ET")
    return "\n".join(parts).encode("latin-1")


def _tounicode(gid_of):
    chars = sorted(gid_of, key=lambda c: gid_of[c])
    lines = ["/CIDInit /ProcSet findresource begin",
             "12 dict begin", "begincmap",
             "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
             "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange",
             "%d beginbfchar" % len(chars)]
    for ch in chars:
        lines.append("<%04X> <%s>" % (gid_of[ch], ch.encode("utf-16-be").hex().upper()))
    lines += ["endbfchar", "endcmap",
              "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines).encode("latin-1")


def build():
    pages = [_layout(p) for p in (PAGE1, PAGE2, PAGE3)]

    gid_of = {}
    for positions in pages:
        for _, _, text in positions:
            for ch in text:
                if ch not in gid_of:
                    gid_of[ch] = len(gid_of) + 1

    content = _content(pages, gid_of)
    tounicode = _tounicode(gid_of)

    objs = {}
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kids = " ".join(f"{7 + 2 * i} 0 R" for i in range(len(pages)))
    objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    objs[3] = b"<< /Type /Font /Subtype /Type0 /BaseFont /Sample /Encoding /Identity-H" \
              b" /DescendantFonts [4 0 R] /ToUnicode 5 0 R >>"
    objs[4] = b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Sample" \
              b" /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >>" \
              b" /FontDescriptor 6 0 R /W [0 [1000]] >>"
    objs[5] = b"<< /Length %d >>\nstream\n" % len(tounicode) + tounicode + b"\nendstream"
    objs[6] = b"<< /Type /FontDescriptor /FontName /Sample /Flags 4" \
              b" /FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 900" \
              b" /Descent -200 /CapHeight 700 /StemV 80 >>"
    for i in range(len(pages)):
        objs[7 + 2 * i] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842]"
            f" /Resources << /Font << /F1 3 0 R >> >> /Contents {8 + 2 * i} 0 R >>"
        ).encode()
        # 每页各写各的内容流：三页共用同一份会让三页叠在一起
        one = _content([pages[i]], gid_of)
        objs[8 + 2 * i] = b"<< /Length %d >>\nstream\n" % len(one) + one + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n"
    xref_at = len(out)
    top = max(objs) + 1
    out += b"xref\n0 %d\n" % top
    out += b"0000000000 65535 f \n"
    for num in range(1, top):
        out += b"%010d 00000 n \n" % offsets.get(num, 0)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (top, xref_at)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "wb") as f:
        f.write(bytes(out))
    return OUT, len(out), len(gid_of)


# --------------------------------------------------------------------------- #
# Adobe-GB1 的合成 fixture：Type0 + 无 ToUnicode + 内嵌字体程序
# --------------------------------------------------------------------------- #
#: 这份 fixture 里印的字。含汉字、半角数字、以及一个**符号区**的标点——
#: 三段 CID 编号规则各占一段，三段都要走到。
GB1_TEXT = "资产合计13,785,280，负债合计12,431,979"


def _gb1_cid(ch):
    """这个字符在 Adobe-GB1 里的 CID。

    这里**按字符集定义独立算一遍**，不复用 ``research.pdftext.gb1_cmap``：
    测试要验的正是那张表，用它自己来造输入就什么都验不出来。

    规则来自 GB2312 本身：``1..95`` 是半角 ASCII（``chr(cid + 31)``），``96``
    起是 GB2312 的 1~9 区符号**按已分配位置排名**，``940`` 起是 16 区往后的
    94×94 方阵线性对应。
    """
    raw = ch.encode("gb2312")
    if len(raw) == 1:
        return raw[0] - 0x1F
    row, pos = raw[0] - 0xA0, raw[1] - 0xA0
    if row >= 16:
        return 940 + 94 * (row - 16) + (pos - 1)
    rank = 0
    for r in range(1, row):
        rank += _assigned_in_row(r)
    for p in range(1, pos):
        rank += 1 if _assigned(row, p) else 0
    return 96 + rank


def _assigned(row, pos):
    try:
        bytes([0xA0 + row, 0xA0 + pos]).decode("gb2312")
    except UnicodeDecodeError:
        return False
    return True


def _assigned_in_row(row):
    return sum(1 for p in range(1, 95) if _assigned(row, p))


def gb1_pdf(*, ordering=b"GB1", with_tounicode=False, text=GB1_TEXT):
    """造一份「CID-keyed、没有 ToUnicode」的单页 PDF，返回字节。

    招商银行半年报的内嵌字体就是这个形状：Type0 + Identity-H + 没有
    ``/ToUnicode`` + ``/CIDSystemInfo`` 写着 ``(Adobe, GB1)``，而 ``/Encoding``
    的 ``Identity-H`` 只说明「两个字节是一个 CID」，不说明 CID 对应哪个字符。
    字符信息在字体程序之外——这就是这份 fixture 要复现的全部。

    ``ordering`` / ``with_tounicode`` 是给**门禁**用的：把字符集换成 Identity
    或 Japan1、或者补上 ToUnicode，派生表就该完全不起作用。
    """
    cids = [_gb1_cid(ch) for ch in text]
    hexed = "".join("%04X" % c for c in cids)
    content = (f"BT /F1 {FONT_SIZE} Tf 1 0 0 1 {COL_NAME} 700 Tm "
               f"<{hexed}> Tj ET").encode("latin-1")

    objs = {}
    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = b"<< /Type /Pages /Kids [7 0 R] /Count 1 >>"
    objs[3] = b"<< /Type /Font /Subtype /Type0 /BaseFont /Sample-GB1" \
              b" /Encoding /Identity-H /DescendantFonts [4 0 R]" + (
                  b" /ToUnicode 5 0 R" if with_tounicode else b"") + b" >>"
    objs[4] = (b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Sample-GB1"
               b" /CIDSystemInfo << /Registry (Adobe) /Ordering (%s)"
               b" /Supplement 4 >> /FontDescriptor 6 0 R /W [0 [1000]] >>"
               % ordering)
    # 内嵌字体程序：一份最小的合法 CFF（CID-keyed 时里面**没有字形名**，
    # 这正是「字符信息不在字体里」的意思）。这里放一段能被解析器认出的头部
    # 即可——这一层从不去读字体程序。
    tu = _tounicode_for(cids, text)
    objs[5] = b"<< /Length %d >>\nstream\n" % len(tu) + tu + b"\nendstream"
    objs[6] = b"<< /Type /FontDescriptor /FontName /Sample-GB1 /Flags 4" \
              b" /FontBBox [0 -200 1000 900] /ItalicAngle 0 /Ascent 900" \
              b" /Descent -200 /CapHeight 700 /StemV 80 /FontFile3 9 0 R >>"
    objs[7] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842]"
               b" /Resources << /Font << /F1 3 0 R >> >> /Contents 8 0 R >>")
    objs[8] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
    objs[9] = b"<< /Subtype /CIDFontType0C /Length 8 >>\nstream\n01234567\nendstream"

    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n"
    xref_at = len(out)
    top = max(objs) + 1
    out += b"xref\n0 %d\n" % top
    out += b"0000000000 65535 f \n"
    for num in range(1, top):
        out += b"%010d 00000 n \n" % offsets.get(num, 0)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (top, xref_at)
    return bytes(out)


def _tounicode_for(cids, text):
    """给 ``with_tounicode=True`` 那条路用的 CMap（走不到时也照样生成，成本可忽略）。"""
    lines = ["/CIDInit /ProcSet findresource begin",
             "12 dict begin", "begincmap",
             "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
             "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
             "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange",
             "%d beginbfchar" % len(cids)]
    for cid, ch in zip(cids, text):
        lines.append("<%04X> <%s>" % (cid, ch.encode("utf-16-be").hex().upper()))
    lines += ["endbfchar", "endcmap",
              "CMapName currentdict /CMap defineresource pop", "end", "end"]
    return "\n".join(lines).encode("latin-1")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(HERE))
    path, size, glyphs = build()
    print(f"已生成 {path}  ({size} 字节, {glyphs} 个字形)")

    from research import pdftext
    with open(path, "rb") as f:
        runs, doc = pdftext.extract_runs(f.read())
    rows = pdftext.to_rows(runs)
    print(f"回读校验：{len(runs)} 个文本片，{len(rows)} 行")
    for r in rows[:6]:
        print("   ", " | ".join(c["text"] for c in r["cells"]))
