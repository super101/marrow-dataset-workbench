# 本地工作台使用手册

本手册对应首版实现范围；[产品方案](PRODUCT_DESIGN.md)也包含后续设计。正式用于论文前，应完成[发布检查表](RELEASE_CHECKLIST.md)中与自身数据和训练程序有关的验收。

## 1. 安装与运行

Python 3.11+，Pillow 为必需依赖。无需 Node.js、数据库服务器或 GPU。工作台使用 Python 本地 HTTP 服务、SQLite 和浏览器 JavaScript。

Windows 在下载解压或克隆的仓库目录打开 PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\start-workbench.cmd -Demo
```

无需激活虚拟环境。若没有 `py`，指定真实解释器路径；不要假设系统默认 `python` 别名可用：

```powershell
& 'C:\Path\To\Python311\python.exe' -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

启动器依次选择仓库 `.venv\Scripts\python.exe`、`py -3.11`、实际可运行的 Python 3.11+ `python3`；也可传 `-PythonExecutable 'C:\Path\To\python.exe'`。启动器只检查依赖，不自动联网安装。首次 `pip install` 需要软件源，后续浏览、编辑和导出均在本地。

默认打开浏览器；使用 `-NoBrowser` 可取消。端口占用时传 `-Port 8766`。关停用启动终端中的 `Ctrl+C`，不要在“保存中”时结束进程。

## 2. 工作空间与数据根目录

默认工作空间位于用户目录 `.marrow-workbench`，其中包含工作图像、SQLite 草稿和导出文件。必须位于代码仓库之外。它与原始资料分开；原始图像和 XML 不原地改写。

`export_records/` 保存每个成功训练包的私有来源映射与冻结快照，可由包内图像/对象 ID 反查原图和修订。该目录与 SQLite 数据库不在下载 ZIP 中，含私有来源信息，请勿随公开训练包分发。备份时应包含工作图像、数据库与私有导出记录；服务关停后复制整个工作目录最简单。

```powershell
.\start-workbench.cmd -Workspace 'D:\ResearchWork\marrow' -AllowRoot 'D:\ResearchData'
```

多个根目录建议直接调用 PowerShell 启动脚本，使用数组：

```powershell
.\start-workbench.ps1 -Workspace 'D:\ResearchWork\marrow' -AllowRoot @('D:\ResearchData', 'E:\ExternalData')
```

也可以设置当前终端环境变量：

```powershell
$env:MARROW_WORKSPACE = 'D:\ResearchWork\marrow'
$env:MARROW_ALLOW_ROOT = 'D:\ResearchData;E:\ExternalData'
.\start-workbench.cmd
```

允许根目录是本次服务的读取边界，不代表数据已经获得研究或公开许可。未登记目录、越界路径和符号链接逃逸会被拒绝。不建议把整块磁盘或整个用户目录列为根目录。

命令行等价接口（允许多次 `--allow-root`）：

```powershell
.\.venv\Scripts\python.exe -m workbench --workspace 'D:\ResearchWork\marrow' --allow-root 'D:\ResearchData' --port 8765 --open-browser
```

安装后也可使用 `marrow-workbench` 命令；Windows 未激活虚拟环境时用 `.\.venv\Scripts\marrow-workbench.exe`。

Linux/macOS 用明确的 Python 3.11+ 解释器创建环境，例如 `python3.11 -m venv .venv`、`.venv/bin/python -m pip install -e .`，再用 `.venv/bin/python -m workbench --workspace /data/work --allow-root /data/incoming --open-browser`。

## 3. 导入与坐标确认

可以导入旧 `marrow-dataset-intake` 工作集，或图像及 VOC/XML。首版不是通用 COCO/YOLO 导入器；这些格式首先用于输出。

VOC 典型来源结构：

```text
source/
  JPEGImages/
  Annotations/
```

字典 CSV 使用 UTF-8，列为 `code,name,role`，编码保留字符串形式。不要凭经验猜未知编码的医学名称。源字典角色为 `cell`、`ignore`、`unresolved`；训练映射另可设置有任务定义及依据的 `out_of_scope`。

```csv
code,name,role
demo_a,合成类别 A,cell
demo_b,合成类别 B,cell
demo_unknown,待确认对象,unresolved
```

导入必须明确源坐标约定：

| 选项 | 源标注语义 | 转换示例 |
| --- | --- | --- |
| `zero` | 零起点半开区间 | `[0,0,10,10]` 保持不变 |
| `voc` | 一起点闭区间 | `[1,1,10,10]` 转为内部 `[0,0,10,10]` |

仅有 `.xml` 扩展名或 VOC 格式名不能证明源坐标语义。拿第一张图叠框确认，尤其检查图像边界与小框。内部一律保存零起点半开区间；导出 VOC 再转为一起点闭区间，COCO 使用零起点 `x,y,width,height`，YOLO 使用归一化中心和宽高。

导入会建立独立文件副本。先用合成数据熟悉流程，再明确选择有权处理的真实目录。演示模式不会自动选择、扫描或授权本机医院资料。

## 4. 复核与自动草稿

以整张图完成复核，同时可按问题或单个细胞跳转。左侧图像队列，中间原图与矩形框，右侧裁剪、标签和问题。修改框位置、尺寸、类别，补框或带理由软删除；原始编码和原始对象记录保留。

草稿自动提交，只有服务端写入成功后才能显示“已保存”。保存失败时先重试或保留恢复草稿，不要继续导出。多个标签页修改同一版本可能遇到版本冲突，应重新读取当前版本并核对，不直接覆盖。

“完成本图复核”是独立的确认动作，要求标注完整性及阻断事项满足条件。已确认图再次编辑会回到待复核；改映射、身份或划分也会使相关准备状态失效。“已查看”不等于“问题已解决”。

未知对象不能只删框后当背景。若确属错误标注，应记录依据；无法解释时保留待解决，或暂缓整图。草稿不是备份。

## 5. 类别、规则与来源

三层信息分开：原始编码、人工修订类别、训练类别方案。允许合并类别但必须有依据；映射角色决定纳入、忽略、待解决或任务外对象。原始资料不随训练类别方案改变。

导出前填写研究使用依据及状态、任务定义、标注是否穷尽及依据、历史开发使用情况。软件不签发伦理批准，也不自动验证上传的材料内容。真实身份与公开摘要分开保存。

## 6. 分组与划分

导入有证据的身份 CSV。字段为 `image_id,patient_id,slide_id,acquisition_batch_id,source_image_id,metadata_evidence`，使用界面中的图像 ID；未知字段留空。患者/玻片标识应使用受控研究 ID，原始身份对应表另存。`source_image_id` 可以引用同一数据集的父图 ID。

已确认重叠、近重复、裁剪或增强关系可另导入 `a,b,kind,evidence` 关系 CSV。证据及对应关系需人工核实，不能把自动候选直接当已确认。

分组以共享患者、玻片、同源关系、完全重复及已确认关系的传递闭包为准。批次默认用于分布分析。编号和时间不能代替真实身份。

可选患者范围、玻片范围或来源连通分量探索范围。身份缺失时不能宣称患者独立；仅在一个组出现的类别不能可靠估计跨组效果。不为覆盖稀有类拆组。

按组数与类别覆盖配置比例及种子，查看预览后确认。先划分原图，再生成单细胞裁剪；所有裁剪跟随父图。历史调参图不会因更名成测试集成为未见样本。预定义外部划分 CSV 使用 `image_id,split`，`split` 为 `train`、`val`、`test`，仍接受组泄漏检查。

## 7. 两档训练导出

| 模式 | 额外要求 | 解释 |
| --- | --- | --- |
| 严格复核版 | 纳入图逐图确认完整性；分类裁剪明确确认纳入 | 适合正式实验的数据准备要求，仍需专业判断 |
| 基础检查版 | 允许部分图尚未人工逐图确认；需有标注完整性依据 | 跑通训练和探索，报告实际复核覆盖率 |

两档均检查文件与框、类别、研究依据、分组和固定划分。不能正确表达 ignore 的普通训练器默认暂缓含 ignore/未解决对象的整图；VOC `difficult`、COCO `iscrowd` 不自动等于训练忽略机制。问题图可被记录后排除，不必阻塞其余合格子集；有效划分为空等全局问题仍阻断。

检测输出支持 VOC、COCO、YOLO；分类输出保留原始分辨率裁剪，默认不扩边、不强制变形。裁剪扩边与最小尺寸在导出前固定。多主细胞风险、严重触边/截断和不确定样本需单独处理，框之间的几何检查不能证明没有漏标细胞。分类排除不一定排除检测父图。

导出前查看纳入/排除数量、原因、各集合图像和细胞数、类别映射、组覆盖、复核覆盖率与裁剪预览。每次构建新包，不覆盖先前实验产物；中途失败不应把半成品标为成功。

包内包含训练文件及类别、划分、规则、版本、统计、排除原因和校验等清单。分类文件记录父图和对象关系。导出编号不是医学去标识化证明：图像像素和元数据仍须单独审查。

## 8. 隐私、发布与备份

公开网站只使用合成示例，不读取本地目录；真实目录导入在本地完整版完成。首版本地服务仅监听 `127.0.0.1`，不是公网服务。不要端口转发，也不要直接改成监听所有网卡来部署给多人。

研究许可、团队共享许可、公开发布许可分开核实。隐私协议不能自动替代数据提供方的公开授权。真实数据上传 GitHub、私有服务器认证和多人协作属于后续功能；下载导出包不等于已上传或已脱敏。

备份时先停止服务，再完整复制仓库外工作空间到受控备份位置。工作空间含图像、草稿、历史和身份信息，不能提交到 GitHub。恢复到另一个仓库外目录后，通过 `--workspace` 打开，核对图像、修订和划分。只备份 SQLite 而遗漏工作资产是不完整备份。

## 9. 故障处理

- **找不到 Python**：确认安装 Python 3.11+，使用 `-PythonExecutable` 指定路径，或先建立 `.venv`。
- **缺依赖/包**：用启动器所选的同一解释器执行 `-m pip install -e .`，不要混用其他环境的 pip。
- **目录被拒绝**：检查 `--allow-root` 和实际解析路径，避免越界或符号链接逃逸，不要为绕过限制开放整盘。
- **版本冲突**：保留自己的未保存改动，再重新加载并比较当前修订。
- **无法确认/导出**：点击阻断原因回到图像、类别、规则或划分；基础档也不会绕过这些问题。
- **端口被占用**：换 `-Port 8766`；不要关闭不认识的进程。

提交问题反馈时用合成复现、错误摘要和软件版本，不贴真实图像、身份表、私有路径、令牌或原始文件哈希。
