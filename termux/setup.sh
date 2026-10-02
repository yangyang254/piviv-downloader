#!/data/data/com.termux/files/usr/bin/bash
# Termux 一键安装: 把本目录拷到手机后, 在 Termux 里 cd 到这里执行 bash setup.sh
set -e
echo "== [1/2] 从 Termux 源安装 Python 与 Pillow =="
pkg install -y python python-pillow
echo "== [2/2] 安装 requests =="
pip install -U requests
echo
echo "安装完成! 使用方法:"
echo "  python pixiv_dl.py bookmarks                        # 下载全部收藏"
echo "  python pixiv_dl.py links <作品链接或ID> ...          # 按链接下载"
echo "  python pixiv_dl.py bookmarks --proxy http://127.0.0.1:7890"
echo "  (手机访问 pixiv 需要代理; 把 7890 换成你代理软件的本地端口)"
