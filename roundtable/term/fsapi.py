"""文件管理：看目录、看和改文本、上传、下载、新建、改名、删除。

以运行服务的那个用户的权限做事，不走 sudo。config.FS_PROTECTED 下面不许改不许删。
网页预览只给图片；别的一律当附件下载，绝不在本站的域名下把文件内容当网页渲染。
"""
import os
import shutil
import stat
import tempfile

from . import config

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


class FsError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def norm(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or len(path) > 4096:
        raise FsError("路径要写完整的绝对路径")
    return os.path.normpath(path)


def _protected(path: str) -> bool:
    real = os.path.realpath(path)
    return any(real == p or real.startswith(p + "/") for p in config.FS_PROTECTED)


def _writable(path: str) -> None:
    if _protected(path) or _protected(os.path.dirname(path)):
        raise FsError("这个目录受保护，文件管理里不许改动（真要动，去终端里做）", 403)


def _entry(full: str, name: str) -> dict:
    try:
        st = os.lstat(full)
    except OSError:
        return {"name": name, "type": "?", "size": 0, "mtime": 0}
    kind = "dir" if stat.S_ISDIR(st.st_mode) else "link" if stat.S_ISLNK(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else "other"
    d = {"name": name, "type": kind, "size": st.st_size, "mtime": st.st_mtime, "mode": stat.filemode(st.st_mode)}
    if kind == "link":
        try:
            d["target"] = os.readlink(full)
            d["to_dir"] = os.path.isdir(full)
        except OSError:
            pass
    return d


def listdir(path: str, hidden: bool = False) -> dict:
    path = norm(path)
    try:
        names = os.listdir(path)
    except NotADirectoryError:
        raise FsError("这不是一个目录") from None
    except FileNotFoundError:
        raise FsError("目录不存在", 404) from None
    except PermissionError:
        raise FsError("没有权限看这个目录", 403) from None
    if not hidden:
        names = [n for n in names if not n.startswith(".")]
    total = len(names)
    names.sort(key=str.lower)
    items = [_entry(os.path.join(path, n), n) for n in names[:config.MAX_LIST]]
    items.sort(key=lambda e: (0 if e["type"] == "dir" or e.get("to_dir") else 1, e["name"].lower()))
    return {"path": path, "parent": os.path.dirname(path) if path != "/" else None, "items": items,
            "total": total, "protected": _protected(path)}


def read_text(path: str) -> dict:
    path = norm(path)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise FsError("文件不存在", 404) from None
    except PermissionError:
        raise FsError("没有权限", 403) from None
    if not stat.S_ISREG(st.st_mode):
        raise FsError("这不是一个普通文件")
    info = {"path": path, "size": st.st_size, "mtime": st.st_mtime, "image": os.path.splitext(path)[1].lower() in IMAGE_TYPES,
            "protected": _protected(path)}
    if info["image"]:
        return {**info, "kind": "image"}
    if st.st_size > config.MAX_EDIT:
        return {**info, "kind": "big"}
    try:
        raw = open(path, "rb").read()
    except PermissionError:
        raise FsError("没有权限读这个文件", 403) from None
    if b"\x00" in raw[:8192]:
        return {**info, "kind": "binary"}
    try:
        return {**info, "kind": "text", "text": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {**info, "kind": "binary"}           # 不是 UTF-8 的不在网页里改，免得存回去把编码弄坏


def write_text(path: str, text: str, expect_mtime: float | None = None, create: bool = False) -> dict:
    path = norm(path)
    _writable(path)
    if len(text.encode()) > config.MAX_EDIT:
        raise FsError("太大了，网页里不改这么大的文件")
    exists = os.path.lexists(path)
    if create and exists:
        raise FsError("已经有同名的东西了")
    if not create:
        if not os.path.isfile(path):
            raise FsError("文件不存在", 404)
        if expect_mtime is not None and abs(os.stat(path).st_mtime - expect_mtime) > 0.0005:
            raise FsError("这个文件在你打开之后被别的程序改过了。重新打开再改，免得把别人的改动盖掉。", 409)
    d = os.path.dirname(path)
    mode = stat.S_IMODE(os.stat(path).st_mode) if exists else 0o644
    try:
        fd, tmp = tempfile.mkstemp(prefix=".term-web-", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except PermissionError:
        raise FsError("没有权限写这个位置", 403) from None
    return {"path": path, "mtime": os.stat(path).st_mtime, "size": os.path.getsize(path)}


def mkdir(path: str) -> None:
    path = norm(path)
    _writable(path)
    try:
        os.mkdir(path)
    except FileExistsError:
        raise FsError("已经有同名的东西了") from None
    except (PermissionError, FileNotFoundError) as e:
        raise FsError("建不了：" + (e.strerror or str(e)), 403) from None


def rename(src: str, dst: str) -> None:
    src, dst = norm(src), norm(dst)
    _writable(src)
    _writable(dst)
    if not os.path.lexists(src):
        raise FsError("原来的文件不在了", 404)
    if os.path.lexists(dst):
        raise FsError("目标位置已经有同名的东西了")
    try:
        os.rename(src, dst)
    except OSError as e:
        raise FsError("改不了：" + (e.strerror or str(e)), 403) from None


def delete(path: str, recursive: bool = False) -> None:
    path = norm(path)
    _writable(path)
    if path in ("/", config.HOME) or path.count("/") < 2:
        raise FsError("这个不让在网页里删", 403)
    try:
        if os.path.islink(path) or not os.path.isdir(path):
            os.unlink(path)
        elif recursive:
            shutil.rmtree(path)
        else:
            os.rmdir(path)
    except FileNotFoundError:
        raise FsError("已经不在了", 404) from None
    except OSError as e:
        raise FsError("删不了：" + ("目录不是空的" if e.errno == 39 else e.strerror or str(e)), 400) from None


def upload_target(directory: str, name: str, overwrite: bool) -> str:
    directory = norm(directory)
    name = os.path.basename(name.replace("\\", "/")).strip()
    if not name or name in (".", ".."):
        raise FsError("文件名不对")
    dst = os.path.join(directory, name)
    _writable(dst)
    if not os.path.isdir(directory):
        raise FsError("目录不存在", 404)
    if os.path.lexists(dst) and not overwrite:
        raise FsError(f"已经有一个 {name} 了", 409)
    return dst


def download_info(path: str) -> tuple[str, str, bool]:
    """→ (路径, Content-Type, 是不是可以直接在页面里显示的图片)。"""
    path = norm(path)
    if not os.path.isfile(path):
        raise FsError("文件不存在", 404)
    if not os.access(path, os.R_OK):
        raise FsError("没有权限读这个文件", 403)
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_TYPES:
        return path, IMAGE_TYPES[ext], True
    return path, "application/octet-stream", False


def roots() -> list[dict]:
    """文件面板顶上的快捷入口。"""
    cand = [("家目录", config.HOME)]
    if config.DEFAULT_CWD != config.HOME:
        cand.append((os.path.basename(config.DEFAULT_CWD) or config.DEFAULT_CWD, config.DEFAULT_CWD))
    cand += [("/tmp", "/tmp"), ("根目录", "/")]
    return [{"name": n, "path": p} for n, p in cand if os.path.isdir(p)]
