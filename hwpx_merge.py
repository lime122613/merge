"""
hwpx_merge.py — 여러 개의 HWPX 파일을 하나로 합치는 순수 파이썬 모듈.

HWPX는 ZIP + XML(OWPML, KS X 6101) 구조라 한컴오피스 없이도 조작할 수 있다.
핵심 난점은 "단순히 section XML을 이어붙이면 안 된다"는 점이다.
본문(section*.xml)은 서식을 숫자 ID로만 참조하고, 실제 서식 정의는
Contents/header.xml 안에 문서별로 따로 들어 있다.
따라서 두 번째 문서부터는 header 항목을 기준 문서 쪽으로 옮기면서
ID를 새로 부여하고, 본문의 모든 *IDRef 값을 그 새 ID로 바꿔줘야 한다.

이 모듈이 하는 일:
  1) 기준 문서(첫 번째 파일)의 header.xml을 베이스로 삼는다
  2) 나머지 문서의 서식 항목(글꼴/테두리/글자모양/문단모양/스타일/번호매기기...)을
     의존 순서대로 베이스에 병합한다. 내용이 완전히 같으면 재사용(중복 제거)하고,
     다르면 새 ID를 발급한다
  3) 각 문서의 section XML을 새 ID로 리라이트해서 Contents/sectionN.xml로 추가한다
  4) BinData(이미지 등)는 파일명·item id에 접두사를 붙여 충돌을 피한다
  5) content.hpf의 manifest/spine, META-INF/manifest.xml, head@secCnt를 재구성한다

한글에서 section은 항상 새 쪽에서 시작하므로, 문서마다 원래의 용지·여백·
머리말/꼬리말 설정이 그대로 유지된 채 이어붙는다.
"""

from __future__ import annotations

import copy
import io
import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree

# --------------------------------------------------------------------------
# 네임스페이스
# --------------------------------------------------------------------------
NSMAP = {
    "ha": "http://www.hancom.co.kr/hwpml/2011/app",
    "hp": "http://www.hancom.co.kr/hwpml/2011/paragraph",
    "hp10": "http://www.hancom.co.kr/hwpml/2016/paragraph",
    "hs": "http://www.hancom.co.kr/hwpml/2011/section",
    "hc": "http://www.hancom.co.kr/hwpml/2011/core",
    "hh": "http://www.hancom.co.kr/hwpml/2011/head",
    "hhs": "http://www.hancom.co.kr/hwpml/2011/history",
    "hm": "http://www.hancom.co.kr/hwpml/2011/master-page",
    "hpf": "http://www.hancom.co.kr/schema/2011/hpf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "opf": "http://www.idpf.org/2007/opf/",
    "ooxmlchart": "http://www.hancom.co.kr/hwpml/2016/ooxmlchart",
    "hwpunitchar": "http://www.hancom.co.kr/hwpml/2016/HwpUnitChar",
    "epub": "http://www.idpf.org/2007/ops",
    "config": "urn:oasis:names:tc:opendocument:xmlns:config:1.0",
    "ocf": "urn:oasis:names:tc:opendocument:xmlns:container",
    "odf": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
}
HH = "{%s}" % NSMAP["hh"]
HP = "{%s}" % NSMAP["hp"]
OPF = "{%s}" % NSMAP["opf"]
ODF = "{%s}" % NSMAP["odf"]

MIMETYPE = b"application/hwp+zip"

# refList 안에서의 표준 순서 (스키마 순서를 지켜야 한글이 연다)
REFLIST_ORDER = [
    "fontfaces", "borderFills", "charProperties", "tabProperties",
    "numberings", "bullets", "paraProperties", "styles",
    "memoProperties", "trackChanges", "trackChangeAuthors",
]

# 컨테이너 태그 -> (항목 태그, 카테고리 키)
CONTAINERS = {
    "borderFills": ("borderFill", "borderFill"),
    "charProperties": ("charPr", "charPr"),
    "tabProperties": ("tabPr", "tabPr"),
    "numberings": ("numbering", "numbering"),
    "bullets": ("bullet", "bullet"),
    "paraProperties": ("paraPr", "paraPr"),
    "styles": ("style", "style"),
    "memoProperties": ("memoPr", "memoPr"),
    "trackChanges": ("trackChange", "trackChange"),
    "trackChangeAuthors": ("trackChangeAuthor", "trackChangeAuthor"),
}

# 의존 순서: 참조당하는 쪽을 먼저 처리해야 리맵이 정확하다.
#   borderFill -> (이미지 채우기) BinData
#   charPr     -> fontface, borderFill
#   numbering  -> charPr
#   paraPr     -> tabPr, borderFill, numbering, bullet
#   style      -> paraPr, charPr, (style: nextStyleIDRef는 2차 패스)
PROCESS_ORDER = [
    "fontfaces", "borderFills", "tabProperties", "charProperties",
    "numberings", "bullets", "paraProperties", "styles",
    "memoProperties", "trackChanges", "trackChangeAuthors",
]

# *IDRef 속성 -> 카테고리
REF_ATTRS = {
    "borderFillIDRef": "borderFill",
    "charPrIDRef": "charPr",
    "tabPrIDRef": "tabPr",
    "paraPrIDRef": "paraPr",
    "styleIDRef": "style",
    "nextStyleIDRef": "style",
    "memoShapeIDRef": "memoPr",
    "numberingIDRef": "numbering",
    "bulletIDRef": "bullet",
    "outlineShapeIDRef": "numbering",
}

# <hh:fontRef hangul="0" latin="0" .../> 의 속성 -> fontface@lang
FONTREF_LANGS = {
    "hangul": "HANGUL", "latin": "LATIN", "hanja": "HANJA",
    "japanese": "JAPANESE", "other": "OTHER", "symbol": "SYMBOL", "user": "USER",
}

SECTION_RE = re.compile(r"^Contents/section(\d+)\.xml$")


class HwpxMergeError(Exception):
    pass


# --------------------------------------------------------------------------
# 유틸
# --------------------------------------------------------------------------
def _local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _parse(data: bytes) -> etree._Element:
    parser = etree.XMLParser(remove_blank_text=False, huge_tree=True, resolve_entities=False)
    return etree.fromstring(data, parser=parser)


def _serialize(root: etree._Element) -> bytes:
    return etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    )


def _signature(el: etree._Element, skip_attrs=()) -> tuple:
    """id 등을 제외한 '내용이 같은지' 판별용 정규화 키."""
    attrs = tuple(sorted(
        (k, v) for k, v in el.attrib.items() if k not in skip_attrs
    ))
    text = (el.text or "").strip()
    kids = tuple(_signature(c) for c in el if isinstance(c.tag, str))
    return (el.tag, attrs, text, kids)


def _read_zip(data: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return {n: z.read(n) for n in z.namelist() if not n.endswith("/")}
    except zipfile.BadZipFile as exc:
        raise HwpxMergeError(
            "ZIP으로 열리지 않습니다. HWPX가 아니라 구형 HWP(바이너리) 파일일 수 있습니다."
        ) from exc


def _assert_hwpx(parts: dict[str, bytes], label: str) -> None:
    mt = parts.get("mimetype", b"").strip()
    if mt and mt != MIMETYPE:
        raise HwpxMergeError(f"{label}: mimetype이 HWPX가 아닙니다 ({mt!r}).")
    if "Contents/header.xml" not in parts:
        raise HwpxMergeError(f"{label}: Contents/header.xml이 없어 HWPX로 볼 수 없습니다.")
    if not any(SECTION_RE.match(n) for n in parts):
        raise HwpxMergeError(f"{label}: 본문(Contents/sectionN.xml)이 없습니다.")


def _section_names(parts: dict[str, bytes]) -> list[str]:
    """spine 순서를 우선하되, 없으면 파일명 숫자순."""
    names = sorted(
        (n for n in parts if SECTION_RE.match(n)),
        key=lambda n: int(SECTION_RE.match(n).group(1)),
    )
    hpf = parts.get("Contents/content.hpf")
    if not hpf:
        return names
    try:
        pkg = _parse(hpf)
    except etree.XMLSyntaxError:
        return names
    href_by_id = {
        it.get("id"): it.get("href")
        for it in pkg.iter(OPF + "item")
    }
    ordered = []
    for ref in pkg.iter(OPF + "itemref"):
        href = href_by_id.get(ref.get("idref"))
        if href and SECTION_RE.match(href) and href in parts:
            ordered.append(href)
    # spine에 빠진 섹션이 있으면 뒤에 붙인다
    ordered += [n for n in names if n not in ordered]
    return ordered or names


def _bindata_items(parts: dict[str, bytes]) -> dict[str, etree._Element]:
    """content.hpf manifest에서 BinData 항목(id -> item element)."""
    hpf = parts.get("Contents/content.hpf")
    if not hpf:
        return {}
    pkg = _parse(hpf)
    out = {}
    for it in pkg.iter(OPF + "item"):
        href = it.get("href") or ""
        if href.startswith("BinData/"):
            out[it.get("id")] = it
    return out


# --------------------------------------------------------------------------
# 베이스 레지스트리
# --------------------------------------------------------------------------
@dataclass
class _Registry:
    """기준 header.xml의 서식 항목 목록 + 중복 판별 인덱스."""
    head: etree._Element
    reflist: etree._Element
    containers: dict[str, etree._Element] = field(default_factory=dict)
    index: dict[str, dict[tuple, str]] = field(default_factory=dict)   # cat -> sig -> id
    next_id: dict[str, int] = field(default_factory=dict)              # cat -> 다음 id
    # 글꼴은 lang별로 id 공간이 따로다
    font_index: dict[str, dict[tuple, str]] = field(default_factory=dict)
    font_next: dict[str, int] = field(default_factory=dict)
    font_container: dict[str, etree._Element] = field(default_factory=dict)
    bindata_ids: set = field(default_factory=set)

    @classmethod
    def from_head(cls, head: etree._Element) -> "_Registry":
        reflist = head.find(HH + "refList")
        if reflist is None:
            raise HwpxMergeError("header.xml에 refList가 없습니다.")
        reg = cls(head=head, reflist=reflist)

        for cname, (iname, cat) in CONTAINERS.items():
            box = reflist.find(HH + cname)
            if box is None:
                continue
            reg.containers[cname] = box
            idx, hi = {}, -1
            for item in box.findall(HH + iname):
                iid = item.get("id")
                skip = ("id", "nextStyleIDRef") if cat == "style" else ("id",)
                idx.setdefault(_signature(item, skip), iid)
                try:
                    hi = max(hi, int(iid))
                except (TypeError, ValueError):
                    pass
            reg.index[cat] = idx
            reg.next_id[cat] = hi + 1

        faces = reflist.find(HH + "fontfaces")
        if faces is not None:
            reg.containers["fontfaces"] = faces
            for face in faces.findall(HH + "fontface"):
                lang = face.get("lang")
                reg.font_container[lang] = face
                idx, hi = {}, -1
                for f in face.findall(HH + "font"):
                    idx.setdefault(_signature(f, ("id",)), f.get("id"))
                    try:
                        hi = max(hi, int(f.get("id")))
                    except (TypeError, ValueError):
                        pass
                reg.font_index[lang] = idx
                reg.font_next[lang] = hi + 1
        return reg

    # -- 컨테이너가 없으면 refList의 올바른 위치에 새로 만든다 ------------
    def ensure_container(self, cname: str) -> etree._Element:
        if cname in self.containers:
            return self.containers[cname]
        box = etree.SubElement(self.reflist, HH + cname)
        box.set("itemCnt", "0")
        # 표준 순서에 맞게 이동
        target = REFLIST_ORDER.index(cname)
        for existing in list(self.reflist):
            name = _local(existing.tag)
            if name in REFLIST_ORDER and REFLIST_ORDER.index(name) > target:
                existing.addprevious(box)
                break
        self.containers[cname] = box
        _, cat = CONTAINERS.get(cname, (None, None))
        if cat:
            self.index.setdefault(cat, {})
            self.next_id.setdefault(cat, 0)
        return box

    def ensure_face(self, lang: str) -> etree._Element:
        if lang in self.font_container:
            return self.font_container[lang]
        faces = self.ensure_container("fontfaces")
        face = etree.SubElement(faces, HH + "fontface")
        face.set("lang", lang)
        face.set("fontCnt", "0")
        self.font_container[lang] = face
        self.font_index[lang] = {}
        self.font_next[lang] = 0
        return face

    def refresh_counts(self) -> None:
        for cname, (iname, _) in CONTAINERS.items():
            box = self.containers.get(cname)
            if box is not None:
                box.set("itemCnt", str(len(box.findall(HH + iname))))
        faces = self.containers.get("fontfaces")
        if faces is not None:
            face_list = faces.findall(HH + "fontface")
            faces.set("itemCnt", str(len(face_list)))
            for face in face_list:
                face.set("fontCnt", str(len(face.findall(HH + "font"))))


# --------------------------------------------------------------------------
# ID 리맵
# --------------------------------------------------------------------------
def _remap(el: etree._Element, idmap: dict[str, dict[str, str]]) -> None:
    """엘리먼트 트리 전체의 참조 ID를 idmap 기준으로 치환한다."""
    for node in el.iter():
        if not isinstance(node.tag, str):
            continue
        tag = _local(node.tag)

        if tag == "fontRef":
            for attr, lang in FONTREF_LANGS.items():
                v = node.get(attr)
                if v is not None:
                    node.set(attr, idmap.get("font:" + lang, {}).get(v, v))
            continue

        if tag == "heading":
            v = node.get("idRef")
            if v is not None:
                htype = (node.get("type") or "").upper()
                cat = "bullet" if htype == "BULLET" else "numbering"
                node.set("idRef", idmap.get(cat, {}).get(v, v))
            # heading에도 charPrIDRef 등이 올 수 있으니 계속 진행

        for attr, value in list(node.attrib.items()):
            if attr == "binaryItemIDRef":
                node.set(attr, idmap.get("binData", {}).get(value, value))
            elif attr in REF_ATTRS:
                cat = REF_ATTRS[attr]
                node.set(attr, idmap.get(cat, {}).get(value, value))


# --------------------------------------------------------------------------
# 본 병합
# --------------------------------------------------------------------------
@dataclass
class MergeResult:
    data: bytes
    section_count: int
    warnings: list[str]


def merge_hwpx(files: list[tuple[str, bytes]], dedupe: bool = True) -> MergeResult:
    """
    files: [(표시용 이름, hwpx 바이트), ...] — 목록 순서대로 이어붙인다.
    반환: MergeResult(합쳐진 hwpx 바이트, 섹션 수, 경고 목록)
    """
    if len(files) < 2:
        raise HwpxMergeError("파일이 2개 이상 필요합니다.")

    warnings: list[str] = []

    base_name, base_bytes = files[0]
    base = _read_zip(base_bytes)
    _assert_hwpx(base, base_name)

    out: dict[str, bytes] = dict(base)
    out["mimetype"] = MIMETYPE

    head = _parse(base["Contents/header.xml"])
    reg = _Registry.from_head(head)
    reg.bindata_ids = set(_bindata_items(base))

    # 기준 문서 섹션 먼저 배치
    sections: list[bytes] = [base[n] for n in _section_names(base)]
    for n in list(out):
        if SECTION_RE.match(n):
            del out[n]

    bindata_items: dict[str, etree._Element] = {}
    for iid, item in _bindata_items(base).items():
        bindata_items[iid] = copy.deepcopy(item)

    # ---------------- 2번째 문서부터 병합 ----------------
    for doc_no, (name, blob) in enumerate(files[1:], start=1):
        parts = _read_zip(blob)
        _assert_hwpx(parts, name)
        prefix = f"d{doc_no}_"
        idmap: dict[str, dict[str, str]] = {}

        # 1) BinData: 파일명·item id에 접두사
        bmap: dict[str, str] = {}
        for iid, item in _bindata_items(parts).items():
            href = item.get("href")
            if href not in parts:
                continue
            new_href = "BinData/" + prefix + href.split("/", 1)[1]
            new_id = prefix + iid
            out[new_href] = parts[href]
            clone = copy.deepcopy(item)
            clone.set("id", new_id)
            clone.set("href", new_href)
            bindata_items[new_id] = clone
            bmap[iid] = new_id
        idmap["binData"] = bmap

        # 2) header 서식 항목을 의존 순서대로 병합
        src_head = _parse(parts["Contents/header.xml"])
        src_ref = src_head.find(HH + "refList")
        if src_ref is None:
            raise HwpxMergeError(f"{name}: header.xml에 refList가 없습니다.")

        pending_next_style: list[tuple[etree._Element, str]] = []

        for cname in PROCESS_ORDER:
            src_box = src_ref.find(HH + cname)
            if src_box is None:
                continue

            # --- 글꼴은 lang별 처리 ---
            if cname == "fontfaces":
                for face in src_box.findall(HH + "fontface"):
                    lang = face.get("lang")
                    dst_face = reg.ensure_face(lang)
                    fmap = idmap.setdefault("font:" + lang, {})
                    for f in face.findall(HH + "font"):
                        old = f.get("id")
                        clone = copy.deepcopy(f)
                        sig = _signature(clone, ("id",))
                        hit = reg.font_index[lang].get(sig) if dedupe else None
                        if hit is not None:
                            fmap[old] = hit
                            continue
                        new = str(reg.font_next[lang])
                        reg.font_next[lang] += 1
                        clone.set("id", new)
                        dst_face.append(clone)
                        reg.font_index[lang][sig] = new
                        fmap[old] = new
                continue

            iname, cat = CONTAINERS[cname]
            dst_box = reg.ensure_container(cname)
            cmap = idmap.setdefault(cat, {})
            reg.index.setdefault(cat, {})
            reg.next_id.setdefault(cat, 0)

            for item in src_box.findall(HH + iname):
                old = item.get("id")
                clone = copy.deepcopy(item)
                # 이미 처리된 카테고리의 참조를 먼저 치환
                _remap(clone, idmap)

                if cat == "style":
                    nxt = clone.get("nextStyleIDRef")
                    skip = ("id", "nextStyleIDRef")
                else:
                    nxt, skip = None, ("id",)

                sig = _signature(clone, skip)
                hit = reg.index[cat].get(sig) if dedupe else None
                if hit is not None:
                    cmap[old] = hit
                    continue

                new = str(reg.next_id[cat])
                reg.next_id[cat] += 1
                clone.set("id", new)
                dst_box.append(clone)
                reg.index[cat][sig] = new
                cmap[old] = new
                if cat == "style" and nxt is not None:
                    pending_next_style.append((clone, nxt))

        # style의 nextStyleIDRef는 전방 참조가 가능하므로 2차 패스
        smap = idmap.get("style", {})
        for el, old in pending_next_style:
            el.set("nextStyleIDRef", smap.get(old, old))

        # 3) 본문 섹션 리라이트
        for sec_name in _section_names(parts):
            sec = _parse(parts[sec_name])
            _remap(sec, idmap)
            sections.append(_serialize(sec))

        # 4) 처리 못 한 부속 파일 경고
        for n in parts:
            if n.startswith("Contents/") and not SECTION_RE.match(n) \
               and n not in ("Contents/header.xml", "Contents/content.hpf"):
                warnings.append(f"{name}: '{n}'은(는) 병합되지 않았습니다(마스터 페이지 등).")

    # ---------------- 결과 조립 ----------------
    reg.refresh_counts()
    head.set("secCnt", str(len(sections)))
    out["Contents/header.xml"] = _serialize(head)

    for i, data in enumerate(sections):
        out[f"Contents/section{i}.xml"] = data

    out["Contents/content.hpf"] = _rebuild_hpf(
        base.get("Contents/content.hpf"), len(sections), bindata_items
    )
    if "META-INF/manifest.xml" in out:
        out["META-INF/manifest.xml"] = _rebuild_odf_manifest(
            out["META-INF/manifest.xml"], out
        )

    order = ["mimetype"] + [n for n in out if n != "mimetype"]
    return MergeResult(_pack(out, order), len(sections), warnings)


def _rebuild_hpf(hpf_bytes: bytes | None, n_sections: int,
                 bindata_items: dict[str, etree._Element]) -> bytes:
    if not hpf_bytes:
        raise HwpxMergeError("기준 문서에 Contents/content.hpf가 없습니다.")
    pkg = _parse(hpf_bytes)
    manifest = pkg.find(OPF + "manifest")
    spine = pkg.find(OPF + "spine")
    if manifest is None or spine is None:
        raise HwpxMergeError("content.hpf에 manifest/spine이 없습니다.")

    keep = []
    for item in list(manifest):
        href = item.get("href") or ""
        if SECTION_RE.match(href) or href.startswith("BinData/"):
            manifest.remove(item)
        else:
            keep.append(item)

    for i in range(n_sections):
        it = etree.SubElement(manifest, OPF + "item")
        it.set("id", f"section{i}")
        it.set("href", f"Contents/section{i}.xml")
        it.set("media-type", "application/xml")
    for iid in sorted(bindata_items):
        manifest.append(copy.deepcopy(bindata_items[iid]))

    for ref in list(spine):
        if (ref.get("idref") or "").startswith("section"):
            spine.remove(ref)
    for i in range(n_sections):
        ref = etree.SubElement(spine, OPF + "itemref")
        ref.set("idref", f"section{i}")
        ref.set("linear", "yes")
    return _serialize(pkg)


def _rebuild_odf_manifest(manifest_bytes: bytes, parts: dict[str, bytes]) -> bytes:
    root = _parse(manifest_bytes)
    entries = root.findall(ODF + "file-entry")
    if not entries:
        return manifest_bytes  # 비어 있는 형태면 그대로 둔다
    media = {e.get(ODF + "full-path"): e.get(ODF + "media-type") for e in entries}
    for e in entries:
        root.remove(e)
    for path in parts:
        if path == "mimetype":
            continue
        e = etree.SubElement(root, ODF + "file-entry")
        e.set(ODF + "full-path", path)
        e.set(ODF + "media-type", media.get(path, _guess_media(path)))
    return _serialize(root)


def _guess_media(path: str) -> str:
    low = path.lower()
    for ext, mt in (
        (".xml", "application/xml"), (".hpf", "application/xml"),
        (".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
        (".gif", "image/gif"), (".bmp", "image/bmp"), (".txt", "text/plain"),
        (".rdf", "application/rdf+xml"),
    ):
        if low.endswith(ext):
            return mt
    return "application/octet-stream"


def _pack(parts: dict[str, bytes], order: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        zi = zipfile.ZipInfo("mimetype", date_time=(1980, 1, 1, 0, 0, 0))
        zi.compress_type = zipfile.ZIP_STORED
        z.writestr(zi, parts["mimetype"])
        for name in order:
            if name == "mimetype":
                continue
            zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, parts[name])
    return buf.getvalue()
