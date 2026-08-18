"""
hwpx_anonymize.py — HWPX 문서의 표에서 특정 셀 값을 지운다.

용도: 나이스 업로드용으로 '지도교사' 칸을 비우기.

두 가지 방식을 지원한다.
  1) 라벨 기준 (권장) — 머리글 셀에서 '지도교사'를 찾아 바로 아래 칸을 비운다.
     표의 열 순서가 과목마다 조금 달라도 안전하다.
  2) 위치 기준 — (섹션, 표 번호, 행, 열) 또는 '표의 N번째 칸'으로 직접 지정.

셀을 지울 때 셀 자체나 문단은 남기고 글자만 없앤다. 글자 모양(charPrIDRef)도
그대로 두므로 나중에 손으로 이름을 다시 넣어도 서식이 유지된다.
"""

from __future__ import annotations

import copy
import io
import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree

HP = "{http://www.hancom.co.kr/hwpml/2011/paragraph}"
SECTION_RE = re.compile(r"^Contents/section(\d+)\.xml$")

DEFAULT_LABELS = ("지도교사", "담당교사", "지도 교사", "담당 교사")


class AnonymizeError(Exception):
    pass


# ---------------------------------------------------------------- 조회
@dataclass
class Cell:
    row: int
    col: int
    row_span: int
    col_span: int
    text: str
    order: int          # 표 안에서 몇 번째 칸인지 (1부터)
    _el: etree._Element = field(repr=False, default=None)


@dataclass
class Table:
    section: int
    index: int          # 그 섹션에서 몇 번째 표인지 (0부터)
    rows: int
    cols: int
    cells: list[Cell]

    def grid(self) -> list[list[str]]:
        g = [["" for _ in range(self.cols)] for _ in range(self.rows)]
        for c in self.cells:
            if c.row < self.rows and c.col < self.cols:
                g[c.row][c.col] = c.text
        return g

    def at(self, row: int, col: int) -> Cell | None:
        for c in self.cells:
            if c.row == row and c.col == col:
                return c
        return None


def _cell_text(tc: etree._Element) -> str:
    parts = [t.text for t in tc.iter(HP + "t") if t.text]
    return "".join(parts).strip()


def _scan_table(tbl: etree._Element, section: int, index: int) -> Table:
    cells: list[Cell] = []
    order = 0
    for tr in tbl.findall(HP + "tr"):
        for tc in tr.findall(HP + "tc"):
            order += 1
            addr = tc.find(HP + "cellAddr")
            span = tc.find(HP + "cellSpan")
            row = int(addr.get("rowAddr", 0)) if addr is not None else 0
            col = int(addr.get("colAddr", 0)) if addr is not None else 0
            rs = int(span.get("rowSpan", 1)) if span is not None else 1
            cs = int(span.get("colSpan", 1)) if span is not None else 1
            cells.append(Cell(row, col, rs, cs, _cell_text(tc), order, tc))
    rows = max((c.row + c.row_span for c in cells), default=0)
    cols = max((c.col + c.col_span for c in cells), default=0)
    return Table(section, index, rows, cols, cells)


def _open(blob: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            return {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
    except zipfile.BadZipFile as exc:
        raise AnonymizeError("HWPX(ZIP)로 열리지 않는 파일입니다.") from exc


def _section_names(parts: dict[str, bytes]) -> list[str]:
    return sorted(
        (n for n in parts if SECTION_RE.match(n)),
        key=lambda n: int(SECTION_RE.match(n).group(1)),
    )


def read_tables(blob: bytes, section: int | None = None) -> list[Table]:
    """문서 안의 표를 훑어 그리드로 돌려준다 (미리보기·확인용)."""
    parts = _open(blob)
    out: list[Table] = []
    for si, name in enumerate(_section_names(parts)):
        if section is not None and si != section:
            continue
        root = etree.fromstring(parts[name])
        for ti, tbl in enumerate(root.iter(HP + "tbl")):
            out.append(_scan_table(tbl, si, ti))
    return out


# ---------------------------------------------------------------- 삭제
def _blank_cell(tc: etree._Element) -> None:
    """셀의 글자만 지우고 문단·글자모양은 남긴다."""
    sub = tc.find(HP + "subList")
    if sub is None:
        return
    paras = sub.findall(HP + "p")
    if not paras:
        return
    for extra in paras[1:]:
        sub.remove(extra)
    para = paras[0]

    runs = para.findall(HP + "run")
    keep = None
    for run in runs:
        # 컨트롤(각주·개체 등)이 든 run은 건드리지 않는다
        if keep is None and all(_local(ch.tag) in ("t", "") for ch in run):
            keep = run
    if keep is None:
        keep = runs[0] if runs else etree.SubElement(para, HP + "run")

    for run in runs:
        if run is not keep:
            para.remove(run)

    for child in list(keep):
        keep.remove(child)
    etree.SubElement(keep, HP + "t").text = ""

    # 줄바꿈 위치 캐시는 한/글이 다시 계산하므로 지운다
    for seg in para.findall(HP + "linesegarray"):
        para.remove(seg)


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


@dataclass
class Removal:
    section: int
    table: int
    row: int
    col: int
    label: str
    old_text: str


def clear_cells(
    blob: bytes,
    *,
    labels: tuple[str, ...] | None = DEFAULT_LABELS,
    positions: list[tuple[int, int, int, int]] | None = None,
    orders: list[tuple[int, int, int]] | None = None,
) -> tuple[bytes, list[Removal]]:
    """
    labels    : 머리글 텍스트. 일치하는 셀 '바로 아래' 칸을 비운다.
    positions : [(섹션, 표번호, 행, 열)] 직접 지정 (0부터).
    orders    : [(섹션, 표번호, N)] 표 안에서 N번째 칸 (1부터).
    반환: (수정된 hwpx 바이트, 지운 목록)
    """
    parts = _open(blob)
    removals: list[Removal] = []
    changed: set[str] = set()

    for si, name in enumerate(_section_names(parts)):
        root = etree.fromstring(parts[name])
        tables = list(root.iter(HP + "tbl"))
        touched = False

        for ti, tbl in enumerate(tables):
            info = _scan_table(tbl, si, ti)
            targets: list[tuple[Cell, str]] = []

            if labels:
                norm = {l.replace(" ", "") for l in labels}
                for cell in info.cells:
                    if cell.text.replace(" ", "") in norm:
                        below = info.at(cell.row + cell.row_span, cell.col)
                        if below is not None:
                            targets.append((below, cell.text))

            for s, t, r, c in (positions or []):
                if s == si and t == ti:
                    cell = info.at(r, c)
                    if cell is not None:
                        targets.append((cell, f"위치 {r},{c}"))

            for s, t, n in (orders or []):
                if s == si and t == ti:
                    for cell in info.cells:
                        if cell.order == n:
                            targets.append((cell, f"{n}번째 칸"))

            seen = set()
            for cell, label in targets:
                key = (cell.row, cell.col)
                if key in seen:
                    continue
                seen.add(key)
                if not cell.text:
                    continue                     # 이미 비어 있으면 건드리지 않음
                removals.append(Removal(si, ti, cell.row, cell.col, label, cell.text))
                _blank_cell(cell._el)
                touched = True

        if touched:
            parts[name] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )
            changed.add(name)

    if not changed:
        return blob, removals
    return _repack(parts), removals


def _repack(parts: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if "mimetype" in parts:
            zi = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_STORED
            z.writestr(zi, parts["mimetype"])
        for name, data in parts.items():
            if name == "mimetype":
                continue
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)
    return buf.getvalue()
