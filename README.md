# Marrow Dataset Workbench

骨髓显微图像数据的本地入库工具与 Codex skill。用于保留原始数据、建立独立工作副本、检查 VOC 图像/XML、核对标签字典并记录来源与后续分组要求。

**仓库仅提供代码、标准和合成测试，不包含医院数据，也不提供研究使用授权或医学真值。**所有实际数据和生成结果放在仓库之外。入库默认状态为 `provisional`，未解决编码不会被静默删除或自动改成背景。

## 安装 skill

在仓库目录用 Python 3.11+ 运行安装器：

```console
python -X utf8 install.py
```

安装器将 skill 及三个角色配置安装到 `CODEX_HOME`（未设置时为 `~/.codex`），先备份现有配置，只添加缺失角色；如发现同名 skill 或角色则停止，避免覆盖。角色为 `marrow-data-curator`、`marrow-label-reviewer`、`marrow-grouping-viewer`。它们继承当前模型，不下载独立模型，不启动后台进程。

也可以只将 `skills/marrow-dataset-intake` 整个目录复制到 Codex 的 skills 目录。下一轮会话发现 skill；重开 Codex 会话以加载新角色。调用：

```text
用 $marrow-dataset-intake 整理这批骨髓图像，保留原始数据，检查字典和来源，生成工作副本及问题清单。
```

安装后 skill 目录中包含运行脚本与标准，单独安装也能使用。`agents/openai.yaml` 提供 skill 的界面名称和调用提示；[AGENTS.md](AGENTS.md) 定义数据整理、标注质检、分组展示三种协作职责。它们不安装独立模型或后台服务。

## 命令行使用

使用 Python 3.11 或以上版本；入库本身不要求联网或云算力。可选安装 Pillow 以检查解码像素重复并生成缩略图：`python -m pip install Pillow`。无 Pillow 时仍可独立复制与检查文件级重复，并明确报告像素检查缺失。

```console
python skills/marrow-dataset-intake/scripts/intake.py --help
python skills/marrow-dataset-intake/scripts/intake.py --source /data/incoming/VOC2007 --dictionary /data/label_dictionary.csv --output /data/work/dataset-v1
python skills/marrow-dataset-intake/scripts/intake.py --source /data/incoming/VOC2007 --dictionary /data/label_dictionary.csv --output /data/work/dataset-v2 --thumbnails
python skills/marrow-dataset-intake/scripts/verify.py --source /data/incoming/VOC2007 --workset /data/work/dataset-v1
```

可用 `--metadata /data/identity_metadata.csv` 补充已知身份。以 `--help` 和脚本输出的模板为准；缺失患者/玻片信息留空，不以文件名冒充身份。输出必须是源目录之外尚不存在的新目录。

标签 CSV 最少包含以下字段，编码按字符串保留：

```csv
code,name,role
demo_a,示例细胞类别,cell
demo_ignore,明确忽略对象,ignore
demo_pending,待复核对象,unresolved
```

`ignore` 要在实际训练/评估框架中实现，不是重命名标签即可。工具完整保留未定义和未解决对象供复核；医学类别仍需专业人员确认。

## 输出和后续工作

入库建立独立工作副本，并产生 `image_manifest.csv`、`object_manifest.csv`、`label_counts.csv`、`source_snapshot.csv`、`identity_to_complete.csv`、`review/issues.csv` 和 `summary.json`。`browse.html` 可直接在本地浏览器打开，支持类别筛选和框叠加；它不修改标注。源数据不会被原地清洗；源冻结采用快照与哈希，不修改源目录权限。XML原文件完整保留（其中可能含源设备路径），不要直接公开它们。

后续训练前按[入库标准](skills/marrow-dataset-intake/references/intake-standard.md)补齐使用依据、标签处置、标注完整性和分组设计。患者优先，玻片次之；裁剪、增强、确认重叠与近重复对象跟随源组。编号或时间代理只能做敏感性分析；格式转换、工作副本和裁剪不增加独立样本量。

本工具不自动生成完整患者独立切分、不自动为细胞赋医学真值，也不承诺自动检测所有近重复/重叠。展示和发布真实样本需要相应权限，代码仓库默认不接收实际数据。论文应分别交代数据来源、处理、历史使用和评估限制。

## 验证

```console
python -m pip install Pillow
python -X utf8 -m unittest discover -s tests -v
```

测试仅生成几何合成图，覆盖源数据不变、独立复制、未知/忽略对象保留、重复标注冲突、工作副本损坏检测及安装配置保留。
