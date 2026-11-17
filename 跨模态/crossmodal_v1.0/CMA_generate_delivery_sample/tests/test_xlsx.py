"""XLSX 写出器测试：真实 OOXML 结构、可被 openpyxl 读取、确定性、边界与转义。"""
from __future__ import annotations

import io
import zipfile

import pytest

from utils import xlsx

openpyxl = pytest.importorskip("openpyxl")


SHEETS = [
    {"name": "交付样例",
     "columns": [{"header": "序号", "width": 6}, {"header": "样本ID", "width": 16},
                 {"header": "模态", "width": 10}, {"header": "描述摘要", "width": 40},
                 {"header": "标注结论", "width": 24}, {"header": "评分结论", "width": 32}],
     "rows": [[1, "cm-0001", "图文", "一只橘猫坐在窗台上", "对齐（自动，未人工确认）",
               "0.410（自动对齐；CLIP-ViT/B-32）"],
              [2, "cm-0002", "音文", "一段路况播报", "对齐（自动，未人工确认）", "未提供"]],
     "freeze_header": True, "autofilter": True},
    {"name": "样例报告",
     "columns": [{"header": "项目", "width": 24}, {"header": "取值", "width": 60}],
     "rows": [["来源包状态", "可交付"], ["抽样条数", 2]],
     "freeze_header": True, "autofilter": False},
]


def test_workbook_is_a_real_zip_with_ooxml_members():
    payload = xlsx.build_workbook(SHEETS)
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        names = set(bundle.namelist())
    for required in ("[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                     "xl/_rels/workbook.xml.rels", "xl/styles.xml",
                     "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml"):
        assert required in names, required


def test_openpyxl_reads_headers_and_rows():
    payload = xlsx.build_workbook(SHEETS)
    book = openpyxl.load_workbook(io.BytesIO(payload))
    assert book.sheetnames == ["交付样例", "样例报告"]
    sheet = book["交付样例"]
    assert [cell.value for cell in sheet[1]] == ["序号", "样本ID", "模态", "描述摘要",
                                                 "标注结论", "评分结论"]
    assert sheet["B2"].value == "cm-0001"
    assert sheet["A2"].value == 1
    assert sheet["D3"].value == "一段路况播报"
    report = book["样例报告"]
    assert report["A2"].value == "来源包状态"
    assert report["B3"].value == 2
    assert sheet.freeze_panes == "A2"
    assert report.freeze_panes == "A2"


def test_openpyxl_sees_header_autofilter():
    payload = xlsx.build_workbook(SHEETS)
    book = openpyxl.load_workbook(io.BytesIO(payload))
    assert book["交付样例"].auto_filter.ref == "A1:F1"
    assert book["样例报告"].auto_filter.ref is None


def test_output_is_deterministic_for_same_input():
    first = xlsx.build_workbook(SHEETS, timestamp=(2026, 10, 8, 12, 0, 0))
    second = xlsx.build_workbook(SHEETS, timestamp=(2026, 10, 8, 12, 0, 0))
    assert first == second


def test_utf8_chinese_round_trips():
    payload = xlsx.build_workbook([{"name": "中文表", "columns": [{"header": "名称"}],
                                    "rows": [["中文内容：图文样例"], ["emoji ✅"]]}])
    book = openpyxl.load_workbook(io.BytesIO(payload))
    assert book["中文表"]["A2"].value == "中文内容：图文样例"
    assert book["中文表"]["A3"].value == "emoji ✅"


def test_special_characters_are_escaped():
    tricky = "<tag> & \"quote\" 'apos' \n换行\t制表"
    payload = xlsx.build_workbook([{"name": "转义", "columns": [{"header": "值"}],
                                    "rows": [[tricky]]}])
    book = openpyxl.load_workbook(io.BytesIO(payload))
    assert book["转义"]["A2"].value == tricky


def test_control_characters_are_stripped_not_fatal():
    payload = xlsx.build_workbook([{"name": "控制", "columns": [{"header": "值"}],
                                    "rows": [["a\x00b\x01c"]]}])
    book = openpyxl.load_workbook(io.BytesIO(payload))
    assert book["控制"]["A2"].value == "abc"


def test_cell_longer_than_excel_limit_is_truncated():
    payload = xlsx.build_workbook([{"name": "长文本", "columns": [{"header": "值"}],
                                    "rows": [["x" * 40000]]}])
    book = openpyxl.load_workbook(io.BytesIO(payload))
    value = book["长文本"]["A2"].value
    assert len(value) == xlsx.EXCEL_CELL_MAX_CHARS
    assert value.endswith("…")


def test_booleans_and_floats_keep_native_types():
    payload = xlsx.build_workbook([{"name": "类型", "columns": [{"header": "值"}],
                                    "rows": [[True], [False], [0.28], [3]]}])
    book = openpyxl.load_workbook(io.BytesIO(payload))
    assert book["类型"]["A2"].value is True
    assert book["类型"]["A3"].value is False
    assert book["类型"]["A4"].value == 0.28
    assert book["类型"]["A5"].value == 3


def test_none_values_become_empty_cells():
    payload = xlsx.build_workbook([{"name": "空值",
                                    "columns": [{"header": "a"}, {"header": "b"}],
                                    "rows": [[None, "b"]]}])
    book = openpyxl.load_workbook(io.BytesIO(payload))
    assert book["空值"]["A2"].value is None
    assert book["空值"]["B2"].value == "b"


def test_header_only_sheet_is_valid():
    payload = xlsx.build_workbook([{"name": "仅表头",
                                    "columns": [{"header": "样本ID"}], "rows": []}])
    book = openpyxl.load_workbook(io.BytesIO(payload))
    assert book["仅表头"]["A1"].value == "样本ID"
    assert book["仅表头"].max_row == 1


def test_row_with_more_cells_than_columns_is_rejected():
    with pytest.raises(xlsx.XlsxError):
        xlsx.build_workbook([{"name": "越界", "columns": [{"header": "a"}],
                              "rows": [["a", "b"]]}])


def test_invalid_sheet_name_is_rejected():
    with pytest.raises(xlsx.XlsxError):
        xlsx.build_workbook([{"name": "非法[名称]", "columns": [{"header": "a"}],
                              "rows": []}])


def test_sheet_name_longer_than_31_is_rejected():
    with pytest.raises(xlsx.XlsxError):
        xlsx.build_workbook([{"name": "长" * 32, "columns": [{"header": "a"}],
                              "rows": []}])


def test_duplicate_sheet_names_are_rejected():
    with pytest.raises(xlsx.XlsxError):
        xlsx.build_workbook([{"name": "同名", "columns": [{"header": "a"}], "rows": []},
                             {"name": "同名", "columns": [{"header": "a"}], "rows": []}])


def test_empty_workbook_is_rejected():
    with pytest.raises(xlsx.XlsxError):
        xlsx.build_workbook([])


def test_column_letter_helper():
    assert xlsx.column_letter(1) == "A"
    assert xlsx.column_letter(26) == "Z"
    assert xlsx.column_letter(27) == "AA"
    assert xlsx.column_letter(52) == "AZ"
    assert xlsx.column_letter(53) == "BA"


def test_write_workbook_reports_real_statistics(tmp_path):
    target = tmp_path / "out.xlsx"
    report = xlsx.write_workbook(target, SHEETS, timestamp=(2026, 10, 8, 12, 0, 0))
    assert target.is_file()
    assert report["sheets"] == ["交付样例", "样例报告"]
    assert report["data_rows"] == 4
    assert report["bytes"] == target.stat().st_size
    assert report["sha256"]
    book = openpyxl.load_workbook(target)
    assert book["交付样例"]["B2"].value == "cm-0001"
