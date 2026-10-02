# -*- coding: utf-8 -*-
"""
Pixiv 批量下载器 - 安卓版 (Kivy)

复用 pixiv_dl.py 的下载逻辑: 链接批量下载 / 收藏批量下载 / 动图合成GIF /
下载历史去重。下载在后台线程执行, 通过队列 + Clock 把日志/进度送回主线程。
"""

import os
import queue
import threading
from pathlib import Path

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput

import pixiv_dl
from pixiv_dl import (CONFIG_PATH, Downloader, HistoryDB, PixivClient,
                      load_config, parse_targets, save_config)

# 尝试申请存储权限(旧版安卓); 新版安卓如果写不进 /sdcard 会提示用户去开权限
try:
    from android.permissions import Permission, request_permissions
    request_permissions([Permission.WRITE_EXTERNAL_STORAGE,
                         Permission.READ_EXTERNAL_STORAGE])
except Exception:
    pass


def default_out_dir():
    if os.path.isdir("/sdcard/Download"):
        return "/sdcard/Download/pixiv"
    return str(Path(pixiv_dl.APP_DIR) / "downloads")


class PixivApp(App):
    title = "Pixiv 批量下载器"

    def build(self):
        self.cfg = load_config()
        self.log_q = queue.Queue()
        self.prog_q = queue.Queue()
        self.done_q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.dl = None

        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))

        # ---------- 滚动表单区
        sv = ScrollView()
        grid = GridLayout(cols=1, size_hint_y=None, spacing=dp(6))
        grid.bind(minimum_height=grid.setter("height"))
        sv.add_widget(grid)

        grid.add_widget(self._label("PHPSESSID (浏览器F12→应用→Cookie→pixiv.net, 填一次即可)"))
        self.sessid = TextInput(text=self.cfg.get("phpsessid", ""), password=True,
                                multiline=False, size_hint_y=None, height=dp(42))
        grid.add_widget(self.sessid)

        grid.add_widget(self._label("代理 (手机必需, 如 http://127.0.0.1:7890)"))
        self.proxy = TextInput(text=self.cfg.get("proxy", ""), multiline=False,
                               size_hint_y=None, height=dp(42))
        grid.add_widget(self.proxy)

        grid.add_widget(self._label("作品链接 / 作品ID (每行一个, 也可填画师主页链接)"))
        self.links = TextInput(hint_text="https://www.pixiv.net/artworks/123456",
                               multiline=True, size_hint_y=None, height=dp(90))
        grid.add_widget(self.links)

        row = GridLayout(cols=4, size_hint_y=None, height=dp(42), spacing=dp(6))
        row.add_widget(self._label("用户ID:"))
        self.bm_user = TextInput(text=self.cfg.get("user_id", ""),
                                 multiline=False)
        row.add_widget(self.bm_user)
        row.add_widget(self._label("标签:"))
        self.bm_tag = TextInput(multiline=False)
        row.add_widget(self.bm_tag)
        grid.add_widget(row)

        row2 = GridLayout(cols=4, size_hint_y=None, height=dp(42), spacing=dp(6))
        self.bm_private = CheckBox(size_hint_x=None, width=dp(40))
        row2.add_widget(self.bm_private)
        row2.add_widget(self._label("私密收藏"))
        self.force = CheckBox(size_hint_x=None, width=dp(40))
        row2.add_widget(self.force)
        row2.add_widget(self._label("忽略历史强制重下"))
        grid.add_widget(row2)

        btns = GridLayout(cols=3, size_hint_y=None, height=dp(50), spacing=dp(6))
        self.start_btn = Button(text="▶ 开始下载", bold=True)
        self.start_btn.bind(on_press=self.start)
        btns.add_widget(self.start_btn)
        self.stop_btn = Button(text="■ 停止", disabled=True)
        self.stop_btn.bind(on_press=self.stop)
        btns.add_widget(self.stop_btn)
        count_btn = Button(text="已下载条数")
        count_btn.bind(on_press=self.show_count)
        btns.add_widget(count_btn)
        grid.add_widget(btns)
        grid.add_widget(self._label(
            f"文件保存于: {default_out_dir()}\n"
            "(若保存失败, 需在系统设置中给本应用授予'所有文件'权限)"))

        root.add_widget(sv, index=0)

        # ---------- 进度与日志
        self.progress = ProgressBar(max=1, size_hint_y=None, height=dp(18))
        root.add_widget(self.progress)
        self.status = self._label("就绪", color=(0.3, 0.3, 0.3, 1))
        root.add_widget(self.status)

        log_sv = ScrollView(size_hint=(1, 1))
        self.log_label = Label(text="", size_hint_y=None, halign="left",
                               valign="top", font_size=dp(12),
                               text_size=(Window.width - dp(24), None))
        self.log_label.bind(texture_size=lambda o, v:
                            setattr(o, "height", v[1]))
        log_sv.add_widget(self.log_label)
        root.add_widget(log_sv)

        Clock.schedule_interval(self._poll, 0.2)
        return root

    @staticmethod
    def _label(text, **kw):
        return Label(text=text, size_hint_y=None, height=dp(20),
                     halign="left", text_size=(Window.width - dp(24), None), **kw)

    # ------------------------------------------------------------ 事件

    def start(self, *_):
        if self.worker and self.worker.is_alive():
            return
        cfg = dict(self.cfg)
        cfg["phpsessid"] = self.sessid.text.strip()
        cfg["proxy"] = self.proxy.text.strip()
        cfg["user_id"] = self.bm_user.text.strip()
        cfg["output_dir"] = default_out_dir()
        self.cfg = cfg
        save_config(cfg)

        out_dir = Path(cfg["output_dir"])
        self.stop_event.clear()
        self._append_log("=== 任务开始 ===")
        # 简化交互: 链接框有内容就按链接下载, 否则下载收藏
        urls = self.links.text.split()
        if urls:
            self.worker = threading.Thread(
                target=self._run_links, daemon=True,
                args=(cfg, urls, out_dir, self.force.active))
        else:
            self.worker = threading.Thread(
                target=self._run_bookmarks, daemon=True,
                args=(cfg, out_dir, self.force_var()))
        self.dl = None
        self.progress.value = 0
        self.status.text = "运行中..."
        self.start_btn.disabled = True
        self.stop_btn.disabled = False
        self.worker.start()

    def force_var(self):
        return self.force.active

    def stop(self, *_):
        self.stop_event.set()
        self.status.text = "停止中(等待当前文件完成)..."

    def show_count(self, *_):
        try:
            db = HistoryDB(Path(pixiv_dl.APP_DIR) / "download_history.db")
            n = db.count()
            db.close()
        except Exception as e:
            n = f"读取失败: {e}"
        self._append_log(f"下载历史共 {n} 个作品 "
                         f"(数据库: {pixiv_dl.APP_DIR})")

    # ------------------------------------------------------------ 后台线程

    def _make_client(self, cfg):
        if not cfg["phpsessid"]:
            raise RuntimeError("请先填写 PHPSESSID "
                               "(浏览器F12→应用→Cookie→pixiv.net)")
        return PixivClient(cfg["phpsessid"], delay=cfg.get("delay", 1.0),
                           proxy=cfg.get("proxy", ""))

    def _run_links(self, cfg, urls, out_dir, force):
        pixiv_dl.set_logger(self.log_q.put)
        try:
            client = self._make_client(cfg)
            db = HistoryDB(Path(pixiv_dl.APP_DIR) / "download_history.db")
            dl = Downloader(client, out_dir, db, force=force,
                            ugoira_format="gif")
            self.dl = dl
            targets = parse_targets(urls)
            if not targets:
                raise RuntimeError("没有可识别的链接或 ID")
            ids = []
            for kind, tid in targets:
                if kind == "user":
                    pixiv_dl.log(f"展开画师 {tid} 的全部作品 ...")
                    ids.extend(client.profile_illust_ids(tid))
                else:
                    ids.append(tid)
            total = len(ids)
            pixiv_dl.log(f"共 {total} 个作品, 输出目录: {out_dir}")
            self.prog_q.put(("total", total))
            for i, iid in enumerate(ids, 1):
                if self.stop_event.is_set():
                    pixiv_dl.log("已停止 (已下载部分已记入历史, 不会重下)")
                    break
                dl.download_illust(iid, index=i, total=total)
                self.prog_q.put(("step", i))
            self._summary(dl)
        except Exception as e:
            pixiv_dl.log(f"出错: {type(e).__name__}: {e}")
        finally:
            pixiv_dl.set_logger(None)
            self.done_q.put(True)

    def _run_bookmarks(self, cfg, out_dir, force):
        pixiv_dl.set_logger(self.log_q.put)
        try:
            client = self._make_client(cfg)
            db = HistoryDB(Path(pixiv_dl.APP_DIR) / "download_history.db")
            dl = Downloader(client, out_dir, db, force=force,
                            ugoira_format="gif")
            self.dl = dl
            typed = self.bm_user.text.strip()
            uid = typed or cfg.get("user_id") or ""
            if not uid:
                # ID 未知时才探测(顺便校验登录); 已知则直接拉收藏列表
                uid = client.check_login()
            if not uid:
                raise RuntimeError("无法确定用户 ID, 请填入用户ID "
                                   "(个人主页链接里的数字)")
            if typed != uid:
                pixiv_dl.log(f"使用用户 ID: {uid}")
                self.cfg["user_id"] = uid
                save_config(self.cfg)
            rest = "hide" if self.bm_private.active else "show"
            tag = self.bm_tag.text.strip()
            pixiv_dl.log(f"拉取用户 {uid} 的收藏列表 ...")
            ids = [w["id"] for w in client.bookmark_works(uid, tag=tag,
                                                          rest=rest)]
            total = len(ids)
            pixiv_dl.log(f"收藏共 {total} 个作品, 输出目录: {out_dir}")
            self.prog_q.put(("total", total))
            for i, iid in enumerate(ids, 1):
                if self.stop_event.is_set():
                    pixiv_dl.log("已停止 (已下载部分已记入历史, 不会重下)")
                    break
                dl.download_illust(iid, index=i, total=total)
                self.prog_q.put(("step", i))
            self._summary(dl)
        except Exception as e:
            pixiv_dl.log(f"出错: {type(e).__name__}: {e}")
        finally:
            pixiv_dl.set_logger(None)
            self.done_q.put(True)

    @staticmethod
    def _summary(dl):
        if dl is None:
            return
        s = dl.stats
        pixiv_dl.log(f"=== 完成: 新下载 {s['downloaded']}, "
                     f"跳过 {s['skipped']}, 失败 {s['failed']} ===")

    # ------------------------------------------------------------ 主线程轮询

    def _poll(self, *_):
        try:
            while True:
                self._append_log(str(self.log_q.get_nowait()))
        except queue.Empty:
            pass
        try:
            while True:
                kind, val = self.prog_q.get_nowait()
                if kind == "total":
                    self.progress.max = max(1, val)
                elif kind == "step":
                    self.progress.value = val
                    if self.dl:
                        s = self.dl.stats
                        self.status.text = (f"新下载 {s['downloaded']} / "
                                            f"跳过 {s['skipped']} / "
                                            f"失败 {s['failed']}")
        except queue.Empty:
            pass
        try:
            while True:
                self.done_q.get_nowait()
                self.start_btn.disabled = False
                self.stop_btn.disabled = True
                self.status.text = "就绪"
        except queue.Empty:
            pass

    def _append_log(self, msg):
        self.log_label.text += msg + "\n"


if __name__ == "__main__":
    PixivApp().run()
