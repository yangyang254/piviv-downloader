# -*- coding: utf-8 -*-
"""不联网的本地功能测试: 链接解析 / 去重 / 多页下载 / ugoira->gif / 磁盘残留跳过"""
import io
import sys
import zipfile
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pixiv_dl
from pixiv_dl import (Downloader, HistoryDB, PixivClient, parse_targets)


class FakeClient:
    """模拟 PixivClient, 生成合成图片数据。"""

    def __init__(self):
        self.calls = []

    def _png(self, color, size=(60, 40)):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", size, color).save(buf, "PNG")
        return buf.getvalue()

    def illust(self, iid):
        self.calls.append(("illust", iid))
        return {"title": f"标题<iid:{iid}>", "userName": f"画师{iid}",
                "illustType": self._types[str(iid)], "pageCount": 2,
                "tags": {"tags": [
                    {"tag": "オリジナル"},
                    {"tag": "girls", "translatedName": "女孩"},
                    {"tag": "bad/slash"},
                    {"tag": "R-18"}]}}

    def pages(self, iid):
        self.calls.append(("pages", iid))
        return [{"urls": {"original": f"https://i.pximg.net/img/{iid}_p0.png"}},
                {"urls": {"original": f"https://i.pximg.net/img/{iid}_p1.png"}}]

    def ugoira_meta(self, iid):
        self.calls.append(("ugoira_meta", iid))
        return {"originalSrc": f"https://i.pximg.net/img/{iid}_ugoira.zip",
                "frames": [{"file": f"{n:06}.jpg", "delay": 80}
                           for n in range(4)]}

    def download_file(self, url, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if "ugoira" in url:  # 帧包: 4 帧, 颜色渐变
            with zipfile.ZipFile(dest, "w") as zf:
                for n in range(4):
                    zf.writestr(f"{n:06}.jpg",
                                self._png((n * 60, 100, 255 - n * 60)))
        else:
            dest.write_bytes(self._png((200, 10, 10)))
        return True

    _types = {"111": 0, "222": 1, "333": 2}  # 插画 / 漫画 / 动图


def run():
    tmp = Path(__file__).parent / "test_out"
    if tmp.exists():
        import shutil; shutil.rmtree(tmp)
    db_path = tmp / "h.db"
    db = HistoryDB(db_path)
    fc = FakeClient()
    dl = Downloader(fc, tmp / "dl", db, ugoira_format="gif")

    # 1) 链接解析
    t = parse_targets([
        "https://www.pixiv.net/artworks/123456#1",
        "https://www.pixiv.net/en/artworks/7777",
        "98765",
        "https://www.pixiv.net/users/42424/artworks",
        "https://www.pixiv.net/illust/index.php?illust_id=55555"])
    assert t == [("illust", "123456"), ("illust", "7777"), ("illust", "98765"),
                 ("user", "42424"), ("illust", "55555")], t
    print("PASS 链接解析")

    # 2) 下载三种类型
    assert dl.download_illust("111") is True   # 单类型插画走 pages
    assert dl.download_illust("222") is True
    assert dl.download_illust("333") is True   # ugoira
    files = sorted(p.name for p in (tmp / "dl").iterdir())
    assert len(files) == 5, files  # 2页*2 + 1gif
    assert files[-1].endswith(".gif"), files
    # 标签在文件名末尾(扩展名前), 优先翻译名, 非法字符转 _
    assert any("_p0_オリジナル_女孩_bad_slash_R-18.png" in f for f in files), files
    assert files[-1].count("_オリジナル_女孩_bad_slash_R-18.gif") == 1, files[-1]
    from PIL import Image
    with Image.open(tmp / "dl" / files[-1]) as gif:
        n_frames = getattr(gif, "n_frames", 1)
        assert n_frames == 4, f"gif 帧数 {n_frames}"
        assert gif.info.get("duration") == 80 or gif.info.get("duration"), gif.info
    print(f"PASS 三类作品下载, GIF {n_frames} 帧, 文件: {files}")

    # 3) 去重: 文件移走后依然跳过
    import shutil
    shutil.move(str(tmp / "dl"), str(tmp / "moved_away"))
    dl2 = Downloader(FakeClient(), tmp / "dl2", db)
    assert dl2.download_illust("111") is None
    assert dl2.download_illust("333") is None
    assert not (tmp / "dl2").exists()  # 没有任何网络/写盘动作
    assert db.count() == 3
    print("PASS 文件移走后不重下(按历史跳过)")

    # 4) force 重新下载
    dl3 = Downloader(FakeClient(), tmp / "dl3", db, force=True)
    assert dl3.download_illust("111") is True
    print("PASS --force 强制重下")

    # 5) 中断残留: 磁盘已有文件则跳过该文件, 只补缺的
    part = tmp / "dl3"
    existing = next(part.iterdir())
    # 手工只保留一个文件, 模拟上次中断
    for p in part.iterdir():
        if p != existing:
            p.unlink()
    db.remove("111")
    dl4 = Downloader(FakeClient(), part, db)
    assert dl4.download_illust("111") is True
    assert len(list(part.iterdir())) == 2
    assert existing.read_bytes() == existing.read_bytes()
    print("PASS 中断后补缺(已有文件不重下)")

    # 6) 历史管理
    assert db.has("111")
    db.remove("111")
    assert not db.has("111")
    db.clear()
    assert db.count() == 0
    print("PASS 历史管理")

    for d in (db, dl2.db, dl3.db, dl4.db):
        d.close()
    import shutil as sh
    sh.rmtree(tmp)
    print("\n全部测试通过 ✓")


if __name__ == "__main__":
    run()
