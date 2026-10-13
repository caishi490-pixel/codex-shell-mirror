@echo off
rem Codex Shell Mirror — 手动启动 / 开机自启用
rem 双击可直接运行；开机自启：Win+R 输入 shell:startup，
rem 把本文件的快捷方式放进打开的文件夹即可。
rem 若提示找不到 pythonw，请把下面的 pythonw 换成你机器上 Python 的完整路径。
start "" pythonw "%~dp0..\codex_shell_mirror.py"
