# -*- coding: utf-8 -*-
"""
pixiv_gui - Pixiv 批量下载器图形界面 (tkinter)

复用 pixiv_dl.py 的下载逻辑: 链接批量下载 / 收藏批量下载 / 动图合成GIF /
下载历史去重。下载在后台线程执行, 通过队列把日志/进度送回主线程刷新 UI。
"""

import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from pathlib import Path

import pixiv_dl
from pixiv_dl import (APP_DIR, CONFIG_PATH, Downloader, HistoryDB,
                      PixivClient, load_config, parse_targets, save_config)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Pixiv 批量下载器")
        self.geometry("780x620")
        self.minsize(680, 520)

        self.cfg = load_config()
        self.log_q = queue.Queue()
        self.progress_q = queue.Queue()
        self.done_q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.dl = None

        self._build_ui()
        self.after(150, self._poll)

    # ------------------------------------------------------------ UI

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # --- 设置区
        top = ttk.LabelFrame(self, text="设置")
        top.pack(fill="x", **pad)

        ttk.Label(top, text="PHPSESSID:").grid(row=0, column=0, sticky="e", **pad)
        self.sessid_var = tk.StringVar(value=self.cfg.get("phpsessid", ""))
        self.sessid_entry = ttk.Entry(top, textvariable=self.sessid_var, show="*")
        self.sessid_entry.grid(row=0, column=1, sticky="we", **pad)
        ttk.Label(top, text="(浏览器F12→应用→Cookie→pixiv.net, 不留空则用已保存的)")\
            .grid(row=0, column=2, sticky="w")

        ttk.Label(top, text="输出目录:").grid(row=1, column=0, sticky="e", **pad)
        self.outdir_var = tk.StringVar(value=self.cfg.get("output_dir", "downloads"))
        ttk.Entry(top, textvariable=self.outdir_var).grid(row=1, column=1,
                                                          sticky="we", **pad)
        ttk.Button(top, text="浏览...", command=self._pick_dir)\
            .grid(row=1, column=2, sticky="w")

        ttk.Label(top, text="请求间隔(秒):").grid(row=2, column=0, sticky="e", **pad)
        self.delay_var = tk.StringVar(value=str(self.cfg.get("delay", 1.0)))
        ttk.Spinbox(top, from_=0.2, to=10, increment=0.5, width=6,
                    textvariable=self.delay_var).grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(top, text="动图保存为:").grid(row=3, column=0, sticky="e", **pad)
        self.ugoira_var = tk.StringVar(value=self.cfg.get("ugoira_format", "gif"))
        ttk.Combobox(top, textvariable=self.ugoira_var, state="readonly",
                     values=["gif", "zip"], width=6).grid(row=3, column=1,
                                                          sticky="w", **pad)
        top.columnconfigure(1, weight=1)

        # --- 任务区
        nb = ttk.Notebook(self)
        nb.pack(fill="x", **pad)

        tab1 = ttk.Frame(nb)
        nb.add(tab1, text=" 按链接下载 ")
        ttk.Label(tab1, text="每行一个作品链接 / 作品ID / 画师主页链接(下载其全部作品):")\
            .pack(anchor="w", padx=8, pady=(6, 0))
        self.links_text = tk.Text(tab1, height=6, wrap="word")
        self.links_text.pack(fill="x", padx=8, pady=6)

        tab2 = ttk.Frame(nb)
        nb.add(tab2, text=" 下载我的收藏 ")
        f2 = ttk.Frame(tab2)
        f2.pack(fill="x", padx=8, pady=10)
        ttk.Label(f2, text="用户ID(留空自动探测):").pack(side="left")
        self.bm_user_var = tk.StringVar(value=self.cfg.get("user_id", ""))
        ttk.Entry(f2, width=14, textvariable=self.bm_user_var).pack(side="left", padx=6)
        ttk.Label(f2, text="标签(可选):").pack(side="left", padx=(16, 0))
        self.bm_tag_var = tk.StringVar()
        ttk.Entry(f2, width=14, textvariable=self.bm_tag_var).pack(side="left", padx=6)
        self.bm_private_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f2, text="私密收藏", variable=self.bm_private_var)\
            .pack(side="left", padx=16)

        # --- 控制区
        ctrl = ttk.Frame(self)
        ctrl.pack(fill="x", **pad)
        self.start_btn = ttk.Button(ctrl, text="▶ 开始下载", command=self.start)
        self.start_btn.pack(side="left", padx=(8, 4))
        self.stop_btn = ttk.Button(ctrl, text="■ 停止", command=self.stop,
                                   state="disabled")
        self.stop_btn.pack(side="left")
        self.force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctrl, text="忽略历史强制重下",
                        variable=self.force_var).pack(side="left", padx=16)
        ttk.Button(ctrl, text="历史: 已下载条数", command=self._show_count)\
            .pack(side="right", padx=8)

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", **pad)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=10)

        # --- 日志区
        self.log_box = scrolledtext.ScrolledText(self, height=12, wrap="word",
                                                 state="disabled")
        self.log_box.pack(fill="both", expand=True, padx=8, pady=(2, 8))

    def _pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.outdir_var.get() or ".")
        if d:
            self.outdir_var.set(d)

    def _show_count(self):
        try:
            db = HistoryDB(APP_DIR / "download_history.db")
            n = db.count()
            db.close()
            messagebox.showinfo("下载历史",
                f"历史记录共 {n} 个作品。\n\n"
                f"数据库位置: {APP_DIR / 'download_history.db'}\n\n"
                "文件被移走也没关系, 记录过的作品不会重新下载。")
        except Exception as e:
            messagebox.showerror("错误", str(e))

    # ------------------------------------------------------------ 事件

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        # 收集并保存设置
        cfg = self._collect_config()
        out_dir = Path(self.outdir_var.get().strip() or "downloads")

        tab_index = self._current_tab()
        self.stop_event.clear()
        self.log_q.put("=== 任务开始 ===")
        if tab_index == 0:
            urls = self.links_text.get("1.0", "end").split()
            if not urls:
                messagebox.showwarning("提示", "请先输入至少一个链接或作品ID")
                return
            self.worker = threading.Thread(
                target=self._run_links, daemon=True,
                args=(cfg, urls, out_dir, self.force_var.get()))
        else:
            self.worker = threading.Thread(
                target=self._run_bookmarks, daemon=True,
                args=(cfg, out_dir, self.force_var.get()))
        self.dl = None
        self.progress["value"] = 0
        self.status_var.set("运行中...")
        self.start_btn["state"] = "disabled"
        self.stop_btn["state"] = "normal"
        self.worker.start()

    def stop(self):
        self.stop_event.set()
        self.status_var.set("停止中(等待当前文件完成)...")

    def _current_tab(self):
        for w in self.winfo_children():
            if isinstance(w, ttk.Notebook):
                return w.index(w.select())
        return 0

    def _collect_config(self):
        cfg = dict(self.cfg)
        cfg["phpsessid"] = self.sessid_var.get().strip()
        cfg["output_dir"] = self.outdir_var.get().strip()
        try:
            cfg["delay"] = max(0.2, float(self.delay_var.get()))
        except ValueError:
            cfg["delay"] = 1.0
        cfg["ugoira_format"] = self.ugoira_var.get()
        self.cfg = cfg
        save_config(cfg)  # 记住设置, 下次启动自动填充
        return cfg

    # ------------------------------------------------------------ 后台线程

    def _make_client(self, cfg):
        if not cfg["phpsessid"]:
            raise RuntimeError("请先在设置里填写 PHPSESSID "
                               "(浏览器F12→应用→Cookie→pixiv.net)")
        return PixivClient(cfg["phpsessid"], delay=cfg["delay"],
                           proxy=cfg.get("proxy", ""))

    def _run_links(self, cfg, urls, out_dir, force):
        pixiv_dl.set_logger(self.log_q.put)
        try:
            client = self._make_client(cfg)
            db = HistoryDB(APP_DIR / "download_history.db")
            dl = Downloader(client, out_dir, db, force=force,
                            ugoira_format=cfg["ugoira_format"])
            self.dl = dl
            targets = parse_targets(urls)
            if not targets:
                raise RuntimeError("没有可识别的链接或 ID")
            queue_ids = []
            for kind, tid in targets:
                if kind == "user":
                    pixiv_dl.log(f"展开画师 {tid} 的全部作品 ...")
                    queue_ids.extend(client.profile_illust_ids(tid))
                else:
                    queue_ids.append(tid)
            total = len(queue_ids)
            pixiv_dl.log(f"共 {total} 个作品待处理, 输出目录: {out_dir.resolve()}")
            self.progress_q.put(("total", total))
            for i, iid in enumerate(queue_ids, 1):
                if self.stop_event.is_set():
                    pixiv_dl.log("已停止 (已下载的部分已记入历史, 不会重下)")
                    break
                dl.download_illust(iid, index=i, total=total)
                self.progress_q.put(("step", i))
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
            db = HistoryDB(APP_DIR / "download_history.db")
            dl = Downloader(client, out_dir, db, force=force,
                            ugoira_format=cfg["ugoira_format"])
            self.dl = dl
            typed = self.bm_user_var.get().strip()
            uid = typed or cfg.get("user_id") or ""
            if not uid:
                # ID 未知时才探测(顺便校验登录); 已知则直接拉收藏列表
                uid = client.check_login()
            if not uid:
                raise RuntimeError("无法确定用户 ID, 请在收藏页填入用户ID "
                                   "(个人主页链接里的数字)")
            if typed != uid:
                pixiv_dl.log(f"使用用户 ID: {uid}")
                self.cfg["user_id"] = uid
                save_config(self.cfg)
            rest = "hide" if self.bm_private_var.get() else "show"
            tag = self.bm_tag_var.get().strip()
            pixiv_dl.log(f"拉取用户 {uid} 的收藏列表 ...")
            ids = [w["id"] for w in
                   client.bookmark_works(uid, tag=tag, rest=rest)]
            total = len(ids)
            pixiv_dl.log(f"收藏共 {total} 个作品, 输出目录: {out_dir.resolve()}")
            self.progress_q.put(("total", total))
            for i, iid in enumerate(ids, 1):
                if self.stop_event.is_set():
                    pixiv_dl.log("已停止 (已下载的部分已记入历史, 不会重下)")
                    break
                dl.download_illust(iid, index=i, total=total)
                self.progress_q.put(("step", i))
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

    def _poll(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                self.log_box["state"] = "normal"
                self.log_box.insert("end", str(msg) + "\n")
                self.log_box.see("end")
                self.log_box["state"] = "disabled"
        except queue.Empty:
            pass
        try:
            while True:
                kind, val = self.progress_q.get_nowait()
                if kind == "total":
                    self.progress["maximum"] = max(1, val)
                elif kind == "step":
                    self.progress["value"] = val
                    if self.dl:
                        s = self.dl.stats
                        self.status_var.set(
                            f"运行中... 新下载 {s['downloaded']} / "
                            f"跳过 {s['skipped']} / 失败 {s['failed']}")
        except queue.Empty:
            pass
        try:
            while True:
                self.done_q.get_nowait()
                self.start_btn["state"] = "normal"
                self.stop_btn["state"] = "disabled"
                self.status_var.set("就绪")
        except queue.Empty:
            pass
        self.after(150, self._poll)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
