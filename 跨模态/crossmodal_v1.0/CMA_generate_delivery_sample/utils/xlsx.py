"""★Excel 写出器：只用标准库生成**真实 OOXML（.xlsx）**工作表。

表1 #26「导出 Excel」要求交付样例清单是验收人员可直接打开的 Excel：
- 只用 Python 标准库 ``zipfile`` 组装 OOXML 包（``[Content_Types].xml`` /
  ``xl/workbook.xml`` / ``xl/worksheets/*.xml`` / ``xl/styles.xml`` …），
  因此本算子运行期**不引入任何第三方 Excel 依赖**；
- 输出为确定性字节：同一输入 + 同一时间戳 → 完全相同的文件（可复现、可校验 sha256）；
- 单元格语义与 Excel 一致：``None`` → 空单元格，``bool`` → 布尔，``int``/``float``
  → 数字（右对齐、可参与统计），``str`` → 内联字符串（保留前导/尾随空格）；
- 边界：单元格文本上限 ``EXCEL_CELL_MAX_CHARS`` 字符，超出截断并追加省略号；
  XML 非法控制字符会被剔除；表名长度、非法字符与重名一律显式失败。

诚实边界：本模块只负责“把给定的行写成 Excel”，不产生任何业务内容、
不填任何默认值、不做任何分数或结论的加工。
"""
from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "EXCEL_CELL_MAX_CHARS",
    "EXCEL_MAX_SHEET_NAME",
    "XlsxError",
    "build_workbook",
    "column_letter",
    "write_workbook",
]

EXCEL_CELL_MAX_CHARS = 32767
EXCEL_MAX_SHEET_NAME = 31

_ILLEGAL_SHEET_CHARS = set("[]:*?/\\")
#: OOXML 1.0 不允许的控制字符（保留 \t \n \r）
_FORBIDDEN_CONTROL = set(range(0x00, 0x20)) - {0x09, 0x0A, 0x0D}

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

_CT_WORKBOOK = ("application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet.main+xml")
_CT_WORKSHEET = ("application/vnd.openxmlformats-officedocument."
                 "spreadsheetml.worksheet+xml")
_CT_STYLES = "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"
_CT_CORE = "application/vnd.openxmlformats-package.core-properties+xml"
_CT_APP = "application/vnd.openxmlformats-officedocument.extended-properties+xml"

_XML_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

#: xml 字面量（单引号包裹，避免与内容里的英文引号冲突）
_CORE_XML = (
    '{h}\n<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/'
    'metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    '<dc:title>{title}</dc:title><dc:creator>{creator}</dc:creator>'
    '<cp:lastModifiedBy>{creator}</cp:lastModifiedBy>'
    '<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
    '<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified>'
    '</cp:coreProperties>')

_APP_XML = (
    '{h}\n<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/'
    'extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/'
    '2006/docPropsVTypes"><Application>{app}</Application>'
    '<DocSecurity>0</DocSecurity><ScaleCrop>false</ScaleCrop>'
    '<SharedDoc>false</SharedDoc><HyperlinksChanged>false</HyperlinksChanged>'
    '<AppVersion>16.0300</AppVersion></Properties>')

_STYLES_XML = (
    '{h}\n<styleSheet xmlns="{ns}">'
    '<fonts count="2">'
    '<font><sz val="11"/><color rgb="FF1F1F1F"/><name val="Calibri"/><family val="2"/></font>'
    '<font><b/><sz val="11"/><color rgb="FF1F1F1F"/><name val="Calibri"/><family val="2"/></font>'
    '</fonts>'
    '<fills count="3">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFDCE6F1"/>'
    '<bgColor indexed="64"/></patternFill></fill>'
    '</fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" '
    'applyFill="1" applyAlignment="1" applyBorder="1"><alignment vertical="center" '
    'horizontal="center"/></xf>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '<dxfs count="0"/><tableStyles count="0"/></styleSheet>')


class XlsxError(ValueError):
    """工作簿/工作表/单元格参数不合法。"""


# --------------------------------------------------------------------------- #
# 校验与小工具
# --------------------------------------------------------------------------- #

def column_letter(index: int) -> str:
    """1 → A、26 → Z、27 → AA。"""
    if not isinstance(index, int) or index < 1:
        raise XlsxError("列序号必须是从 1 开始的正整数：%r" % (index,))
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _check_sheet_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise XlsxError("工作表名不能为空")
    candidate = name.strip()
    if len(candidate) > EXCEL_MAX_SHEET_NAME:
        raise XlsxError("工作表名不能超过 %d 个字符：%r"
                        % (EXCEL_MAX_SHEET_NAME, name))
    illegal = sorted(set(candidate) & _ILLEGAL_SHEET_CHARS)
    if illegal:
        raise XlsxError("工作表名包含非法字符 %s：%r"
                        % ("".join(illegal), name))
    if candidate.startswith("'") or candidate.endswith("'"):
        raise XlsxError("工作表名不能以单引号开头或结尾：%r" % name)
    return candidate


def _escape_xml(text: str, *, attribute: bool = False) -> str:
    escaped = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    if attribute:
        escaped = escaped.replace('"', "&quot;")
    return escaped


def _clean_text(value: str) -> str:
    """剔除 OOXML 非法控制字符；空文本保留（不清空业务内容）。"""
    cleaned = "".join(ch for ch in value if ord(ch) not in _FORBIDDEN_CONTROL)
    if len(cleaned) > EXCEL_CELL_MAX_CHARS:
        cleaned = cleaned[:EXCEL_CELL_MAX_CHARS - 1] + "…"
    return cleaned


def _cell_xml(reference: str, value: Any, *, style: Optional[int] = None) -> str:
    style_attr = ' s="%d"' % style if style else ""
    if value is None:
        return ""
    if isinstance(value, bool):                       # bool 必须先于 int 判断
        return '<c r="%s"%s t="b"><v>%d</v></c>' % (reference, style_attr, int(value))
    if isinstance(value, int):
        return '<c r="%s"%s><v>%d</v></c>' % (reference, style_attr, value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):   # NaN/Inf
            text = _clean_text(str(value))
            return ('<c r="%s"%s t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                    % (reference, style_attr, _escape_xml(text)))
        return '<c r="%s"%s><v>%s</v></c>' % (reference, style_attr, repr(value))
    text = _clean_text(str(value))
    return ('<c r="%s"%s t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
            % (reference, style_attr, _escape_xml(text)))


# --------------------------------------------------------------------------- #
# 工作表与工作簿
# --------------------------------------------------------------------------- #

def _sheet_xml(sheet: Dict[str, Any], index: int) -> Tuple[str, int]:
    name = sheet.get("name")
    _check_sheet_name(name)
    columns = sheet.get("columns") or []
    if not isinstance(columns, list) or not columns:
        raise XlsxError("工作表 %r 至少需要一列" % name)
    headers: List[str] = []
    for position, column in enumerate(columns, start=1):
        if not isinstance(column, dict):
            raise XlsxError("工作表 %r 第 %d 列必须是对象" % (name, position))
        header = column.get("header")
        if not isinstance(header, str) or not header:
            raise XlsxError("工作表 %r 第 %d 列缺少表头" % (name, position))
        headers.append(header)

    rows = sheet.get("rows") or []
    if not isinstance(rows, list):
        raise XlsxError("工作表 %r 的 rows 必须是数组" % name)

    last_column = column_letter(len(headers))
    parts: List[str] = [_XML_HEADER,
                        '<worksheet xmlns="%s" xmlns:r="%s">' % (_MAIN_NS, _REL_NS),
                        '<dimension ref="A1:%s%d"/>' % (last_column, len(rows) + 1)]
    if sheet.get("freeze_header"):
        parts.append('<sheetViews><sheetView workbookViewId="0">'
                     '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" '
                     'state="frozen"/></sheetView></sheetViews>')
    else:
        parts.append('<sheetViews><sheetView workbookViewId="0"/></sheetViews>')
    parts.append('<sheetFormatPr defaultRowHeight="15"/>')

    widths = []
    for position, column in enumerate(columns, start=1):
        width = column.get("width")
        if width is None:
            continue
        try:
            numeric = float(width)
        except (TypeError, ValueError):
            raise XlsxError("工作表 %r 第 %d 列宽度必须是数字" % (name, position))
        if numeric <= 0:
            raise XlsxError("工作表 %r 第 %d 列宽度必须为正数" % (name, position))
        widths.append('<col min="%d" max="%d" width="%s" customWidth="1"/>'
                      % (position, position, _trim_number(numeric)))
    if widths:
        parts.append("<cols>%s</cols>" % "".join(widths))

    parts.append("<sheetData>")
    parts.append('<row r="1" spans="1:%d">%s</row>'
                 % (len(headers),
                    "".join(_cell_xml("%s1" % column_letter(position), header, style=1)
                            for position, header in enumerate(headers, start=1))))
    for offset, row in enumerate(rows):
        row_number = offset + 2
        if not isinstance(row, (list, tuple)):
            raise XlsxError("工作表 %r 第 %d 行必须是数组" % (name, row_number))
        if len(row) > len(headers):
            raise XlsxError("工作表 %r 第 %d 行有 %d 个单元格，超过 %d 列"
                            % (name, row_number, len(row), len(headers)))
        cells = "".join(_cell_xml("%s%d" % (column_letter(position), row_number), value)
                        for position, value in enumerate(row, start=1))
        parts.append('<row r="%d" spans="1:%d">%s</row>'
                     % (row_number, len(headers), cells))
    parts.append("</sheetData>")

    if sheet.get("autofilter"):
        parts.append('<autoFilter ref="A1:%s1"/>' % last_column)
    parts.append("</worksheet>")
    return "".join(parts), len(rows)


def _trim_number(value: float) -> str:
    text = ("%.4f" % value).rstrip("0").rstrip(".")
    return text or "0"


def build_workbook(sheets: Sequence[Dict[str, Any]],
                   timestamp: Optional[Sequence[int]] = None) -> bytes:
    """把工作表定义序列组装成 ``.xlsx`` 字节（真实 OOXML）。"""
    if not isinstance(sheets, (list, tuple)) or not sheets:
        raise XlsxError("工作簿至少需要一个工作表")
    stamp = _normalize_timestamp(timestamp)

    seen_names: Dict[str, int] = {}
    sheet_xmls: List[Tuple[str, str]] = []
    data_rows = 0
    for index, sheet in enumerate(sheets, start=1):
        if not isinstance(sheet, dict):
            raise XlsxError("工作表定义必须是对象")
        name = _check_sheet_name(sheet.get("name"))
        key = name.lower()
        if key in seen_names:
            raise XlsxError("工作表名重复：%r" % name)
        seen_names[key] = index
        xml, rows = _sheet_xml(sheet, index)
        sheet_xmls.append((name, xml))
        data_rows += rows

    members: List[Tuple[str, bytes]] = []

    overrides = ["<Override PartName=\"/xl/workbook.xml\" ContentType=\"%s\"/>" % _CT_WORKBOOK,
                 "<Override PartName=\"/xl/styles.xml\" ContentType=\"%s\"/>" % _CT_STYLES,
                 "<Override PartName=\"/docProps/core.xml\" ContentType=\"%s\"/>" % _CT_CORE,
                 "<Override PartName=\"/docProps/app.xml\" ContentType=\"%s\"/>" % _CT_APP]
    for index in range(1, len(sheet_xmls) + 1):
        overrides.append("<Override PartName=\"/xl/worksheets/sheet%d.xml\" "
                         "ContentType=\"%s\"/>" % (index, _CT_WORKSHEET))
    members.append(("[Content_Types].xml", (
        '%s\n<Types xmlns="%s"><Default Extension="rels" ContentType="application/'
        'vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" '
        'ContentType="application/xml"/>%s</Types>'
        % (_XML_HEADER, _CT_NS, "".join(overrides))).encode("utf-8")))

    members.append(("_rels/.rels", (
        '%s\n<Relationships xmlns="%s">'
        '<Relationship Id="rId1" Type="%s/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/'
        'relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="%s/extended-properties" Target="docProps/app.xml"/>'
        '</Relationships>' % (_XML_HEADER, _PKG_REL_NS, _REL_NS, _REL_NS))
        .encode("utf-8")))

    member_core = (_CORE_XML.format(h=_XML_HEADER, title=_escape_xml("交付样例清单"),
                                    creator=_escape_xml("operator_26"),
                                    stamp="%04d-%02d-%02dT%02d:%02d:%02dZ" % stamp)
                   .encode("utf-8"))
    members.append(("docProps/core.xml", member_core))
    members.append(("docProps/app.xml",
                    _APP_XML.format(h=_XML_HEADER, app=_escape_xml("生成交付样例")).encode("utf-8")))

    sheet_entries = "".join(
        '<sheet name="%s" sheetId="%d" r:id="rId%d"/>'
        % (_escape_xml(name, attribute=True), index, index)
        for index, (name, _xml) in enumerate(sheet_xmls, start=1))
    members.append(("xl/workbook.xml", (
        '%s\n<workbook xmlns="%s" xmlns:r="%s"><bookViews>'
        '<workbookView activeTab="0"/></bookViews><sheets>%s</sheets>'
        '</workbook>' % (_XML_HEADER, _MAIN_NS, _REL_NS, sheet_entries)).encode("utf-8")))

    rels = "".join('<Relationship Id="rId%d" Type="%s/worksheet" '
                   'Target="worksheets/sheet%d.xml"/>' % (index, _REL_NS, index)
                   for index in range(1, len(sheet_xmls) + 1))
    rels += ('<Relationship Id="rIdStyles" Type="%s/styles" Target="styles.xml"/>'
             % _REL_NS)
    members.append(("xl/_rels/workbook.xml.rels", (
        '%s\n<Relationships xmlns="%s">%s</Relationships>'
        % (_XML_HEADER, _PKG_REL_NS, rels)).encode("utf-8")))

    members.append(("xl/styles.xml",
                    _STYLES_XML.format(h=_XML_HEADER, ns=_MAIN_NS).encode("utf-8")))

    for index, (_name, xml) in enumerate(sheet_xmls, start=1):
        members.append(("xl/worksheets/sheet%d.xml" % index, xml.encode("utf-8")))

    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as bundle:
        for name, payload in members:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            bundle.writestr(info, payload)
    return buffer.getvalue()


def _normalize_timestamp(timestamp: Optional[Sequence[int]]
                         ) -> Tuple[int, int, int, int, int, int]:
    if timestamp is None:
        return (2026, 1, 1, 0, 0, 0)
    values = [int(item) for item in timestamp]
    if len(values) != 6:
        raise XlsxError("时间戳必须是 6 元组 (year, month, day, hour, minute, second)")
    year, month, day, hour, minute, second = values
    if not (1980 <= year <= 2107):
        raise XlsxError("ZIP 时间戳年份必须在 1980–2107 之间")
    return (year, month, day, hour, minute, second)


def write_workbook(path: Path,
                   sheets: Sequence[Dict[str, Any]],
                   timestamp: Optional[Sequence[int]] = None) -> Dict[str, Any]:
    """写入 ``.xlsx`` 并返回真实统计（成员数、数据行数、字节与 sha256）。"""
    payload = build_workbook(sheets, timestamp=timestamp)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".partial")
    try:
        with open(temp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:                            # pragma: no cover
                pass
    return {"sheets": [str(item.get("name")) for item in sheets],
            "data_rows": sum(len(item.get("rows") or []) for item in sheets),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest()}
