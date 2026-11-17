# -*- coding: utf-8 -*-
"""011 报告 PDF 渲染。

用 reportlab 的 ``STSong-Light`` CID 字体，不依赖任何外部字体文件（容器里没中文字体也能出中文），
也不联网。输入是 11 号算子内部统一的 ``model``，与 Markdown / HTML 同源，保证三种格式数字一致。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

# 文档里出现的 [1] / **强调** 之类标记在 PDF 里要压成纯文本
_MD_NOISE = re.compile(r"(\*\*|__|\*|`)")

FONT = "STSong-Light"
FONT_BOLD = "STSong-Light"


def _esc(text: Any) -> str:
    raw = str(text if text is not None else "")
    raw = _MD_NOISE.sub("", raw)
    return (raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _long(text: Any, limit: int = 2000) -> str:
    raw = str(text if text is not None else "")
    return raw if len(raw) <= limit else raw[: limit - 1] + "…"


def _register_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    if FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(FONT))
    # CID 字体没有独立粗体：把 bold 映射回同一字体，`<b>` 标签才不会报错
    pdfmetrics.registerFontFamily(FONT, normal=FONT, bold=FONT, italic=FONT, boldItalic=FONT)
    return FONT


def _styles():
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle

    ink = colors.HexColor("#1f2937")
    muted = colors.HexColor("#667085")
    label = colors.HexColor("#475467")

    def ps(name, size, leading, **extra):
        kwargs = dict(fontName=FONT, wordWrap="CJK", textColor=ink,
                      fontSize=size, leading=leading)
        kwargs.update(extra)
        return ParagraphStyle(name, **kwargs)

    return {
        "title": ps("title", 17, 24, spaceAfter=6),
        "h2": ps("h2", 13, 19, spaceBefore=12, spaceAfter=5, textColor=colors.HexColor("#4169f6")),
        "h3": ps("h3", 11, 16, spaceBefore=6, spaceAfter=3),
        "body": ps("body", 9.6, 15, spaceAfter=3),
        "cell": ps("cell", 8.4, 12),
        "cellu": ps("cellu", 8.4, 12, textColor=muted),
        "label": ps("label", 9.0, 14, textColor=label),
        "meta": ps("meta", 9.0, 14, textColor=label),
        "note": ps("note", 8.4, 12.5, textColor=muted),
    }


def _kv_table(rows: List[Dict[str, Any]], st, width: float):
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle

    data = [[Paragraph("<b>%s</b>" % _esc(r.get("label")), st["label"]),
             Paragraph(_esc(_long(r.get("text"))), st["body"])] for r in rows]
    table = Table(data, colWidths=[34 * mm, width - 34 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e4e7ec")),
    ]))
    return table


def _data_table(block: Dict[str, Any], st, width: float):
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle

    columns = block.get("columns") or []
    if not columns:
        return None
    head = [Paragraph(_esc(c.get("text", "")), st["cellu"]) for c in columns]
    body = []
    for row in block.get("rows") or []:
        cells = []
        for cell in row:
            text = _esc(_long(cell.get("text", ""), 400))
            cells.append(Paragraph(text, st["cell"]))
        body.append(cells)
    n = len(columns)
    # 数值列窄一点、文字列宽一点，避免百分比列被挤成两行
    weights = []
    for col in columns:
        weights.append(0.62 if col.get("num") else 1.0)
    total = sum(weights) or float(n)
    col_widths = [width * w / total for w in weights]

    table = Table([head] + body, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f7f9fd")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e4e7ec")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _kpi_table(block: Dict[str, Any], st, width: float):
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle

    head = [Paragraph(_esc(x), st["cellu"]) for x in ("指标", "本模型", "与对比对象", "对比对象值")]
    body = []
    for item in block.get("items") or []:
        body.append(
            [
                Paragraph(_esc(item.get("name", "")), st["cell"]),
                Paragraph("<b>%s</b>" % _esc(item.get("value", "")), st["cell"]),
                Paragraph(_esc(item.get("diff", "")), st["cell"]),
                Paragraph(_esc(item.get("ref", "")), st["cell"]),
            ]
        )
    table = Table([head] + body, colWidths=[width * 0.26, width * 0.18, width * 0.30, width * 0.26],
                  repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f7f9fd")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e4e7ec")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return table


def _blocks(blocks: List[Dict[str, Any]], st, width: float) -> List[Any]:
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    flow: List[Any] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "rows":
            flow.append(_kv_table(block["rows"], st, width))
        elif kind == "kpi":
            flow.append(_kpi_table(block, st, width))
        elif kind == "table":
            table = _data_table(block, st, width)
            if table is not None:
                flow.append(table)
            if block.get("note"):
                flow.append(Paragraph("注：%s" % _esc(block["note"]), st["note"]))
        elif kind == "chips":
            flow.append(Paragraph(" · ".join(_esc(x) for x in block["items"]), st["body"]))
        elif kind == "list":
            for idx, item in enumerate(block["items"], start=1):
                flow.append(Paragraph("%d. %s" % (idx, _esc(item)), st["body"]))
        elif kind == "note":
            flow.append(Paragraph(_esc(block["text"]), st["note"]))
        elif kind == "subhead":
            flow.append(Paragraph(_esc(block["text"]), st["h3"]))
        elif kind == "chain":
            steps = []
            for step in block["steps"]:
                label = "%s" % _esc(step["label"]) if step.get("label") else ""
                steps.append("%s%s" % (label, _esc(step["text"])))
            flow.append(Paragraph(" → ".join(steps), st["body"]))
        elif kind == "unavailable":
            flow.append(Paragraph(_esc(block["text"]), st["note"]))
        elif kind == "suggestions":
            for item in block["items"]:
                flow.append(Paragraph("建议 %s：%s" % (item["no"], _esc(item["title"])), st["h3"]))
                flow.append(_kv_table(item["rows"], st, width))
                flow.append(Spacer(1, 3))
        flow.append(Spacer(1, 4))
    return flow


def render(model: Dict[str, Any], cfg: Dict[str, Any], report_no: str = "") -> bytes:
    """把报告 model 渲染成 PDF 字节流。"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import (BaseDocTemplate, Frame, PageTemplate, Paragraph,
                                    Spacer)

    _register_font()
    st = _styles()

    margin_x, margin_y = 18 * mm, 16 * mm
    page_w, page_h = A4
    width = page_w - margin_x * 2

    def _decorate(canvas, doc):
        canvas.saveState()
        canvas.setFont(FONT, 7.5)
        canvas.setFillColor(colors.HexColor("#98a2b3"))
        canvas.drawString(margin_x, 9 * mm, _long(report_no or model.get("report_no", ""), 60))
        canvas.drawRightString(page_w - margin_x, 9 * mm, "第 %d 页" % doc.page)
        canvas.setStrokeColor(colors.HexColor("#e4e7ec"))
        canvas.line(margin_x, 12 * mm, page_w - margin_x, 12 * mm)
        canvas.restoreState()

    from io import BytesIO

    buf = BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=margin_x, rightMargin=margin_x,
                          topMargin=margin_y, bottomMargin=margin_y,
                          title=_long(model.get("title", "语音加扰报告"), 120),
                          author="语音加扰算子")
    frame = Frame(margin_x, margin_y, width, page_h - margin_y * 2, id="body")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_decorate)])

    flow: List[Any] = [Paragraph(_esc(model.get("title", "")), st["title"])]
    for label, text in model.get("meta_rows") or []:
        flow.append(Paragraph("<b>%s</b>：%s" % (_esc(label), _esc(_long(text))), st["meta"]))
    flow.append(Spacer(1, 6))

    for section in model.get("sections") or []:
        flow.append(Paragraph("%s. %s" % (section.get("no"), _esc(section.get("title"))), st["h2"]))
        flow.extend(_blocks(section.get("blocks") or [], st, width))

    flow.append(Spacer(1, 8))
    flow.append(Paragraph(
        "本报告由规则引擎自动生成：所有数字来自 007/008/009 输入或由输入直接换算，"
        "未接大模型、未做二次推算。", st["note"]))

    doc.build(flow)
    return buf.getvalue()
