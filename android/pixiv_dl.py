# -*- coding: utf-8 -*-
"""
pixiv_dl - Pixiv 批量下载命令行工具

功能:
  1. 通过作品分享链接(或作品ID/用户主页链接)批量下载
  2. 批量下载自己账号的收藏
  3. 动图(ugoira)自动下载帧 zip 并合成 GIF
  4. 下载历史持久化(SQLite): 文件移走后再次运行也不会重复下载

依赖: requests, Pillow
"""

import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:  # 动图合成才需要 Pillow
    from PIL import Image
except ImportError:
    Image = None

APP_DIR = Path(getattr(sys, "frozen", False) and Path(sys.executable).parent
               or Path(__file__).resolve().parent)
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_CONFIG = {
    "phpsessid": "",
    "user_id": "",
    "output_dir": "downloads",
    "delay": 1.0,
    "ugoira_format": "gif",
    "proxy": "",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PIXIV_ROOT = "https://www.pixiv.net"
IMG_REFERER = "https://www.pixiv.net/"


_log_sink = None


def set_logger(fn):
    """GUI 等环境可接管日志输出; 传 None 恢复 print。"""
    global _log_sink
    _log_sink = fn


def log(msg):
    if _log_sink is not None:
        _log_sink(msg)
    else:
        print(msg, flush=True)


# ---------------------------------------------------------------- 配置 / 历史

def load_config():
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"! config.json 解析失败({e}), 使用默认配置")
            cfg = {}
    else:
        cfg = {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    return merged


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def ensure_login(cfg):
    """没有 PHPSESSID 时交互式引导输入。"""
    if cfg.get("phpsessid", "").strip():
        return cfg["phpsessid"].strip()
    log("首次使用, 需要登录 pixiv 的 PHPSESSID cookie:")
    log("  1. 浏览器登录 https://www.pixiv.net")
    log("  2. 按 F12 打开开发者工具 -> Application(应用) -> Cookies -> https://www.pixiv.net")
    log("  3. 复制 PHPSESSID 的值粘贴到这里")
    sessid = input("PHPSESSID: ").strip()
    if not sessid:
        log("未输入 cookie, 退出。")
        sys.exit(1)
    cfg["phpsessid"] = sessid
    save_config(cfg)
    log(f"已保存到 {CONFIG_PATH}")
    return sessid


class HistoryDB:
    """下载历史。以作品ID为唯一键, 与文件是否存在无关 —— 移走文件也不会重下。"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS downloaded ("
            " ill_id TEXT PRIMARY KEY,"
            " title TEXT, author TEXT, file TEXT,"
            " downloaded_at TEXT)")
        self.conn.commit()

    def has(self, ill_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM downloaded WHERE ill_id=?", (str(ill_id),))
        return cur.fetchone() is not None

    def add(self, ill_id, title, author, file):
        self.conn.execute(
            "INSERT OR REPLACE INTO downloaded VALUES (?,?,?,?,?)",
            (str(ill_id), title or "", author or "", str(file),
             datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()

    def remove(self, ill_id):
        self.conn.execute("DELETE FROM downloaded WHERE ill_id=?", (str(ill_id),))
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM downloaded").fetchone()[0]

    def clear(self):
        self.conn.execute("DELETE FROM downloaded")
        self.conn.commit()

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------- Pixiv 客户端

class PixivClient:
    def __init__(self, phpsessid, delay=1.0, proxy=""):
        self.delay = delay
        self.proxies = ({"http": proxy, "https": proxy} if proxy else None)
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Referer": IMG_REFERER,
            "Cookie": f"PHPSESSID={phpsessid}",
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        if proxy:
            self.s.proxies.update(self.proxies)
        retry = Retry(total=3, backoff_factor=2,
                      status_forcelist=[429, 500, 502, 503],
                      allowed_methods=["GET"])
        self.s.mount("https://", HTTPAdapter(max_retries=retry))

    def _reconnect(self):
        """重建底层连接。代理多节点时, 一条连接会固定在同一个节点上:
        命中被 pixiv 拒绝的节点后, 必须重开连接才有机会换到好节点;
        而一旦成功, keep-alive 会一直复用好节点的连接。"""
        proxy = self.s.proxies.get("https") if self.s.proxies else ""
        headers = dict(self.s.headers)
        self.s.close()
        self.s = requests.Session()
        self.s.headers.update(headers)
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        retry = Retry(total=3, backoff_factor=2,
                      status_forcelist=[429, 500, 502, 503],
                      allowed_methods=["GET"])
        self.s.mount("https://", HTTPAdapter(max_retries=retry))

    def ajax(self, endpoint, params=None):
        """GET /ajax/{endpoint}, 返回 body。失败抛异常。
        遇到 400/401/403/429 视为可能连上了被拒节点, 重开连接重试。"""
        url = f"{PIXIV_ROOT}/ajax{endpoint}"
        last = None
        for i in range(4):
            try:
                r = self.s.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                last = e
                self._reconnect()
                time.sleep(3)
                continue
            if r.status_code in (400, 401, 403, 429) and i < 3:
                last = RuntimeError(f"HTTP {r.status_code}")
                self._reconnect()
                time.sleep(4)
                continue
            r.raise_for_status()
            data = r.json()
            if data.get("error"):
                raise RuntimeError(f"接口错误 {endpoint}: {data.get('message')}")
            time.sleep(self.delay)
            return data["body"]
        if last:
            raise last
        r.raise_for_status()

    def illust(self, ill_id):
        return self.ajax(f"/illust/{ill_id}")

    def pages(self, ill_id):
        return self.ajax(f"/illust/{ill_id}/pages")

    def ugoira_meta(self, ill_id):
        return self.ajax(f"/illust/{ill_id}/ugoira_meta")

    def bookmark_works(self, uid, tag="", rest="show", limit=100):
        """迭代收藏列表(仅插画/漫画)。
        2026-10 实测: 旧接口 illustmanga/bookmarks 已下线(404),
        现用 /ajax/user/{uid}/illusts/bookmarks, 需带全参数。"""
        offset = 0
        while True:
            body = self.ajax(f"/user/{uid}/illusts/bookmarks", params={
                "tag": tag, "offset": offset, "limit": limit,
                "rest": rest, "order": "desc", "mode": "all",
                "work_tag": "", "bm": "", "rdm": random.random()})
            works = body.get("works") or []
            if not works:
                return
            yield from works
            offset += limit
            try:
                total = int(body.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            if total and offset >= total:
                return

    def profile_illust_ids(self, uid):
        """用户主页全部作品 ID(插画+漫画)。"""
        body = self.ajax(f"/user/{uid}/profile/all")
        ids = list((body.get("illusts") or {}).keys())
        ids += list((body.get("manga") or {}).keys())
        return ids

    def _bookmark_page(self):
        return self.s.get(f"{PIXIV_ROOT}/bookmark.php", timeout=30)

    @staticmethod
    def _parse_uid(html):
        for pat in (r"user_id:'(\d+)'",          # dataLayer 单引号
                    r'\\"userId\\":\\"(\d+)\\"',  # 内嵌转义 JSON
                    r'"userId"\s*:\s*"(\d+)"'):
            m = re.search(pat, html)
            if m:
                return m.group(1)
        return None

    def own_user_id(self):
        """从收藏页 HTML 探测自己的用户 ID, 未登录返回 None。"""
        return self._parse_uid(self._bookmark_page().text)

    def check_login(self, attempts=15, gap=4):
        """校验登录状态。代理出口 IP 被 pixiv 拒绝时会表现为未登录,
        且代理节点切换时好时坏, 因此失败后重开连接重试直到命中可用节点。"""
        for i in range(attempts):
            html = self._bookmark_page().text
            login = re.search(r"login:'(yes|no)'", html)
            uid = self._parse_uid(html)
            if uid and (login is None or login.group(1) == "yes"):
                return uid
            if i < attempts - 1:
                self._reconnect()
                time.sleep(gap)
        raise RuntimeError(
            "多次尝试后 pixiv 仍认为未登录。你的 cookie 可能已过期, "
            "或当前代理节点的 IP 被 pixiv 屏蔽: 请在代理软件里切换到"
            "日本节点并固定住(不要用自动选择), 确认已填写代理, 再重试。")

    def download_file(self, url, dest: Path):
        """下载二进制文件(需要 Referer 防止 403)。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.s.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
            tmp.replace(dest)
        time.sleep(self.delay)


# ---------------------------------------------------------------- 下载逻辑

def sanitize(name, maxlen=60):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip().strip(".")
    return name[:maxlen] or "untitled"


class Downloader:
    def __init__(self, client: PixivClient, out_dir: Path, db: HistoryDB,
                 force=False, ugoira_format="gif"):
        self.client = client
        self.out_dir = out_dir
        self.db = db
        self.force = force
        self.ugoira_format = ugoira_format
        self.stats = {"downloaded": 0, "skipped": 0, "failed": 0}

    def _dest_path(self, base, ext, page=None, tagstr=""):
        """文件名: 作品ID_画师_标题[_p页码][_标签们].扩展名"""
        name = base
        if page is not None:
            name += f"_p{page}"
        if tagstr:
            name += f"_{tagstr}"
        return self.out_dir / f"{name}.{ext}"

    def _download_if_missing(self, url, dest: Path):
        if dest.exists() and dest.stat().st_size > 0:
            return False  # 磁盘上已有(上次中断残留), 跳过该文件
        self.client.download_file(url, dest)
        return True

    def download_illust(self, ill_id, index=None, total=None):
        """下载单个作品。成功返回 True, 已下载过返回 None, 失败返回 False。"""
        ill_id = str(ill_id)
        prefix = f"[{index}/{total}] " if index else ""
        try:
            if not self.force and self.db.has(ill_id):
                self.stats["skipped"] += 1
                log(f"{prefix}跳过 {ill_id} (历史记录中已下载)")
                return None

            info = self.client.illust(ill_id)
            title = info.get("title") or "untitled"
            author = info.get("userName") or "unknown"
            ill_type = info.get("illustType")  # 0插画 1漫画 2动图
            base = sanitize(f"{ill_id}_{author}_{title}")
            # 标签串: 优先中文翻译名, 加到文件名末尾
            tag_list = (info.get("tags") or {}).get("tags") or []
            names = [t.get("translatedName") or t.get("tag") or ""
                     for t in tag_list]
            names = [sanitize(n, 30) for n in names if n]
            tagstr = "_".join(names[:8]) if names else ""
            tagstr = sanitize(tagstr, 90)
            log(f"{prefix}下载 {ill_id} 《{title}》 by {author}")

            if ill_type == 2:
                saved = self._download_ugoira(ill_id, base, tagstr)
            else:
                saved = self._download_static(ill_id, base, tagstr)

            self.db.add(ill_id, title, author, saved)
            self.stats["downloaded"] += 1
            return True
        except Exception as e:
            self.stats["failed"] += 1
            log(f"{prefix}! 下载 {ill_id} 失败: {type(e).__name__}: {e}")
            return False

    def _download_static(self, ill_id, base, tagstr=""):
        pages = self.client.pages(ill_id)
        for i, page in enumerate(pages):
            url = page["urls"]["original"]
            ext = os.path.splitext(url)[1].lstrip(".") or "jpg"
            dest = self._dest_path(base, ext,
                                   page=i if len(pages) > 1 else None,
                                   tagstr=tagstr)
            if self._download_if_missing(url, dest):
                log(f"  已保存 {dest.name}")
            else:
                log(f"  已存在 {dest.name}")

    def _download_ugoira(self, ill_id, base, tagstr=""):
        if self.ugoira_format == "gif" and Image is None:
            raise RuntimeError("未安装 Pillow, 无法合成 GIF (pip install Pillow)")
        meta = self.client.ugoira_meta(ill_id)
        zip_url = meta.get("originalSrc") or next(iter(
            (meta.get("zipUrls") or {}).values()), None)
        if not zip_url:
            raise RuntimeError("拿不到动图帧包地址")
        frames = meta.get("frames") or []

        if self.ugoira_format == "zip":
            dest = self._dest_path(base, "zip", tagstr=tagstr)
            if self._download_if_missing(zip_url, dest):
                log(f"  已保存 {dest.name}")
            return dest.name

        # 帧zip -> 逐帧 -> GIF (frames 给出每帧文件名与延时 ms)
        with tempfile.TemporaryDirectory() as td:
            zip_path = Path(td) / "ugoira.zip"
            self.client.download_file(zip_url, zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(td)
            imgs, durations = [], []
            for fr in frames:
                fp = Path(td) / fr["file"]
                if not fp.exists():
                    raise RuntimeError(f"帧包缺少 {fr['file']}")
                imgs.append(Image.open(fp).convert("RGB"))
                durations.append(int(fr.get("delay", 100)))
            if not imgs:
                raise RuntimeError("帧包为空")
            dest = self._dest_path(base, "gif", tagstr=tagstr)
            imgs[0].save(dest, save_all=True, append_images=imgs[1:],
                         duration=durations, loop=0)
            log(f"  已合成 GIF ({len(imgs)} 帧): {dest.name}")
            return dest.name


# ---------------------------------------------------------------- 链接解析

RE_ILLUST = re.compile(
    r"(?:pixiv\.net/(?:en/)?artworks/|pixiv\.net/(?:en/)?illust/index\.php\?illust_id="
    r"|pixiv\.cc/\d+/|/img/(?:\w+/)+)(\d+)")
RE_USER = re.compile(r"pixiv\.net/(?:en/)?users/(\d+)")
RE_PURE_ID = re.compile(r"^\d{5,}$")


def parse_targets(tokens):
    """把命令行里的链接/ID 解析成 [('illust'|'user', id)]。"""
    targets = []
    for tok in tokens:
        tok = tok.strip().strip('"')
        if not tok:
            continue
        if RE_PURE_ID.match(tok):
            targets.append(("illust", tok))
            continue
        mu, mi = RE_USER.search(tok), RE_ILLUST.search(tok)
        if mu:
            targets.append(("user", mu.group(1)))
        elif mi:
            targets.append(("illust", mi.group(1)))
        else:
            log(f"! 无法识别的链接, 已忽略: {tok}")
    return targets


# ---------------------------------------------------------------- 子命令

def cmd_links(args, cfg, client, db):
    out_dir = Path(args.out or cfg["output_dir"])
    dl = Downloader(client, out_dir, db, force=args.force,
                    ugoira_format=cfg.get("ugoira_format", "gif"))
    targets = parse_targets(args.urls)
    if not targets:
        log("没有可识别的链接或 ID。支持: 作品页链接 / 作品纯ID / 用户主页链接")
        sys.exit(2)
    log(f"输出目录: {out_dir.resolve()}")
    # 逐个处理; 用户链接会展开为其全部作品
    queue = []
    for kind, tid in targets:
        if kind == "user":
            log(f"展开用户 {tid} 的全部作品 ...")
            queue.extend(client.profile_illust_ids(tid))
        else:
            queue.append(tid)
    log(f"共 {len(queue)} 个作品待处理")
    for i, ill_id in enumerate(queue, 1):
        dl.download_illust(ill_id, index=i, total=len(queue))
    _report(dl)


def cmd_bookmarks(args, cfg, client, db):
    uid = args.user or cfg.get("user_id") or ""
    if not uid:
        # 只有 ID 未知时才探测(顺便校验登录);
        # ID 已知则直接拉收藏列表, 避开不稳定的 bookmark.php 探测
        uid = client.check_login()
        cfg["user_id"] = uid
        save_config(cfg)
        log(f"用户 ID: {uid}")
    if not uid:
        log("无法确定用户 ID。请打开 pixiv 个人主页, 把链接里的数字 ID "
            "填入 config.json 的 user_id, 或用 --user 参数指定。")
        sys.exit(1)
    out_dir = Path(args.out or cfg["output_dir"])
    dl = Downloader(client, out_dir, db, force=args.force,
                    ugoira_format=cfg.get("ugoira_format", "gif"))
    rest = "hide" if args.private else "show"
    log(f"输出目录: {out_dir.resolve()}")
    log(f"拉取用户 {uid} 的{'私密' if args.private else '公开'}收藏 ...")
    ids = [w["id"] for w in client.bookmark_works(uid, tag=args.tag or "",
                                                  rest=rest)]
    log(f"收藏共 {len(ids)} 个作品, 开始下载 (已下载过的会自动跳过)")
    for i, ill_id in enumerate(ids, 1):
        dl.download_illust(ill_id, index=i, total=len(ids))
    _report(dl)


def _report(dl):
    s = dl.stats
    log(f"完成: 新下载 {s['downloaded']}, 跳过 {s['skipped']}, 失败 {s['failed']}")
    if s["failed"]:
        sys.exit(3)


def cmd_history(args, cfg, client, db):
    if args.history_cmd == "count":
        log(f"下载历史共 {db.count()} 条 (数据库: "
            f"{APP_DIR / 'download_history.db'})")
    elif args.history_cmd == "clear":
        if input("确认清空全部下载历史? 之后重跑会重新下载 (y/N): ").lower() == "y":
            db.clear()
            log("已清空。")
    elif args.history_cmd == "forget":
        db.remove(args.ill_id)
        log(f"已从历史移除 {args.ill_id}, 下次运行会重新下载该作品。")


def cmd_show_config(args, cfg, client, db):
    shown = dict(cfg)
    if shown.get("phpsessid"):
        shown["phpsessid"] = shown["phpsessid"][:8] + "..."
    log(json.dumps(shown, ensure_ascii=False, indent=2))
    log(f"\nconfig 位置: {CONFIG_PATH}")


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="pixiv_dl",
        description="Pixiv 批量下载工具: 支持分享链接批量下载 / 收藏批量下载 / "
                    "动图合成GIF / 下载历史去重")
    parser.add_argument("--out", help="输出目录(默认取 config.json 的 output_dir)")
    parser.add_argument("--force", action="store_true",
                        help="忽略下载历史, 强制重新下载")
    parser.add_argument("--delay", type=float,
                        help="每次请求间隔秒数(默认 1.0, 太小易被限流)")
    parser.add_argument("--proxy",
                        help="HTTP 代理地址, 如 http://127.0.0.1:7890 "
                             "(手机端访问 pixiv 一般需要)")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_links = sub.add_parser("links", help="批量下载分享链接(作品链接/作品ID/用户主页)")
    p_links.add_argument("urls", nargs="+", help="一个或多个链接/ID")

    p_bm = sub.add_parser("bookmarks", help="批量下载自己账号的收藏")
    p_bm.add_argument("--user", help="用户ID(默认自动探测/读 config.json)")
    p_bm.add_argument("--tag", help="只下载指定标签的收藏")
    p_bm.add_argument("--private", action="store_true", help="下载私密收藏")

    p_h = sub.add_parser("history", help="管理下载历史")
    p_h.add_argument("history_cmd", choices=["count", "clear", "forget"])
    p_h.add_argument("ill_id", nargs="?", help="forget 子命令: 要移除的作品ID")

    sub.add_parser("config", help="查看配置")

    args = parser.parse_args()
    cfg = load_config()
    if args.delay is not None:
        cfg["delay"] = args.delay
    if args.proxy:
        cfg["proxy"] = args.proxy

    if args.cmd in ("config", "history"):
        # 纯本地操作, 无需登录
        if args.cmd == "config":
            cmd_show_config(args, cfg, None, None)
        else:
            cmd_history(args, cfg, None,
                        HistoryDB(APP_DIR / "download_history.db"))
        return

    sessid = ensure_login(cfg)
    client = PixivClient(sessid, delay=cfg.get("delay", 1.0),
                         proxy=cfg.get("proxy", ""))
    db = HistoryDB(APP_DIR / "download_history.db")

    if args.cmd == "links":
        cmd_links(args, cfg, client, db)
    elif args.cmd == "bookmarks":
        cmd_bookmarks(args, cfg, client, db)


if __name__ == "__main__":
    main()
