@echo off
REM 打包两个 exe (需要已 pip install pyinstaller)
cd /d "%~dp0"
echo [1/2] 打包命令行版...
pyinstaller --onefile --console --noconfirm --name pixiv_dl pixiv_dl.py
echo [2/2] 打包图形界面版...
pyinstaller --onefile --windowed --noconfirm --name pixiv_dl_gui pixiv_gui.py
echo.
echo 完成: dist\pixiv_dl.exe (命令行)  dist\pixiv_dl_gui.exe (图形界面)
pause
