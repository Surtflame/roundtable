"""上传文件的保存、图片规范化、文本抽取；模型生成图片的入库。"""
import base64
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path

from . import config, db

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".log", ".xml", ".html", ".htm", ".css", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".py", ".rb", ".go", ".rs",
    ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh", ".bash", ".zsh", ".sql", ".r", ".swift",
    ".lua", ".pl", ".tex", ".bib", ".srt", ".vtt", ".svg", ".env", ".conf", ".diff", ".patch", ".ipynb",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif"}


class FileError(Exception):
    pass


def _path_for(fid: str, ext: str) -> Path:
    d = config.FILES_DIR / fid[:2]
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{fid}{ext}"


def _clean_name(name: str) -> str:
    name = os.path.basename((name or "file").replace("\\", "/")).strip() or "file"
    return "".join(ch for ch in name if ch >= " " and ch not in '<>"|?*')[:200] or "file"


# ---------- 图片 ----------

def normalize_image(data: bytes) -> tuple[bytes, str, dict]:
    """统一成 PNG/JPEG，限制边长和体积。返回 (bytes, mime, meta)。"""
    from PIL import Image, ImageOps
    heif = True
    try:
        import pillow_heif                    # 可选依赖（HEIC/HEIF），见 requirements.txt 里的说明
        pillow_heif.register_heif_opener()
    except Exception:
        heif = False
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception as e:
        if not heif and data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"hevc", b"mif1", b"msf1"):
            raise FileError("这是 HEIC 图片，服务器没装 pillow-heif，读不了。"
                            "先转成 JPEG 再传，或者在服务器上 pip install pillow-heif。")
        raise FileError(f"图片无法解析：{e}")
    fmt = (im.format or "").upper()
    w, h = im.size
    exif_rot = False
    try:
        exif_rot = im.getexif().get(0x0112, 1) not in (1, None)
    except Exception:
        pass
    keep = (fmt in ("PNG", "JPEG") and max(w, h) <= config.MAX_IMAGE_SIDE
            and len(data) <= config.MAX_IMAGE_BYTES and not exif_rot)
    if keep:
        return data, "image/png" if fmt == "PNG" else "image/jpeg", {"w": w, "h": h}
    im = ImageOps.exif_transpose(im)
    if getattr(im, "n_frames", 1) > 1:
        im.seek(0)
    if max(im.size) > config.MAX_IMAGE_SIDE:
        im.thumbnail((config.MAX_IMAGE_SIDE, config.MAX_IMAGE_SIDE), Image.LANCZOS)
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    out = io.BytesIO()
    if has_alpha or fmt in ("PNG", "GIF", "BMP"):
        im.convert("RGBA" if has_alpha else "RGB").save(out, "PNG", optimize=True)
        mime = "image/png"
        if out.tell() > config.MAX_IMAGE_BYTES:
            out = io.BytesIO()
            bg = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            bg.save(out, "JPEG", quality=88)
            mime = "image/jpeg"
    else:
        im.convert("RGB").save(out, "JPEG", quality=90)
        mime = "image/jpeg"
    return out.getvalue(), mime, {"w": im.size[0], "h": im.size[1]}


# ---------- 文本抽取 ----------

def _cap(text: str) -> tuple[str, bool]:
    if len(text) > config.MAX_TEXT_CHARS:
        return text[:config.MAX_TEXT_CHARS], True
    return text, False


def _open_pdf(path):
    import pypdfium2 as pdfium
    try:
        return pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as e:
        if "password" in str(e).lower():
            raise FileError("PDF 有密码，无法读取") from None
        raise FileError("PDF 打不开（文件可能已损坏）") from None


def extract_pdf(path: Path) -> tuple[str, dict]:
    doc = _open_pdf(path)
    try:
        pages = len(doc)
        parts, chars = [], 0
        for i in range(pages):
            page = doc[i]
            tp = page.get_textpage()
            t = tp.get_text_range().replace("\r\n", "\n").replace("\ufffe", "").strip()
            tp.close()
            page.close()
            chars += len(t)
            parts.append(f"--- 第 {i + 1} 页 ---\n{t}")
            if chars > config.MAX_TEXT_CHARS:
                break
    finally:
        doc.close()
    text, trunc = _cap("\n\n".join(parts))
    return text, {"pages": pages, "chars": chars, "scanned": chars < 40 * max(pages, 1), "truncated": trunc}


def extract_docx(path: Path) -> str:
    import docx
    d = docx.Document(str(path))
    out = []
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            t = "".join(n.text or "" for n in child.iter() if n.tag.endswith("}t"))
            if t.strip():
                out.append(t)
        elif tag == "tbl":
            for tr in child.iter():
                if tr.tag.endswith("}tr"):
                    cells = []
                    for tc in tr:
                        if tc.tag.endswith("}tc"):
                            cells.append(" ".join((n.text or "") for n in tc.iter() if n.tag.endswith("}t")).strip())
                    out.append(" | ".join(cells))
            out.append("")
    return "\n".join(out)


def extract_xlsx(path: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    out, total = [], 0
    for ws in wb.worksheets:
        out.append(f"=== 工作表：{ws.title} ===")
        for r in ws.iter_rows(values_only=True):
            if not any(c is not None and str(c) != "" for c in r):
                continue
            cells = ["" if c is None else str(c) for c in r]
            while cells and cells[-1] == "":
                cells.pop()
            line = "\t".join(cells)
            out.append(line)
            total += len(line)
            if total > config.MAX_TEXT_CHARS:
                break
        if total > config.MAX_TEXT_CHARS:
            break
    wb.close()
    return "\n".join(out)


def extract_pptx(path: Path) -> str:
    from pptx import Presentation
    prs = Presentation(str(path))
    out = []
    for i, slide in enumerate(prs.slides, 1):
        out.append(f"--- 幻灯片 {i} ---")
        for shp in slide.shapes:
            if shp.has_text_frame:
                t = "\n".join(p.text for p in shp.text_frame.paragraphs if p.text.strip())
                if t:
                    out.append(t)
            if getattr(shp, "has_table", False) and shp.has_table:
                for r in shp.table.rows:
                    out.append(" | ".join(c.text for c in r.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            n = slide.notes_slide.notes_text_frame.text.strip()
            if n:
                out.append(f"[备注] {n}")
    return "\n".join(out)


def decode_text(data: bytes) -> str | None:
    if b"\x00" in data[:8192] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None
    for enc in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return None


def ipynb_to_text(raw: str) -> str:
    try:
        nb = json.loads(raw)
        out = []
        for c in nb.get("cells", []):
            src = "".join(c.get("source", []))
            out.append(f"```{'python' if c.get('cell_type') == 'code' else 'markdown'}\n{src}\n```")
        return "\n\n".join(out)
    except Exception:
        return raw


# ---------- 入库 ----------

def save_upload(name: str, data: bytes, declared_mime: str = "") -> dict:
    """同步函数，放线程里跑。返回可直接 insert 的 dict（不含 conv_id）。"""
    if not data:
        raise FileError("空文件")
    if len(data) > config.MAX_UPLOAD:
        raise FileError(f"文件超过 {config.MAX_UPLOAD // (1024 * 1024)}MB")
    name = _clean_name(name)
    ext = os.path.splitext(name)[1].lower()
    fid = db.new_id()
    meta: dict = {"sha256": hashlib.sha256(data).hexdigest()}

    is_pdf = data[:5] == b"%PDF-" or ext == ".pdf"
    is_image = (ext in IMAGE_EXT or declared_mime.startswith("image/")) and ext != ".svg" \
        and declared_mime != "image/svg+xml"

    if is_image:
        blob, mime, m = normalize_image(data)
        meta.update(m)
        path = _path_for(fid, ".png" if mime == "image/png" else ".jpg")
        path.write_bytes(blob)
        return dict(id=fid, origin="upload", kind="image", name=name, mime=mime, size=len(blob),
                    path=str(path), text_path=None, meta=meta)

    if is_pdf:
        path = _path_for(fid, ".pdf")
        path.write_bytes(data)
        try:
            text, m = extract_pdf(path)
        except FileError:
            path.unlink(missing_ok=True)
            raise
        except Exception as e:
            path.unlink(missing_ok=True)
            raise FileError(f"PDF 解析失败：{e}")
        meta.update(m)
        tp = _path_for(fid, ".txt")
        tp.write_text(text, encoding="utf-8")
        return dict(id=fid, origin="upload", kind="pdf", name=name, mime="application/pdf", size=len(data),
                    path=str(path), text_path=str(tp), meta=meta)

    text = None
    try:
        if ext in (".docx", ".xlsx", ".xlsm", ".pptx"):
            tmp = _path_for(fid, ext)
            tmp.write_bytes(data)
            try:
                text = {".docx": extract_docx, ".xlsx": extract_xlsx, ".xlsm": extract_xlsx,
                        ".pptx": extract_pptx}[ext](tmp)
            finally:
                tmp.unlink(missing_ok=True)
        elif ext in (".doc", ".xls", ".ppt"):
            raise FileError("旧版 Office 格式（.doc/.xls/.ppt）不支持，请另存为 docx/xlsx/pptx 或 PDF")
        else:
            text = decode_text(data)
            if text is not None and ext == ".ipynb":
                text = ipynb_to_text(text)
    except FileError:
        raise
    except Exception as e:
        raise FileError(f"文件解析失败：{e}")
    if text is None:
        raise FileError("不支持的文件类型（支持图片、PDF、docx/xlsx/pptx 和各种文本/代码文件）")
    text, trunc = _cap(text)
    meta.update({"chars": len(text), "truncated": trunc})
    path = _path_for(fid, ".bin")
    path.write_bytes(data)
    tp = _path_for(fid, ".txt")
    tp.write_text(text, encoding="utf-8")
    mime = declared_mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return dict(id=fid, origin="upload", kind="text", name=name, mime=mime, size=len(data),
                path=str(path), text_path=str(tp), meta=meta)


def insert_file(rec: dict, conv_id: str | None) -> dict:
    db.x("INSERT INTO files(id,conv_id,origin,kind,name,mime,size,path,text_path,meta,created_at) "
         "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
         (rec["id"], conv_id, rec["origin"], rec["kind"], rec["name"], rec["mime"], rec["size"], rec["path"],
          rec["text_path"], json.dumps(rec["meta"], ensure_ascii=False), db.now()))
    return public(get(rec["id"]))


def save_generated_image(data: bytes, conv_id: str, name: str = "") -> dict | None:
    """模型生成的图片：原样保存（只校验确实是图片），返回 public dict。"""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(data))
        fmt = (im.format or "").upper()
        w, h = im.size
    except Exception:
        return None
    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}.get(fmt)
    if not mime:
        try:
            data, mime, m = normalize_image(data)
            w, h = m["w"], m["h"]
        except FileError:
            return None
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}[mime]
    fid = db.new_id()
    path = _path_for(fid, ext)
    path.write_bytes(data)
    rec = dict(id=fid, origin="generated", kind="image", name=name or f"image-{fid[:6]}{ext}", mime=mime,
               size=len(data), path=str(path), text_path=None,
               meta={"w": w, "h": h, "sha256": hashlib.sha256(data).hexdigest()})
    return insert_file(rec, conv_id)


def get(fid: str) -> dict | None:
    return db.row(db.q1("SELECT * FROM files WHERE id=?", (fid,)))


def public(f: dict | None) -> dict | None:
    if not f:
        return None
    m = f.get("meta") or {}
    return {"id": f["id"], "kind": f["kind"], "name": f["name"], "mime": f["mime"], "size": f["size"],
            "origin": f["origin"], "w": m.get("w"), "h": m.get("h"), "pages": m.get("pages"),
            "chars": m.get("chars"), "scanned": m.get("scanned"), "truncated": m.get("truncated")}


def delete_file_rows(rows: list[dict]) -> None:
    for f in rows:
        for p in (f.get("path"), f.get("text_path")):
            if p:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass
        if f["kind"] == "pdf":
            for p in Path(f["path"]).parent.glob(f"{f['id']}.p*.jpg"):
                p.unlink(missing_ok=True)
        db.x("DELETE FROM files WHERE id=?", (f["id"],))


# ---------- 给后端用的读取 ----------

def read_text(f: dict) -> str:
    try:
        return Path(f["text_path"]).read_text(encoding="utf-8") if f.get("text_path") else ""
    except OSError:
        return ""


def read_b64(f: dict) -> str:
    return base64.b64encode(Path(f["path"]).read_bytes()).decode()


def pdf_page_images(f: dict, max_pages: int = 20, width: int = 1400) -> list[Path]:
    """扫描版 PDF：把前 max_pages 页渲染成 JPEG（缓存到磁盘）。"""
    out = []
    base = Path(f["path"]).parent
    doc = None
    n = min((f.get("meta") or {}).get("pages") or 0, max_pages)
    for i in range(n):
        p = base / f"{f['id']}.p{i + 1}.jpg"
        if not p.exists():
            if doc is None:
                doc = _open_pdf(f["path"])
            page = doc[i]
            zoom = min(2.5, width / max(page.get_width(), 1))
            bitmap = page.render(scale=zoom)
            bitmap.to_pil().convert("RGB").save(str(p), "JPEG", quality=80)
            bitmap.close()
            page.close()
        out.append(p)
    if doc is not None:
        doc.close()
    return out
