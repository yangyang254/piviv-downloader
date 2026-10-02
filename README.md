# pixiv_dl — Pixiv 批量下载命令行工具

参考了 GitHub 上成熟实现（[gallery-dl](https://github.com/mikf/gallery-dl)、
[PixivBatchDownloader](https://github.com/xuejianxianzun/PixivBatchDownloader)）的接口用法，
基于 Pixiv 网页版 ajax 接口实现。

## 重要说明

- **AI 辅助开发**：本项目使用 ZCode（AI 编程助手）辅助完成开发，
  包括代码编写、接口调研与测试。
- **隐私声明**：本项目**不收集任何数据、不上传任何信息**，没有统计、没有遥测。
  你的登录凭证（PHPSESSID）只保存在本机 `config.json`，仅用于向 pixiv 官方接口
  发送请求；下载历史 `download_history.db` 也只存在本地。这两个文件包含个人隐私，
  已被 `.gitignore` 排除，**永远不会被提交到本仓库**。
- **接口风险**：本项目依赖 pixiv 网页版的 ajax 接口（非官方公开 API），**pixiv
  随时可能改版导致功能损坏**（开发过程中就实际遇到过收藏接口下线的情况）。
  接口失效时欢迎提 issue，也欢迎 PR。
- **使用限制**：仅供个人学习交流使用，请尊重画师的著作权，勿用于商业用途，
  不要二次分发下载的作品。
- 已打包的 Windows exe 不随仓库分发，请按下文用 `build.bat` 自行打包；
  Termux 手机包 `pixiv-phone.zip` 已附带在本仓库。

## 功能

1. **分享链接批量下载**：`links` 命令，支持作品链接、纯作品 ID、用户主页链接（展开其全部作品），一次可给多个
2. **收藏批量下载**：`bookmarks` 命令，拉取自己账号的全部收藏（默认公开收藏，`--private` 为私密收藏）
3. **动图自动合成 GIF**：动图(ugoira)下载原始帧 zip 后按每帧延时合成 `.gif`
4. **下载历史去重**：所有下载过的作品 ID 记录在 exe 旁边的 `download_history.db`（SQLite）里。
   **即使你把下载好的文件移走，下次运行也不会重复下载**。想强制重下用 `--force`。

## 使用方法

### 1. 登录

第一次运行任何下载命令时，会引导你输入 PHPSESSID：

1. 浏览器登录 https://www.pixiv.net
2. 按 `F12` → 应用(Application) → Cookie → `https://www.pixiv.net`
3. 复制 `PHPSESSID` 的值，粘贴回车。会自动保存到同目录 `config.json`

### 2. 下载分享链接

```
pixiv_dl links https://www.pixiv.net/artworks/12345678
pixiv_dl links 12345678 https://www.pix.net/users/99999/artworks/111 ...
```

- 作品链接或纯数字 ID：下载该作品全部页
- 用户主页链接：下载该画师全部作品
- 可以一次给多个，空格分隔

### 3. 下载自己的收藏

```
pixiv_dl bookmarks            # 全部公开收藏
pixiv_dl bookmarks --private  # 私密收藏
pixiv_dl bookmarks --tag ROC  # 只下载指定标签
```

用户 ID 会自动探测；失败时打开 pixiv 个人主页，把链接里的数字填入
`config.json` 的 `user_id`。

### 4. 下载历史管理

```
pixiv_dl history count          # 看已下载多少
pixiv_dl history forget 12345   # 让某个作品下次重新下载
pixiv_dl history clear          # 清空历史(会重新下载全部)
pixiv_dl links ... --force      # 本次忽略历史强制重下
```

### 5. 配置（config.json）

| 字段 | 说明 | 默认 |
|---|---|---|
| `phpsessid` | 登录 cookie | 自动引导输入 |
| `user_id` | 自己的用户 ID | 自动探测 |
| `output_dir` | 输出目录（相对运行目录） | `downloads` |
| `delay` | 请求间隔秒数，太小易被限流 | `1.0` |
| `ugoira_format` | 动图保存格式：`gif` 或 `zip` | `gif` |

也可以用 `pixiv_dl --out D:\pic bookmarks` 临时指定输出目录。

## 文件命名

- 普通作品：`作品ID_画师_标题[_p页码]_标签1_标签2....jpg/png`
- 动图：`作品ID_画师_标题_标签1_标签2....gif`
- 标签在文件名末尾（扩展名之前），优先使用翻译名（中文），最多取前 8 个，
  单个标签里的非法字符会转成 `_`

## 打包 exe

```
pip install -r requirements.txt pyinstaller
build.bat
```

生成两个单文件 exe：

- **`dist\pixiv_dl_gui.exe` — 图形界面版**（推荐日常使用）
- `dist\pixiv_dl.exe` — 命令行版

两个 exe **放在同一个目录使用**即可共享 `config.json` 和 `download_history.db`
（登录信息、下载历史通用；它们会生成在 exe 所在目录）。

## 图形界面版使用

双击打开 `pixiv_dl_gui.exe`：

1. **设置区**：粘贴 PHPSESSID（见上文登录方法）、选输出目录、调请求间隔、
   选动图保存格式。设置会自动保存
2. **按链接下载** 标签页：每行粘贴一个作品链接 / 作品ID / 画师主页链接
3. **下载我的收藏** 标签页：填标签过滤（可选），勾选私密收藏（可选）
4. 点 **▶ 开始下载**，进度条与日志实时显示；随时可 **■ 停止**，
   已下载部分已记入历史，重开不会重下
5. 右下角"已下载条数"可随时查看历史数量

## 注意

- 请求间隔默认 1 秒，收藏量大时需要耐心（几千个作品约 1 小时）。
- 出现 429 说明被限流了，把 `delay` 调大再继续即可（已下载的部分会自动跳过，不会重下）。
