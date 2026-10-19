# Marrow Dataset Workbench

以复核为核心的本地骨髓显微图像数据工作台：保留原始资料，修订矩形框和标签，建立类别映射及分组划分，导出可追溯的检测与分类训练包。

**代码仓库与公开演示不包含真实医院数据。数据、草稿、身份表和导出结果均放在仓库之外。** 自动检查不能替代专业标注复核、研究授权或独立测试设计。

## 本地启动

需要 Python 3.11+，无需 Node.js、GPU 或云服务。Windows 建议明确使用 Python Launcher 的 `py -3.11`，避免默认 `python` 别名指向其他环境。

下载或克隆项目后，在仓库目录执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\start-workbench.cmd -Demo
```

首次安装依赖需要访问软件源；启动器不会静默安装任何依赖。启动后打开 `http://127.0.0.1:8765`，演示只生成几何合成图。

无 `py` 命令时，用已安装的 Python 3.11+ 完整路径替换，例如：

```powershell
& 'C:\Path\To\Python311\python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\start-workbench.cmd -Demo
```

真实数据需要由使用者明确登记可读取的根目录：

```powershell
.\start-workbench.cmd -Workspace 'D:\ResearchWork\marrow' -AllowRoot 'D:\ResearchData'
```

默认工作空间为用户目录下的 `.marrow-workbench`；可用 `MARROW_WORKSPACE` 和 `MARROW_ALLOW_ROOT` 配置。启动器参数及多根目录、备份和故障说明见[使用手册](docs/USER_GUIDE.md)。程序只监听本机，不是可直接开放公网的服务器。

## 工作流程

1. 导入旧入库工具的工作集，或导入图像与 VOC/XML；选择明确的源坐标约定和标签字典。
2. 在原图上改框、改标签、直接软删除；备注选填，草稿自动保存。点击“完成复核”即可记录复核状态，无须勾选完整性或填写理由。
3. 固定训练类别映射；按实际情况记录研究使用依据和标注范围，缺失信息在导出报告中提示。
4. 根据有证据的患者/玻片/来源关系分组，再划分原图；分类裁剪继承父图划分。
5. 运行导出检查，选择已复核版或基础检查版，生成 VOC、COCO、YOLO 检测包和单细胞分类包。

复核状态和技术问题分别记录：保留问题备注也可完成复核，软件自动记录修改历史。已复核版选择已复核图，基础检查版允许尚未逐图复核；两档都检查有效框、可输出类别、文件完整性和跨集合泄漏。使用依据、完整性说明及复核、框删除理由不设必填，报告如实列出未记录事项。研究者负责按实际制度判断数据用途。普通训练器无法正确处理 ignore 时，相关整图暂缓纳入；分类裁剪单独判断纳入。

首版是科研数据准备工具，不训练模型、不生成医学真值；不自动发现全部近重复或重叠视野，不提供真实数据 GitHub 上传、远程多人协作或公网认证。公开网站为合成演示与下载入口，与本地完整版分开。设计范围及验收记录分别见[产品方案](docs/PRODUCT_DESIGN.md)和[发布检查表](docs/RELEASE_CHECKLIST.md)；文档中的设计不等于已经通过验收。

## 安装 Codex skill 和角色

网页工作台可独立使用。需要 Codex 辅助入库时，另行执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 install.py
```

安装器将 `marrow-dataset-intake` 和三个角色配置安装至 `CODEX_HOME`（默认 `~/.codex`），备份已有配置，只添加缺失角色，遇同名 skill 或角色停止。重新打开 Codex 会话后调用：

```text
用 $marrow-dataset-intake 整理显微图像与 VOC 标注，保留原始数据，核对标签与来源，生成独立工作版本。
```

角色为 `marrow-data-curator`、`marrow-label-reviewer`、`marrow-grouping-viewer`，继承当前模型，不另装模型或启动常驻服务。职责见 [AGENTS.md](AGENTS.md)。

原命令行入库工具仍保留：

```powershell
.\.venv\Scripts\python.exe skills/marrow-dataset-intake/scripts/intake.py --help
.\.venv\Scripts\python.exe skills/marrow-dataset-intake/scripts/verify.py --help
```

标签 CSV 为 `code,name,role`，编码按字符串保留。规则见[入库标准](skills/marrow-dataset-intake/references/intake-standard.md)。旧工作集中的原始 XML 可能保留设备路径，不能直接拿去公开发布。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m workbench --help
```

测试使用临时目录和合成图。贡献仅提交代码、通用文档和合成测试；真实图像、标注、身份表及个体文件哈希不进入 Git 历史。MIT 许可适用于本项目代码，不授予外部数据使用权。
