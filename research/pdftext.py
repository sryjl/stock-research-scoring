# -*- coding: utf-8 -*-
"""pdftext.py — 纯标准库的 PDF 文本 / 版面抽取。

为什么要自己写：这个项目是零依赖的（只用 Python 标准库），而财报原文只有
PDF 一种形态。不引第三方库就只能自己解析。

只做三件事，别的都不做：

1. 对象层：xref（含 /Prev 增量更新链）、对象、流解码（FlateDecode + PNG 预测器）、
   对象流（ObjStm）、XRef 流。
2. 字体层：Type0 + Identity-H 的 ToUnicode CMap → Unicode。
3. 版面层：内容流的文本算子 → 带 (page, x, y) 的文本片段。

不做：渲染、图片、注释、加密（遇到加密抛 :class:`PDFEncrypted`）。

**输出为什么带 x / y**：财报附注是「靠坐标对齐的表格」，文本流里的先后顺序
是没有意义的——「可转让大额存单」和它右边那串金额可能在流里隔着半页。
所以每个片段都保留自己的起点坐标，行列还原完全建在这两个数上。
"""
import math
import re
import zlib


class PDFError(Exception):
    """PDF 结构层面的错误。"""


class PDFEncrypted(PDFError):
    """加密 PDF：本模块不处理，交由上层降级。"""


# --------------------------------------------------------------------------- #
# 对象层
# --------------------------------------------------------------------------- #
_WS = b"\x00\t\n\x0c\r "
_DELIM = b"()<>[]{}/%"


class Name(bytes):
    """PDF 名字对象（``/Foo``）。"""


class Str(bytes):
    """PDF 字符串对象（字面量或十六进制）。与裸 bytes 区分，后者只用于算子。"""


class Ref:
    """间接引用（``12 0 R``）。"""
    __slots__ = ("num", "gen")

    def __init__(self, num, gen):
        self.num = num
        self.gen = gen

    def __repr__(self):
        return f"{self.num} {self.gen} R"


def _utf16be(raw):
    """把 UTF-16BE 字节解成字符串；失败返回空串而不是抛错。"""
    if len(raw) == 1:
        return chr(raw[0])
    try:
        return raw.decode("utf-16-be", "ignore")
    except Exception:
        return ""


class _Lexer:
    """够用的 PDF 对象词法分析器。"""

    __slots__ = ("d", "i", "n")

    def __init__(self, d, i=0):
        self.d = d
        self.i = i
        self.n = len(d)

    def skip(self):
        d, n = self.d, self.n
        while self.i < n:
            c = d[self.i]
            if c in _WS:
                self.i += 1
            elif c == 0x25:                     # % 注释到行尾
                while self.i < n and d[self.i] not in b"\r\n":
                    self.i += 1
            else:
                return

    def _token(self):
        d = self.d
        s = self.i
        while self.i < self.n and d[self.i] not in _WS and d[self.i] not in _DELIM:
            self.i += 1
        return d[s:self.i]

    def parse(self):
        self.skip()
        if self.i >= self.n:
            raise PDFError("对象解析越界")
        c = self.d[self.i]
        if c == 0x2F:                                   # /
            return self._name()
        if c == 0x28:                                   # (
            return self._lit_string()
        if c == 0x3C:                                   # <
            if self.d[self.i + 1:self.i + 2] == b"<":
                return self._dict()
            return self._hex_string()
        if c == 0x5B:                                   # [
            return self._array()
        if c in b"0123456789+-.":
            return self._number()
        return self._keyword()

    def _name(self):
        self.i += 1
        raw = self._token()
        if b"#" in raw:
            out = bytearray()
            k = 0
            while k < len(raw):
                if raw[k] == 0x23 and k + 2 < len(raw) + 1:
                    try:
                        out.append(int(raw[k + 1:k + 3], 16))
                        k += 3
                        continue
                    except ValueError:
                        pass
                out.append(raw[k])
                k += 1
            raw = bytes(out)
        return Name(raw)

    def _lit_string(self):
        self.i += 1
        d, n = self.d, self.n
        out = bytearray()
        depth = 1
        while self.i < n:
            c = d[self.i]
            if c == 0x5C:                               # 反斜杠转义
                self.i += 1
                if self.i >= n:
                    break
                e = d[self.i]
                mapped = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}.get(e)
                if mapped is not None:
                    out.append(mapped)
                    self.i += 1
                elif 0x30 <= e <= 0x37:                 # 1~3 位八进制
                    k = self.i
                    while self.i < n and self.i - k < 3 and 0x30 <= d[self.i] <= 0x37:
                        self.i += 1
                    out.append(int(d[k:self.i], 8) & 0xFF)
                elif e in b"\r\n":                      # 续行
                    self.i += 1
                    if e == 0x0D and self.d[self.i:self.i + 1] == b"\n":
                        self.i += 1
                else:
                    out.append(e)
                    self.i += 1
                continue
            if c == 0x28:
                depth += 1
            elif c == 0x29:
                depth -= 1
                if depth == 0:
                    self.i += 1
                    return Str(bytes(out))
            out.append(c)
            self.i += 1
        return Str(bytes(out))

    def _hex_string(self):
        self.i += 1
        d = self.d
        s = self.i
        while self.i < self.n and d[self.i] != 0x3E:
            self.i += 1
        raw = re.sub(rb"[^0-9A-Fa-f]", b"", d[s:self.i])
        self.i += 1
        if len(raw) % 2:
            raw += b"0"
        try:
            return Str(bytes.fromhex(raw.decode("ascii")))
        except ValueError:
            return Str(b"")

    def _array(self):
        self.i += 1
        out = []
        while True:
            self.skip()
            if self.i >= self.n:
                return out
            if self.d[self.i] == 0x5D:                  # ]
                self.i += 1
                return out
            out.append(self.parse())

    def _dict(self):
        self.i += 2
        out = {}
        while True:
            self.skip()
            if self.i >= self.n:
                return out
            if self.d[self.i:self.i + 2] == b">>":
                self.i += 2
                return out
            key = self.parse()
            if not isinstance(key, Name):
                # 容错：键坏了就丢掉这一项，不要让整份文档解析失败
                continue
            out[bytes(key)] = self.parse()

    def _number(self):
        t = self._token()
        try:
            val = float(t) if b"." in t else int(t)
        except ValueError:
            return t
        # 「12 0 R」形式：只有在确实跟着第二个整数和 R 时才当引用
        if isinstance(val, int) and val >= 0:
            save = self.i
            self.skip()
            if self.d[self.i:self.i + 1].isdigit():
                t2 = self._token()
                try:
                    gen = int(t2)
                except ValueError:
                    gen = None
                if gen is not None:
                    self.skip()
                    if self.d[self.i:self.i + 1] == b"R":
                        self.i += 1
                        return Ref(val, gen)
            self.i = save
        return val

    def _keyword(self):
        t = self._token()
        if t == b"true":
            return True
        if t == b"false":
            return False
        if t == b"null":
            return None
        if not t:
            # 不认识的定界符：跳过一个字节，保证一定前进
            self.i += 1
            return b""
        return t


def _scan_objects(data):
    """兜底：正则扫全文件的 ``N G obj``。

    只在 xref 坏了/丢了的时候用。后出现的覆盖先出现的——增量更新的语义就是
    后写的对象生效。
    """
    out = {}
    for m in re.finditer(rb"(?:^|[\s>\]\)])(\d{1,9})\s+(\d{1,5})\s+obj\b", data):
        out[int(m.group(1))] = m.start(1)
    return out


def _plausible_char(cp):
    """两字节码位看起来像不像一个真的字符。

    排除代理区、私用区和未分配区：中文子集字体的 CID 是 Unicode 码位，而
    西文子集字体的 CID 是顺序编码（1、2、3……），后者解出来是控制字符。
    不设这道闸，顺序编码的字体会被解成一串乱码，比丢掉更糟——乱码会以
    「文本」的身份流进版面重建，可能凑出看着像数字的东西。
    """
    if cp < 0x20:
        return False
    if 0xD800 <= cp <= 0xDFFF:      # 代理区
        return False
    if 0xE000 <= cp <= 0xF8FF:      # 私用区
        return False
    if 0xFFF0 <= cp <= 0xFFFF:
        return False
    return cp <= 0x10FFFF


def _apply_png_predictor(data, colors, bpc, columns):
    """PNG 预测器（PDF 的 /Predictor >= 10 也是这一套）。"""
    bpp = max(1, colors * bpc // 8)
    rowlen = (columns * colors * bpc + 7) // 8
    out = bytearray()
    prev = bytearray(rowlen)
    i = 0
    n = len(data)
    while i + 1 <= n - 1:
        ft = data[i]
        i += 1
        row = bytearray(data[i:i + rowlen])
        if len(row) < rowlen:
            row.extend(b"\x00" * (rowlen - len(row)))
        i += rowlen
        if ft == 1:
            for k in range(bpp, rowlen):
                row[k] = (row[k] + row[k - bpp]) & 0xFF
        elif ft == 2:
            for k in range(rowlen):
                row[k] = (row[k] + prev[k]) & 0xFF
        elif ft == 3:
            for k in range(rowlen):
                left = row[k - bpp] if k >= bpp else 0
                row[k] = (row[k] + ((left + prev[k]) >> 1)) & 0xFF
        elif ft == 4:
            for k in range(rowlen):
                a = row[k - bpp] if k >= bpp else 0
                b = prev[k]
                c = prev[k - bpp] if k >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[k] = (row[k] + pr) & 0xFF
        out.extend(row)
        prev = row
    return bytes(out)


class Document:
    """一份 PDF。构造即完成对象层解析。"""

    def __init__(self, data):
        self.data = data
        self.xref = {}
        self.trailer = {}
        self.cache = {}
        self.undecodable = 0
        self.synthetic_cmap = 0     # 靠 Latin-1 / UTF-16BE 兜底解出来的片段数
        self.derived_cmap = 0       # 靠 Adobe-GB1 的字符集定义派生出来的片段数
        self.compressed = {}        # 对象流内的对象：号 → (ObjStm 号, 流内序号)
        # objstm 必须在这里就位：读交叉引用流要用 get()，而 get() 会查它。
        # 只在 _expand_objstms 里初始化的话，交叉引用流的路径会在展开之前就
        # 撞上 AttributeError。
        self.objstm = {}
        self.xref_error = None
        self._load()

    # ---- 对象层 ----
    def _load(self):
        if b"/Encrypt" in self.data[-4096:]:
            # 尾部 trailer 里有 /Encrypt 就是加密文档，不猜口令
            raise PDFEncrypted("PDF 已加密，无法解析文本")
        try:
            self._read_xref()
        except Exception as exc:
            # 兜底可以兜底，但不能装作无事发生：xref 读失败会让「页面数对不对」
            # 这种问题在界面上一片空白，查起来毫无线索。把原因留下来。
            self.xref_error = f"{type(exc).__name__}: {exc}"
            self.xref = {}
            self.trailer = {}
        if not self.xref:
            self.xref = _scan_objects(self.data)
        self._expand_objstms()

    def _read_xref(self):
        d = self.data
        i = d.rfind(b"startxref")
        if i < 0:
            raise PDFError("没有 startxref")
        m = re.search(rb"startxref\s+(\d+)", d[i:])
        if not m:
            raise PDFError("startxref 后面没有偏移量")
        off = int(m.group(1))
        seen = set()
        while off and off not in seen and off < len(d):
            seen.add(off)
            off = self._read_xref_section(off)

    def _read_xref_section(self, off):
        d = self.data
        lx = _Lexer(d, off)
        lx.skip()
        if d[lx.i:lx.i + 4] == b"xref":
            lx.i += 4
            while True:
                lx.skip()
                if d[lx.i:lx.i + 7] == b"trailer":
                    lx.i += 7
                    t = lx.parse()
                    if isinstance(t, dict):
                        merged = dict(t)
                        merged.update(self.trailer)     # 靠前的更新优先
                        self.trailer = merged
                        prev = t.get(b"Prev")
                        return self._maybe_stream_xref(t) or (prev if isinstance(prev, int) else None)
                    return None
                start = lx.parse()
                count = lx.parse()
                if not isinstance(start, int) or not isinstance(count, int):
                    return None
                for k in range(count):
                    lx.skip()
                    o = lx.parse()
                    g = lx.parse()
                    t = lx.parse()
                    if isinstance(o, int) and t == b"n":
                        self.xref[start + k] = o
                    _ = g
        # XRef 流（PDF 1.5+）
        if d[lx.i:lx.i + 3] == b"obj":
            return None
        obj = self._parse_indirect_at(off)
        if isinstance(obj, dict) and obj.get(b"Type") == b"XRef":
            self._read_xref_stream(obj)
            prev = self.resolve(obj.get(b"Prev"))
            return prev if isinstance(prev, int) else None
        return None

    def _maybe_stream_xref(self, trailer):
        # 混合引用文件：trailer 里的 /XRefStm 指向 XRef 流
        x = self.resolve(trailer.get(b"XRefStm"))
        if isinstance(x, int):
            obj = self.get(x)
            if isinstance(obj, dict) and obj.get(b"Type") == b"XRef":
                self._read_xref_stream(obj)
                return None
        return None

    def _read_xref_stream(self, obj):
        """解析 PDF 1.5+ 的交叉引用流。

        ``/W`` 给出每个字段的字节宽度，字段顺序是**固定的**：类型、第二字段、
        第三字段。类型 1 表示「未压缩对象」，第二字段是文件偏移；类型 2 表示
        「在对象流里」，第二字段是对象流号、第三字段是流内序号。

        这里曾经把类型读成最后一个字段、把偏移读成第一个字段——对默认的
        ``/W [1 2 1]`` 恰好错得很隐蔽（偏移取到了类型字节），对青岛啤酒这种
        ``/W [1 4 2]`` 就直接一条都读不出来，整份报告的正文全部丢失。字段宽度
        是文件说了算的，不能按印象写死。

        另外，交叉引用流**同时充当 trailer**：``/Root``、``/Info`` 就挂在这个
        字典上。不接住它们，就没有目录入口，页面树也就无从谈起。
        """
        data = self.get_stream(obj)
        if data is None:
            return
        w = [self.resolve(v) or 0 for v in (self.resolve(obj.get(b"W")) or [])]
        if len(w) < 3:
            return
        size = self.resolve(obj.get(b"Size")) or 0
        index = [self.resolve(v) for v in (self.resolve(obj.get(b"Index")) or [0, size])]
        p = 0
        for s in range(0, len(index) - 1, 2):
            start = index[s] or 0
            count = index[s + 1] or 0
            for k in range(count):
                vals = []
                for width in w:
                    if p + width > len(data):
                        vals.append(None)
                    elif width:
                        vals.append(int.from_bytes(data[p:p + width], "big"))
                    else:
                        vals.append(None)
                    p += width
                ftype = vals[0]
                if ftype == 1 and vals[1] is not None:
                    self.xref[start + k] = vals[1]
                elif ftype == 2 and vals[1] is not None:
                    # 对象在对象流里，记下来交给 _expand_objstms
                    self.compressed[start + k] = (vals[1], vals[2] or 0)
                # ftype == 0 是空槽，跳过
        # 交叉引用流本身就是 trailer
        merged = dict(self.trailer)
        for key in (b"Root", b"Info", b"Encrypt", b"ID"):
            if obj.get(key) is not None and merged.get(key) is None:
                merged[key] = obj[key]
        self.trailer = merged

    def _expand_objstms(self):
        """把对象流里的对象解出来，塞进 xref（指向流内偏移，用特殊标记）。

        两条来源都要收：交叉引用流里标了类型 2 的（``self.compressed``），以及
        xref 坏掉时靠扫描发现的 ``/Type /ObjStm`` 对象。只认前者的话，一旦
        xref 没读出来，对象流就永远打不开，整份文档一个页面都取不到——而这
        恰恰是最需要兜底的时候（青岛啤酒的报告就踩在这上面：xref 流解析修复
        之前，它连目录都找不到）。
        """
        self.objstm = {}
        pending = set(self.compressed) if self.compressed else set()
        for num in list(self.xref):
            obj = self.get(num)
            if isinstance(obj, dict) and obj.get(b"Type") == b"ObjStm":
                data = self.get_stream(obj)
                if not data:
                    continue
                n = self.resolve(obj.get(b"N")) or 0
                first = self.resolve(obj.get(b"First")) or 0
                head = data[:first]
                nums = [int(x) for x in re.findall(rb"\d+", head)]
                for k in range(min(n, len(nums) // 2)):
                    onum = nums[2 * k]
                    ooff = nums[2 * k + 1]
                    # 已经在外面标了位置的以外面为准（那是有 xref 的权威来源）
                    if onum in pending and onum in self.objstm:
                        continue
                    self.objstm[onum] = (num, first + ooff)
                    self.xref.setdefault(onum, None)
                    self.cache.pop(onum, None)

    def _parse_indirect_at(self, off):
        lx = _Lexer(self.data, off)
        first = lx.parse()
        if isinstance(first, int):
            lx.parse()                       # generation
            kw = lx.parse()
            if kw != b"obj":
                return first
            return self._parse_with_stream(lx)
        return first

    def _parse_with_stream(self, lx):
        obj = lx.parse()
        if not isinstance(obj, dict):
            return obj
        save = lx.i
        lx.skip()
        if lx.d[lx.i:lx.i + 6] != b"stream":
            lx.i = save
            return obj
        lx.i += 6
        if lx.d[lx.i:lx.i + 2] == b"\r\n":
            lx.i += 2
        elif lx.d[lx.i:lx.i + 1] in (b"\r", b"\n"):
            lx.i += 1
        start = lx.i
        length = self.resolve(obj.get(b"Length"))
        if isinstance(length, int) and 0 <= length <= len(lx.d) - start:
            raw = lx.d[start:start + length]
        else:
            # /Length 是坏引用或不存在：按 endstream 找
            end = lx.d.find(b"endstream", start)
            raw = lx.d[start:end if end >= 0 else len(lx.d)]
        obj = dict(obj)
        obj[b"__stream__"] = raw
        return obj

    def get(self, num):
        if num in self.cache:
            return self.cache[num]
        obj = None
        if num in self.objstm:
            host, off = self.objstm[num]
            h = self.get(host)
            data = self.get_stream(h) if isinstance(h, dict) else None
            if data:
                try:
                    obj = _Lexer(data, off).parse()
                except Exception:
                    obj = None
        else:
            off = self.xref.get(num)
            if isinstance(off, int):
                try:
                    obj = self._parse_indirect_at(off)
                except Exception:
                    obj = None
        self.cache[num] = obj
        return obj

    def resolve(self, obj):
        """跟着 Ref 走到底（带环路保护）。"""
        seen = 0
        while isinstance(obj, Ref):
            obj = self.get(obj.num)
            seen += 1
            if seen > 32:
                return None
        return obj

    def get_stream(self, obj):
        obj = self.resolve(obj)
        if not isinstance(obj, dict):
            return None
        raw = obj.get(b"__stream__")
        if raw is None:
            return None
        return self._decode(obj, raw)

    def _decode(self, obj, raw):
        filters = self.resolve(obj.get(b"Filter"))
        if filters is None:
            return raw
        if isinstance(filters, (Name, bytes, str)):
            filters = [filters]
        parms = self.resolve(obj.get(b"DecodeParms")) or {}
        if isinstance(parms, dict):
            parms = [parms]
        out = raw
        for k, f in enumerate(filters):
            f = bytes(f) if isinstance(f, (Name, bytes)) else b""
            if f in (b"FlateDecode", b"Fl"):
                out = self._inflate(out)
                pm = self.resolve(parms[k]) if k < len(parms) else None
                if isinstance(pm, dict):
                    out = self._unpredict(pm, out)
            elif f in (b"ASCIIHexDecode", b"AHx"):
                out = bytes.fromhex(re.sub(rb"[^0-9A-Fa-f]", b"", out.split(b">")[0]).decode("ascii", "ignore") + "0" * 0)
            elif f in (b"LZWDecode", b"LZW"):
                out = b""                       # 不实现：极罕见，交给上层降级
            else:
                # DCTDecode / JPXDecode / CCITTFaxDecode 是图像，不是文本
                return None
        return out

    @staticmethod
    def _inflate(data):
        for wbits in (15, -15, 47):
            try:
                return zlib.decompress(data, wbits)
            except zlib.error:
                continue
        try:
            d = zlib.decompressobj()
            return d.decompress(data)
        except zlib.error:
            return b""

    def _unpredict(self, pm, data):
        pred = self.resolve(pm.get(b"Predictor")) or 1
        if pred < 2:
            return data
        colors = self.resolve(pm.get(b"Colors")) or 1
        bpc = self.resolve(pm.get(b"BitsPerComponent")) or 8
        columns = self.resolve(pm.get(b"Columns")) or 1
        if pred == 2:
            return data
        return _apply_png_predictor(data, colors, bpc, columns)

    # ---- 文档结构 ----
    def catalog(self):
        root = self.resolve(self.trailer.get(b"Root"))
        if isinstance(root, dict) and root.get(b"Type") == b"Catalog":
            return root
        for num in self.xref:
            obj = self.get(num)
            if isinstance(obj, dict) and obj.get(b"Type") == b"Catalog":
                return obj
        return None

    def pages(self):
        """按顺序返回页面字典列表（含继承下来的 Resources / MediaBox）。"""
        cat = self.catalog()
        if not cat:
            return []
        root = self.resolve(cat.get(b"Pages"))
        out = []

        def walk(node, inherited, depth=0):
            if depth > 64 or not isinstance(node, dict):
                return
            inh = dict(inherited)
            for key in (b"Resources", b"MediaBox", b"CropBox", b"Rotate"):
                if key in node:
                    inh[key] = node[key]
            kids = self.resolve(node.get(b"Kids"))
            if isinstance(kids, list):
                for kid in kids:
                    walk(self.resolve(kid), inh, depth + 1)
            elif node.get(b"Type") == b"Page" or b"Contents" in node:
                page = dict(node)
                for key, val in inh.items():
                    page.setdefault(key, val)
                out.append(page)

        walk(root, {})
        return out

    def page_content(self, page):
        contents = self.resolve(page.get(b"Contents"))
        if isinstance(contents, list):
            parts = [self.get_stream(c) for c in contents]
            return b"\n".join(p for p in parts if p)
        return self.get_stream(contents) or b""

    def page_fonts(self, page):
        """页面上用到的字体：名字 → {cmap, bytes}。"""
        res = self.resolve(page.get(b"Resources")) or {}
        fonts = self.resolve(res.get(b"Font")) or {}
        out = {}
        for key, ref in fonts.items():
            font = self.resolve(ref)
            if not isinstance(font, dict):
                continue
            info = {"cmap": {}, "bytes": 1, "subtype": bytes(self.resolve(font.get(b"Subtype")) or b"")}
            name = key if isinstance(key, bytes) else bytes(key)
            tu = None
            if font.get(b"ToUnicode") is not None:
                tu = self.get_stream(font.get(b"ToUnicode"))
            if tu is None:
                desc = self.resolve(font.get(b"DescendantFonts"))
                if isinstance(desc, list) and desc:
                    d0 = self.resolve(desc[0])
                    if isinstance(d0, dict) and d0.get(b"ToUnicode") is not None:
                        tu = self.get_stream(d0.get(b"ToUnicode"))
            if tu:
                cmap, width = parse_tounicode(tu)
                info["cmap"] = cmap
                info["bytes"] = width
            elif info["subtype"] in (b"Type0",):
                info["bytes"] = 2
            enc = self.resolve(font.get(b"Encoding"))
            if isinstance(enc, (Name, bytes)) and bytes(enc) == b"Identity-H":
                info["bytes"] = 2
            # ---- 单字节字体的宽度以字体自己的字形表为准，不以声明为准 ----
            #
            # 简单字体（TrueType/Type1）的字符码按规范就是一个字节，但 ToUnicode
            # CMap 的 codespacerange 有时声明成两个字节，于是 ``width`` 被读成 2。
            # 后果不是「读错一个字符」而是**整段静默消失**：单字节串 ``b" "`` 按
            # 两字节切片得到空串，这个字就没了，不报错也不计数。新希望（000876）
            # 的半年报就是这样——4 个 TrueType 字体、cmap 键全落在 0x20~0x97，
            # 结果 45,692 个字符解不出来，资产负债表一个科目都抽不出来。
            #
            # 判据是字体自己的 **cmap 键域**：键全在单字节范围内，那它就只能按
            # 单字节读——这是从这份 PDF 自己身上读出来的事实，比任何声明都可靠。
            # 复合字体（Type0）不在此列，它的两字节是规范要求的。
            if (info["subtype"] not in (b"Type0",) and info["cmap"]
                    and max(info["cmap"]) <= 0xFF):
                info["bytes"] = 1
            # ---- 没有 ToUnicode 的 Adobe-GB1 复合字体：按字符集定义派生 ----
            #
            # 走到这里还是空 cmap 的两字节字体，剩下的路只有「按 UTF-16BE 猜」
            # 那条兜底（把 CID 当 Unicode 码位）。对招商银行这种内嵌 CID-keyed
            # CFF 的文档，这条兜底会把整篇财报解成乱码：41,089 个片段里只有 1 个
            # 含汉字，而 CID 号根本不是 Unicode 码位——它是 Adobe-GB1 里的编号。
            #
            # 三道闸缺一不可：没有 cmap（有就说明字体自己声明了）、两字节（复合
            # 字体的规范宽度）、`/CIDSystemInfo` 就是 (Adobe, GB1)。第三道是
            # **前提**而不是补充说明——派生表只在 CID 属于这套字符集时成立，声明
            # 缺席或写着别的集合时一律不认，宁可解不出来。
            if (not info["cmap"] and info["bytes"] == 2
                    and _is_adobe_gb1(self, font)):
                info["cmap"] = gb1_cmap()
                info["derived"] = "adobe-gb1"
            out[name.lstrip(b"/")] = info
        return out


# --------------------------------------------------------------------------- #
# 字体层：ToUnicode CMap
# --------------------------------------------------------------------------- #
def _is_adobe_gb1(doc, font):
    """字体的 ``/CIDSystemInfo`` 是不是 ``(Adobe, GB1)``。

    这个字段声明的是「这套 CID 编号属于哪本字符集」，正是派生表成立的**前提**。
    所以只认它，不认别的线索：猜错字符集的代价不是「少几个字」而是把整篇财务
    数据解成另一种语言的乱码——那种错误在上层看起来完全像「这份报表格式特殊」。
    """
    desc = doc.resolve(font.get(b"DescendantFonts"))
    if not isinstance(desc, list) or not desc:
        return False
    d0 = doc.resolve(desc[0])
    if not isinstance(d0, dict):
        return False
    csi = doc.resolve(d0.get(b"CIDSystemInfo"))
    if not isinstance(csi, dict):
        return False
    return (bytes(csi.get(b"Registry") or b"") == b"Adobe"
            and bytes(csi.get(b"Ordering") or b"") == b"GB1")


_GB1_CMAP = None


def gb1_cmap():
    """从标准库 ``gb2312`` 现算 Adobe-GB1 的 ``CID → Unicode`` 表。

    招商银行半年报内嵌的是 CID-keyed CFF，**没有 /ToUnicode**，字形也没有名字
    （只有 ``/CIDToGIDMap``，那是 CID 到字形轮廓的对应，不携带字符信息）。字符
    信息不在字体程序里，而在 Adobe-GB1 这本字符集的定义里：它的 CID 就是按
    GB2312 的码位顺序编的。GB2312 是标准库自带的编码，于是这张表可以当场算出
    来——不内嵌表、不落盘、也不用去解析 CFF。

    编号分三段：

    * ``1..95``   —— ``chr(cid + 0x1F)``，即半角 ASCII ``0x20``~``0x7E``。
    * ``96..777`` —— GB2312 的 1~9 区（符号区），**在这 9 个区里按「已分配位置」
      的先后排名**，共 682 个；未分配的格子不占 CID。
    * ``940..``   —— GB2312 的 16 区起（汉字区），94×94 方阵线性对应：
      ``(cid - 940) // 94 + 16`` 区、``(cid - 940) % 94 + 1`` 位，到 87 区为止。
      ``778``~``939`` 这一段是空的，不填。

    表是**惰性**算的：绝大多数 PDF 里没有 Adobe-GB1 字体，不该为它付代价。
    """
    global _GB1_CMAP
    if _GB1_CMAP is not None:
        return _GB1_CMAP
    table = {}
    for cid in range(1, 96):
        table[cid] = chr(cid + 0x1F)
    rank = 0
    for row in range(1, 10):
        for pos in range(1, 95):
            try:
                ch = bytes([0xA0 + row, 0xA0 + pos]).decode("gb2312")
            except UnicodeDecodeError:
                continue            # 未分配的格子不占 CID
            table[96 + rank] = ch
            rank += 1
    for cid in range(940, 940 + 94 * 72):
        n = cid - 940
        row, pos = n // 94 + 16, n % 94 + 1
        if row > 87:
            break
        try:
            table[cid] = bytes([0xA0 + row, 0xA0 + pos]).decode("gb2312")
        except UnicodeDecodeError:
            continue
    _GB1_CMAP = table
    return table


def parse_tounicode(data):
    """解析 ToUnicode CMap，返回 (码 → 字符, 每码字节数)。"""
    cmap = {}
    width = 2
    m = re.search(rb"begincodespacerange(.*?)endcodespacerange", data, re.S)
    if m:
        first = re.search(rb"<([0-9A-Fa-f]+)>", m.group(1))
        if first:
            width = max(1, len(first.group(1)) // 2)
    for blk in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", blk):
            cmap[int(src, 16)] = _utf16be(bytes.fromhex(dst.decode("ascii")))
    for blk in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.S):
        for lo, hi, arr in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", blk, re.S):
            a = int(lo, 16)
            for k, item in enumerate(re.findall(rb"<([0-9A-Fa-f]+)>", arr)):
                cmap[a + k] = _utf16be(bytes.fromhex(item.decode("ascii")))
        stripped = re.sub(rb"<[0-9A-Fa-f]+>\s*<[0-9A-Fa-f]+>\s*\[.*?\]", b"", blk, flags=re.S)
        for lo, hi, dst in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", stripped):
            a, b = int(lo, 16), int(hi, 16)
            base = bytes.fromhex(dst.decode("ascii"))
            base_val = int.from_bytes(base, "big")
            single = len(base) <= 2
            for k in range(a, min(b, a + 65535) + 1):
                if single:
                    cmap[k] = chr(base_val + (k - a))
                else:
                    cmap[k] = _utf16be((base_val + (k - a)).to_bytes(len(base), "big"))
    return cmap, width


# --------------------------------------------------------------------------- #
# 版面层：内容流 → 文本片段
# --------------------------------------------------------------------------- #
class Run:
    """一段文本及其起点坐标。

    ``x`` / ``y`` 是**设备坐标**（原点左下、y 向上），``size`` 是换算到设备
    空间的字号——行列还原要靠它来估计「同一格内的字距」有多宽。
    """
    __slots__ = ("page", "x", "y", "text", "font", "size")

    def __init__(self, page, x, y, text, font, size):
        self.page = page
        self.x = x
        self.y = y
        self.text = text
        self.font = font
        self.size = size

    def __repr__(self):
        return f"Run(p{self.page} x={self.x:.1f} y={self.y:.1f} {self.text!r})"


def _mul(m, n):
    """两个 2x3 文本矩阵相乘（PDF 的行向量约定）。"""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D,
            c * A + d * C, c * B + d * D,
            e * A + f * C + E, e * B + f * D + F)


class _Content:
    """内容流解释器：只关心文本，别的算子直接跳过。"""

    def __init__(self, data, fonts, page_no, sink, doc):
        self.d = data
        self.fonts = fonts
        self.page = page_no
        self.sink = sink
        self.doc = doc

    def run(self):
        lx = _Lexer(self.d)
        stack = []
        tm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        tlm = tm
        # CTM 不能省。Word/WPS 导出的中方财报大量使用 `1 0 0 -1 0 792 cm`
        # 把坐标系翻过来，此时裸的 Tm 里 y 越大越靠下，直接拿它排行会把
        # 整张表读成倒的。只有把 Tm 乘进 CTM、换到设备坐标（原点左下、
        # y 向上），「按 y 从大到小」才永远等于从上到下。
        ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        ctm_stack = []
        leading = 0.0
        font = None
        size = 0.0
        while True:
            lx.skip()
            if lx.i >= lx.n:
                break
            if self.d[lx.i:lx.i + 2] == b"BI":
                self._skip_inline_image(lx)
                continue
            try:
                obj = lx.parse()
            except Exception:
                lx.i += 1
                continue
            if type(obj) is bytes:                       # 裸 bytes 一定是算子
                op = obj
                try:
                    if op == b"BT":
                        tm = tlm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
                    elif op == b"q":
                        ctm_stack.append(ctm)
                    elif op == b"Q":
                        ctm = ctm_stack.pop() if ctm_stack else (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
                    elif op == b"cm":
                        if len(stack) >= 6:
                            ctm = _mul(tuple(float(v) for v in stack[-6:]), ctm)
                    elif op == b"Tf":
                        if len(stack) >= 2:
                            size = float(stack[-1]) if isinstance(stack[-1], (int, float)) else 0.0
                            nm = stack[-2]
                            font = self.fonts.get(bytes(nm).lstrip(b"/")) if isinstance(nm, Name) else None
                    elif op == b"TL":
                        if stack:
                            leading = float(stack[-1])
                    elif op in (b"Td", b"TD"):
                        if len(stack) >= 2:
                            tx, ty = float(stack[-2]), float(stack[-1])
                            if op == b"TD":
                                leading = -ty
                            tlm = _mul((1, 0, 0, 1, tx, ty), tlm)
                            tm = tlm
                    elif op == b"Tm":
                        if len(stack) >= 6:
                            tlm = tuple(float(v) for v in stack[-6:])
                            tm = tlm
                    elif op == b"T*":
                        tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
                        tm = tlm
                    elif op in (b"Tj", b"'", b'"'):
                        if op in (b"'", b'"'):
                            tlm = _mul((1, 0, 0, 1, 0, -leading), tlm)
                            tm = tlm
                        if stack and isinstance(stack[-1], Str):
                            self._emit(self._dev(tm, ctm, size), stack[-1], font)
                    elif op == b"TJ":
                        if stack and isinstance(stack[-1], list):
                            self._emit_array(self._dev(tm, ctm, size), stack[-1], font)
                except (ValueError, TypeError, IndexError):
                    pass
                stack = []
            else:
                stack.append(obj)

    @staticmethod
    def _dev(tm, ctm, size):
        """把文本矩阵乘进 CTM，得到设备矩阵；顺便把字号也换算过去。

        ``Tf`` 给的字号是文本空间的，Word 导出常常写成 209 这种大数，
        真正的视觉字号要乘上矩阵的缩放才有意义——直接拿 209 当字宽会
        把整页判成一格。
        """
        dev = _mul(tm, ctm)
        scale = math.hypot(dev[0], dev[1]) or 1.0
        return dev, size * scale

    def _emit(self, dev, s, font):
        if not s:
            return
        text = self._decode(bytes(s), font)
        if text:
            self.sink(Run(self.page, dev[0][4], dev[0][5], text, font, dev[1]))

    def _emit_array(self, dev, arr, font):
        buf = []
        for item in arr:
            if isinstance(item, Str):
                buf.append(self._decode(bytes(item), font))
            elif isinstance(item, (int, float)) and item <= -200:
                # 负的位移很大 = 排版上的一个空档（Word 导出常见）。
                # 不还原它的话「可转让大额存单」和相邻单元格会粘在一起。
                buf.append(" ")
        text = "".join(buf).strip()
        if text:
            self.sink(Run(self.page, dev[0][4], dev[0][5], text, font, dev[1]))

    def _decode(self, raw, font):
        """把字符串字节解成文本。

        有的字体没有 ToUnicode CMap——这在中文财报里很常见：正文用带 CMap 的
        Type0 中文字体，而**表格里的数字**用一份普通的 TrueType 西文字体
        （Arial 之类），它的 ``/Encoding`` 直接就是码位，压根不需要 CMap。

        原来的实现遇到没有 CMap 的字体就把整段字符丢掉。对牧原股份来说，这
        意味着资产负债表上每一个金额都不见了——只剩下左边的科目名，一张
        「货币资金 | （空白）」的表。既然表格没有数字，资产语义层也就无从
        谈起。

        所以按字体类型兜底，且**只在 ASCII 可见范围内**兜底：

        * 单字节字体（TrueType/Type1）→ Latin-1。WinAnsi、MacRoman、Standard
          三种编码在 0x20~0x7E 上完全一致，数字、逗号、小数点、负号都在其中。
          高位字节各编码互不相同，猜不得，仍旧计入 undecodable。
        * 双字节 Type0 无 CMap → 按 UTF-16BE 解释。Identity-H 的语义就是
          「两字节码即 CID」，中文子集字体的 CID 通常就是 Unicode 码位。

        兜底会记在 ``doc.synthetic_cmap`` 里：这些字符是**推出来的**，不是
        字体自己声明的，下游要能知道。
        """
        if font is None:
            self.doc.undecodable += len(raw)
            return ""
        cmap = font.get("cmap") or {}
        if not cmap:
            return self._decode_without_cmap(raw, font)
        width = font.get("bytes", 2)
        if width == 2:
            codes = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw) - 1, 2)]
        else:
            codes = list(raw)
        out = []
        for c in codes:
            ch = cmap.get(c)
            if ch is None:
                self.doc.undecodable += 1
            else:
                out.append(ch)
        # 用派生表解出来的片段单独记一笔：这些字是**按字符集定义推的**，不是
        # 字体声明的。它们比 UTF-16BE 兜底可信得多（那一套是猜码位），但仍然
        # 与「字体自己说了算」不是一回事，下游有权知道有多少文本靠这一层撑着。
        if out and font.get("derived"):
            self.doc.derived_cmap += 1
        return "".join(out)

    def _decode_without_cmap(self, raw, font):
        """字体没有 ToUnicode 时的兜底解码；见 :meth:`_decode` 的说明。"""
        if font.get("bytes", 1) == 2:
            text = []
            missed = 0
            for i in range(0, len(raw) - 1, 2):
                cp = int.from_bytes(raw[i:i + 2], "big")
                if cp == 0:
                    continue
                if _plausible_char(cp):
                    text.append(chr(cp))
                else:
                    text.append("�")
                    missed += 1
            # 解不出来的字符**必须计入 undecodable**。这一路原来只记
            # synthetic_cmap（「这一段是推出来的」），把 ``�`` 漏在计数之外，
            # 于是「一份中文财报 4 万个片段里只有 1 个含汉字」在 ``undecodable``
            # 上看起来是干净的，上层据此判定文本层可信——招商银行半年报就是
            # 这样被判成「找不到报表」的。两个问题不同，都要答：synthetic_cmap
            # 记「这一段靠兜底推的」，undecodable 记「这些字根本没解出来」。
            self.doc.undecodable += missed
            self.doc.synthetic_cmap += 1
            return "".join(text)
        out = []
        for b in raw:
            if 0x20 <= b <= 0x7E:
                out.append(chr(b))
            else:
                self.doc.undecodable += 1
        if out:
            self.doc.synthetic_cmap += 1
        return "".join(out)

    def _skip_inline_image(self, lx):
        """跳过 ``BI ... ID <二进制> EI``。

        二进制数据里完全可能包含 ``EI`` 这两个字节，所以按「前面是空白、
        后面是定界符」来找，降低误判。
        """
        m = re.compile(rb"\sEI(?=[\s\[/<(]|$)")
        lx.i += 2
        hit = m.search(self.d, lx.i)
        lx.i = (hit.end() if hit else len(self.d))


def extract_runs(data, max_pages=None):
    """把 PDF 抽成文本片段列表。

    返回 ``(runs, doc)``；``doc.undecodable`` 是无法映射到 Unicode 的字符数，
    它是「这次的文本层可不可信」的判据——上层据此决定要不要降级。
    """
    doc = Document(data)
    sink = []
    for i, page in enumerate(doc.pages(), start=1):
        if max_pages and i > max_pages:
            break
        content = doc.page_content(page)
        if not content:
            continue
        fonts = doc.page_fonts(page)
        _Content(content, fonts, i, sink.append, doc).run()
    return sink, doc


def extraction_diag(doc, runs):
    """这次抽取**可不可信**的证据。

    ``doc.undecodable`` 只说「多少个字没解出来」，那是必要不充分的：一份用
    错编码解析出来的 PDF，可能每个字都「解出来了」，只是解成了别的字。所以
    再记两样：

    * ``synthetic_cmap`` —— 靠兜底**推出来**的片段数。这些字符不是字体自己
      声明的，能读不等于读对了。
    * ``derived_cmap`` —— 靠 Adobe-GB1 的字符集定义派生出来的片段数。它比
      ``synthetic_cmap`` 那一套可靠（后者是猜码位，前者是按声明的字符集算），
      但仍不是字体自己声明的；两个数要分开，因为**修好一层不该把另一层也
      说成正常**。招商银行半年报修好后正是 ``synthetic_cmap`` 大幅下降、
      ``derived_cmap`` 从 0 涨起来，两份数合起来才不会把「修好了」读成
      「什么都没变」。
    * ``cjk_runs`` / ``runs`` —— 含汉字的片段占比。**这一条最灵**：中文财报
      里汉字片段占绝大多数，比例塌到接近零，文本层一定是坏的，而这时
      ``undecodable`` 可能还是一个小数字。

    这些数字要落进 rows 缓存，因为它正是「抽不出报表」时**真正的失败原因**。
    「找不到报表：合并资产负债表」在文本层已坏的情况下是一句误导——那张表
    就在那儿，只是它的字没解出来。
    """
    cjk = 0
    for r in runs:
        for ch in r.text:
            if 0x4E00 <= ord(ch) <= 0x9FFF:
                cjk += 1
                break
    return {"undecodable": doc.undecodable, "synthetic_cmap": doc.synthetic_cmap,
            "derived_cmap": doc.derived_cmap, "cjk_runs": cjk, "runs": len(runs)}


# --------------------------------------------------------------------------- #
# 行列还原
# --------------------------------------------------------------------------- #
def to_rows(runs, y_tol=2.0, gap_ratio=1.05, x_gap_floor=3.0):
    """把片段按坐标还原成「行」。

    返回 ``[{page, y, cells:[{x, text}]}]``。同一行内按 x 排序；相邻片段的
    间距超过阈值就当成两个单元格——财报表格的一行就是靠这个切开的。

    阈值 = **该处字号 × 1.05**，跟着字号走而不是写死磅数。这不是拍脑袋定的，
    是量出来的：这些财报把每个汉字拆成独立片段，实测同一格内的间距是
    0.05~5.4 磅（``可转让大额存单`` 六个字之间接近 0，``1、货币资金`` 里
    「、」和「货」之间 5.4），而列与列之间是 16.5~180 磅。正文 10.45 磅时
    门槛约 11 磅，正好落在 5.4 和 16.5 中间。

    一开始用的是 1.8 倍，结果窄列表格里 ``5,936,426.16`` 和 ``7,420,597.70``
    被拼成了一个数——那一处的列间距恰好只有 16.5 磅，卡进了宽松的阈值里。
    所以这个系数不能往宽了放。

    排序用 ``-y``：x/y 已经是设备坐标（原点左下、y 向上），所以 y 越大越靠上，
    取负号之后「从大到小」就等于从上往下读。
    """
    rows = []
    for r in sorted(runs, key=lambda r: (r.page, -r.y, r.x)):
        row = None
        for cand in reversed(rows[-8:]):
            if cand["page"] == r.page and abs(cand["y"] - r.y) <= y_tol:
                row = cand
                break
        if row is None:
            row = {"page": r.page, "y": r.y, "cells": []}
            rows.append(row)
        row["cells"].append({"x": r.x, "text": r.text, "size": r.size})
    for row in rows:
        row["cells"].sort(key=lambda c: c["x"])
        merged = []
        for cell in row["cells"]:
            if merged:
                prev = merged[-1]
                limit = max(x_gap_floor, gap_ratio * (prev["size"] or 0.0))
                prev_end = prev["x"] + _width_estimate(prev["text"], prev["size"])
                if cell["x"] - prev_end <= limit:
                    prev["text"] += cell["text"]
                    continue
            merged.append(dict(cell))
        row["cells"] = merged
        for cell in row["cells"]:
            cell.pop("size", None)
    return rows


_FULLWIDTH = re.compile(r"[ᄀ-ᅟ⺀-꓏가-힣豈-﫿"
                        r"︰-﹏＀-｠￠-￦]")


def _width_estimate(text, size):
    """按字宽估算一段文本的排版宽度（全角 1em、半角 0.5em）。"""
    if not size:
        return 0.0
    wide = len(_FULLWIDTH.findall(text))
    return (wide + (len(text) - wide) * 0.5) * size




def rows_to_text(rows):
    """把行还原成便于阅读/正则的文本，单元格之间用制表符分隔。"""
    out = []
    for row in rows:
        out.append("\t".join(c["text"] for c in row["cells"]))
    return "\n".join(out)
