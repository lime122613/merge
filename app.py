"""
한글 문서 합치기 (HWPX Merger) — Streamlit 앱

여러 과목 계획서(.hwpx)를 순서대로 하나로 이어붙이고,
나이스 업로드용으로 '지도교사' 칸을 비운다.

이 앱은 .hwpx 만 다룬다. 구형 .hwp 는 한/글에서 hwpx 로 저장한 뒤 올린다.

실행:
    pip install -r requirements.txt
    streamlit run app.py
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from datetime import datetime

import streamlit as st
from lxml import etree

from hwpx_anonymize import DEFAULT_LABELS, clear_cells, read_tables
from hwpx_merge import HwpxMergeError, merge_hwpx

HP_T = "{http://www.hancom.co.kr/hwpml/2011/paragraph}t"
SECTION_RE = re.compile(r"^Contents/section(\d+)\.xml$")
HWP_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # 구형 .hwp (OLE2) 시그니처

st.set_page_config(page_title="한글 문서 합치기", page_icon="📎", layout="centered")


# ------------------------------------------------------------------ helpers
def is_real_hwpx(blob: bytes) -> bool:
    return blob[:2] == b"PK" and not blob[:8] == HWP_MAGIC


def is_legacy_hwp(blob: bytes) -> bool:
    return blob[:8] == HWP_MAGIC


def peek(blob: bytes, limit: int = 90) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = sorted(
                (n for n in z.namelist() if SECTION_RE.match(n)),
                key=lambda n: int(SECTION_RE.match(n).group(1)),
            )
            chunks = [t.text for n in names
                      for t in etree.fromstring(z.read(n)).iter(HP_T) if t.text]
        text = " ".join(chunks).strip()
        return text[:limit] + ("…" if len(text) > limit else "")
    except Exception:
        return "(미리보기 불가)"


def preview_table_html(table, mark: set) -> str:
    rows = []
    for r, line in enumerate(table.grid()):
        cells = []
        for c, val in enumerate(line):
            hit = (r, c) in mark
            style = (
                "background:#fff3f3;color:#c92a2a;font-weight:600;"
                if hit else "background:#fff;"
            )
            if hit and val:
                shown = f"<s>{html.escape(val)}</s> → (빈칸)"
            else:
                shown = html.escape(val) if val else "&nbsp;"
            cells.append(
                f'<td style="border:1px solid #adb5bd;padding:5px 9px;'
                f'font-size:13px;{style}">{shown}</td>'
            )
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f'<table style="border-collapse:collapse;width:100%">{"".join(rows)}</table>'


def teacher_marks(table, labels: tuple) -> set:
    norm = {l.replace(" ", "") for l in labels}
    marks = set()
    for cell in table.cells:
        if cell.text.replace(" ", "") in norm:
            below = table.at(cell.row + cell.row_span, cell.col)
            if below is not None:
                marks.add((below.row, below.col))
    return marks


def swap(i: int, j: int) -> None:
    order = st.session_state.order
    order[i], order[j] = order[j], order[i]


# ------------------------------------------------------------------ 헤더
st.title("📎 한글 문서 합치기")
st.caption("여러 과목의 운영계획서를 하나의 한글 파일로 합칩니다. 나이스 업로드용으로 지도교사 칸도 비울 수 있습니다.")

st.info(
    "**이 앱은 `.hwpx` 파일만 받습니다.**\n\n"
    "가지고 있는 파일이 `.hwp`라면 한/글에서 먼저 변환해 주세요 — "
    "**파일 → 다른 이름으로 저장 → 파일 형식을 `HWPX 문서 (*.hwpx)`로 선택.** "
    "그런 다음 저장된 `.hwpx`를 올리면 됩니다.",
    icon="📌",
)

with st.expander("처음 오셨나요? 3단계로 끝납니다"):
    st.markdown(
        """
1. **올리기** — 합칠 `.hwpx` 파일들을 한꺼번에 선택합니다.
2. **정하기** — 지도교사 칸을 비울지 고르고, 문서 순서를 맞춥니다.
3. **받기** — `만들기`를 누르고 합쳐진 파일을 내려받습니다.

- 각 문서는 새 쪽에서 시작하므로 용지·여백·머리말이 원본 그대로 유지됩니다.
- 지도교사 칸은 글자만 지우고 칸과 서식은 남으므로, 나중에 이름을 다시 넣어도 됩니다.
- 받은 파일은 한/글에서 한 번 열어 확인한 뒤 나이스에 올리는 것을 권장합니다.

---
🔒 **개인정보 안내**
업로드한 파일은 작업에 필요한 동안만 서버 메모리에 임시 보관되며,
디스크나 데이터베이스에는 저장되지 않습니다.
파일 내려받기가 끝나거나 페이지를 닫으면 즉시 삭제됩니다.
"""
    )

# ------------------------------------------------------------------ 1. 업로드
st.subheader("파일 올리기")
st.caption("🔒 업로드한 파일은 서버에 저장되지 않습니다. 작업이 끝나면 메모리에서 즉시 삭제됩니다.")
uploads = st.file_uploader(
    "합칠 파일 선택 (여러 개 가능)",
    type=["hwpx"],
    accept_multiple_files=True,
)
if not uploads:
    st.stop()

items = []
rejected = []
for f in uploads:
    raw = f.getvalue()
    if is_legacy_hwp(raw) or not is_real_hwpx(raw):
        rejected.append(f.name)
    else:
        items.append((f.name, raw))

if rejected:
    st.error(
        "다음 파일은 `.hwpx`가 아니라 처리할 수 없습니다: **"
        + ", ".join(rejected)
        + "**\n\n한/글에서 `다른 이름으로 저장 → HWPX 문서`로 변환한 뒤 다시 올려 주세요."
    )

if not items:
    st.stop()

# 업로드가 바뀌면 순서 초기화
fingerprint = [(n, len(b)) for n, b in items]
if st.session_state.get("fingerprint") != fingerprint:
    st.session_state.fingerprint = fingerprint
    st.session_state.order = list(range(len(items)))

# ------------------------------------------------------------------ 2. 익명화 선택
st.subheader("① 익명화")
mode = st.radio(
    "합치는 방식을 골라 주세요.",
    options=["anon", "plain"],
    format_func=lambda m: {
        "anon": "🙈 지도교사 칸을 비우고 합치기 (나이스 업로드용)",
        "plain": "📄 원본 그대로 합치기",
    }[m],
    index=0,
)
labels = DEFAULT_LABELS

if mode == "anon":
    st.caption("첫 번째 파일 기준 미리보기 — 빨간 칸이 비워집니다.")
    try:
        tables = read_tables(items[st.session_state.order[0]][1], section=0)
        found = False
        for t in tables[:2]:
            marks = teacher_marks(t, labels)
            found = found or bool(marks)
            st.markdown(preview_table_html(t, marks), unsafe_allow_html=True)
            st.write("")
        if not tables:
            st.warning("첫 페이지에서 표를 찾지 못했습니다. 파일 형식을 확인해 주세요.")
        elif not found:
            st.warning(
                "‘지도교사’ 머리글을 찾지 못했습니다. 표 문구가 다르면 그대로 합쳐질 수 있으니 "
                "결과를 한/글에서 확인해 주세요."
            )
    except Exception as exc:  # noqa: BLE001
        st.error(f"미리보기 실패: {exc}")

# ------------------------------------------------------------------ 3. 순서
st.subheader("② 순서")
if len(items) == 1:
    st.caption("파일이 1개라 병합 없이 처리합니다.")
else:
    st.caption("위에서부터 차례대로 이어붙습니다. 화살표로 순서를 바꿀 수 있습니다.")

for row, idx in enumerate(st.session_state.order):
    name, blob = items[idx]
    c1, c2, c3 = st.columns([8, 1, 1])
    with c1:
        st.markdown(f"**{row + 1}. {name}** · {len(blob) / 1024:,.0f} KB")
        st.caption(peek(blob))
    c2.button("▲", key=f"up{row}", disabled=row == 0,
              on_click=swap, args=(row, row - 1), use_container_width=True)
    c3.button("▼", key=f"dn{row}", disabled=row == len(items) - 1,
              on_click=swap, args=(row, row + 1), use_container_width=True)

now = datetime.now()
_year = now.year
_month = now.month
# 3~7월 → 1학기, 나머지(8~12월, 1~2월) → 2학기
_semester = 1 if 3 <= _month <= 7 else 2
default_name = f"{_year}학년도 {_semester}학기 교수학습 및 평가 운영계획_OO과.hwpx"
out_name = st.text_input(
    "저장할 파일 이름",
    value=default_name,
    help="학년도·학기가 맞지 않으면 직접 수정해 주세요.",
)

# ------------------------------------------------------------------ 4. 실행
if st.button("만들기", type="primary", use_container_width=True):
    ordered = [items[i] for i in st.session_state.order]
    log = []   # (level, message)

    if mode == "anon":
        cleaned = []
        for name, blob in ordered:
            try:
                blob, removals = clear_cells(blob, labels=labels)
            except Exception as exc:  # noqa: BLE001
                st.error(f"{name} 익명화 실패: {exc}")
                st.stop()
            if removals:
                for r in removals:
                    log.append(("ok", f"{name}: ‘{r.old_text}’ 삭제"))
            else:
                log.append(("warn", f"{name}: 지울 지도교사 칸을 찾지 못했습니다"))
            cleaned.append((name, blob))
        ordered = cleaned

    try:
        if len(ordered) == 1:
            data, sections, warnings = ordered[0][1], None, []
        else:
            with st.spinner("합치는 중…"):
                res = merge_hwpx(ordered)
            data, sections, warnings = res.data, res.section_count, res.warnings
    except HwpxMergeError as exc:
        st.error(str(exc))
        st.stop()

    st.success(
        f"완료 — {len(ordered)}개 문서"
        + (f", {sections}쪽 묶음" if sections else "")
        + f", {len(data) / 1024:,.0f} KB"
    )
    for level, msg in log:
        (st.warning if level == "warn" else st.write)(
            ("⚠️ " if level == "warn" else "✅ ") + msg
        )
    for w in warnings:
        st.warning(w)

    st.download_button(
        "⬇️ 합쳐진 파일 내려받기",
        data=data,
        file_name=out_name if out_name.endswith(".hwpx") else out_name + ".hwpx",
        mime="application/hwp+zip",
        use_container_width=True,
    )
    if mode == "anon":
        st.caption("나이스에 올리기 전에 한/글에서 열어 지도교사 칸이 비었는지 확인해 주세요.")
    
