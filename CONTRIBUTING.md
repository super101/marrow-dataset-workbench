# 参与项目

欢迎通过 GitHub Issues 报告问题、讨论格式兼容和复核体验。请描述操作步骤、系统/Python/浏览器版本、预期与实际结果；使用合成图复现，移除真实图像、标注、身份、令牌和私有路径。

开发请先阅读 AGENTS.md、docs/PRODUCT_DESIGN.md 和 docs/IMPLEMENTATION_CONTRACT.md。原始文件只读、身份有据、修改可追溯、同源同组、未知对象不改成背景是基本约束。算法建议须区分模型输出与人工真值。

建立独立 Python 3.11+ 虚拟环境，运行 `python -m pip install -e .`。验证用 `python -X utf8 -m unittest discover -s tests -v`；前端修改还需在浏览器检查持久化、失败状态和窄屏。新增测试只生成合成数据，数据与产物放在仓库外。

提交前检查 Git 文件清单，不提交工作数据库、实际图像/XML、私有导出记录、缓存或环境。PR 说明具体变化和验证结果，未完成能力清楚标记；首版是本地个人工具，远程协作和真实数据发布需要独立设计。
