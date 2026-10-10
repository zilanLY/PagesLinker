PagesLinker
===========

Cloudflare Pages 资源直链打包器（纯静态方案，Windows EXE）。

功能
----
- 输入：文件夹 / zip / 多文件（拖拽或点击选择，可混合）
- 输出：可直接部署到 Cloudflare Pages 的纯静态站点（`_site/` 目录 + `site.zip`）
- 大文件自动分片（默认 20 MiB/片，单文件硬上限 25 MiB），重组页在浏览器内拼接还原
- 内容去重、中文文件名保留、URL 自动 percent-encoding
- 无 Pages Functions、无 R2、无任何服务端逻辑，完全离线

用法（源码）
----
    python src/app.py                  # GUI（需 tkinter + tkinterdnd2）
    python src/pageslinker.py --cli <路径...>   # 命令行模式（无 GUI 依赖）
    python src/selftest.py             # 端到端自测

CI
----
GitHub Actions 自动测试并编译单文件 EXE（Windows），产物见 Actions →
PagesLinker-windows artifact。

本地打包
----
    pip install -r requirements.txt
    cd src
    pyinstaller --onefile --windowed --name PagesLinker --collect-all tkinterdnd2 app.py
