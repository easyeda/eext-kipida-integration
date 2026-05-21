# KiPIDA 桥接插件

将 [KiPIDA](https://github.com/kbralten/KiPIDA) PDN IR Drop 分析工具桥接到嘉立创EDA专业版的扩展插件。

**项目地址**: [https://github.com/easyeda/eext-kipida-integration](https://github.com/easyeda/eext-kipida-integration)

**English Version**: [README_EN.md](README_EN.md)

## 什么是 PDN 仿真？

### 术语说明

| 缩略词 | 全称 | 含义 |
|--------|------|------|
| PDN | Power Distribution Network | 电源分配网络——PCB 上从电源到芯片的整条供电路径，包括电源平面、走线、过孔、去耦电容等 |
| IR Drop | Voltage Drop (I×R) | 电流（I）流过导体电阻（R）产生的电压降落 |

### 为什么需要 PDN 仿真？

在 PCB 设计中，电源从 VRM（稳压模块）到达芯片引脚的过程中，会经过铜箔走线、过孔、焊盘等导体。这些导体虽然电阻很小，但当负载电流较大时，累积的压降（IR Drop）可能导致芯片实际供电电压低于最低工作电压，引发逻辑错误、时序违规甚至芯片无法启动。

PDN 仿真通过对 PCB 铜箔进行网格化建模，计算每个节点的电压分布，帮助设计者在制板前发现：

- **供电不足区域**：哪些芯片引脚的实际电压低于容限
- **电流瓶颈**：走线过窄或过孔数量不足导致的局部高电流密度
- **布局优化方向**：去耦电容放置位置、铜箔加宽区域等改进建议

本插件将 KiPIDA 的 PDN IR Drop 求解能力集成到嘉立创EDA中，让用户无需导出文件到第三方工具，即可在 PCB 编辑器内完成仿真并直观查看结果。

## 功能概述

- 从 EasyEDA 提取 PCB 走线、过孔、焊盘数据
- 在配置面板中选择电源网络、指定电压源与电流负载
- 调用本地 KiPIDA 求解器进行 IR Drop 分析
- 以 3D 热力图 + 各层 2D 热力图的形式展示分析结果

## 工作流程

### 1. 配置分析参数

打开 PCB 文件后，点击菜单 **PDN 分析 → 运行 IR Drop 分析**，弹出配置面板：

![配置界面](images/配置界面.png)

- 左侧自动检测 PCB 中的电源网络，点击选中目标网络
- 右侧为该网络添加 **SOURCE**（电压源器件）和 **LOAD**（负载器件）
- 设置额定电压、各负载电流值
- 调整底部的 **Mesh Resolution**（网格精度，越小越精确但越慢）

### 2. 查看分析结果

点击 **Run Simulation** 后，结果窗口自动弹出：

![输出结果](images/输出结果.png)

- 顶部文字摘要：每个 Rail 的电压范围和 IR Drop 值
- 标签页切换：3D View（全局三维热力图）+ 各铜层 2D 热力图
- 颜色映射：黄色 = 高电压，紫色 = 低电压（viridis 色阶）
- 分析图像同时保存到 `kipida-service/output/` 目录

---

## 安装与配置

> 本插件通过嘉立创EDA扩展商店发布，用户可直接安装 `.eext` 插件文件。但插件运行依赖本地 Python 服务，需要从 [GitHub 仓库](https://github.com/easyeda/eext-kipida-integration) 下载 `kipida-service` 文件夹并配置。

### 前置依赖

| 依赖 | 说明 |
|------|------|
| 嘉立创EDA专业版 ≥ 2.3.0 | 插件运行环境 |
| Python 3.10+ | 运行 kipida-service |

### 1. 安装 EasyEDA 插件

在嘉立创EDA专业版中：**高级 → 扩展管理器 → 导入扩展**，选择 `build/dist/kipida-bridge_v1.0.0.eext`。

也可通过嘉立创EDA扩展商店直接搜索安装。

### 2. 获取 kipida-service

从 GitHub 仓库下载 `kipida-service` 文件夹：

```bash
git clone https://github.com/easyeda/eext-kipida-integration.git
```

仅需其中的 `kipida-service/` 目录。

### 3. 启动 Python 服务

双击 `kipida-service/start.bat` 即可。脚本会自动完成以下操作：

1. 从 GitHub 下载 KiPIDA 核心算法文件（`mesh.py`、`solver.py`）
2. 安装 Python 依赖（fastapi、numpy、scipy 等）
3. 启动服务（默认端口 5000）

如需手动启动：

```bash
cd kipida-service
pip install -r requirements.txt
python -m uvicorn main:app --reload --port 5000
```

服务启动后访问 http://localhost:5000/docs 可查看 API 文档。

---

## 项目结构

```
eext-kipida-integration/
├── src/                    # TypeScript 插件源码
│   ├── index.ts            # 主入口，菜单注册
│   ├── extract.ts          # PCB 数据提取
│   ├── convert.ts          # EasyEDA → KiPIDA 格式转换
│   ├── api.ts              # HTTP 客户端
│   ├── display.ts          # 结果展示
│   └── types.ts            # 类型定义
├── ui/
│   ├── config.html         # 配置面板
│   └── results.html        # 结果展示面板
├── kipida-service/
│   ├── main.py             # FastAPI 服务（调用 KiPIDA 求解器）
│   ├── start.bat           # 一键启动脚本
│   └── requirements.txt
├── build/dist/             # 编译产物（.eext 文件）
└── extension.json          # 插件配置
```

---

## 开发构建

```bash
npm install
npm run build
```

构建产物输出到 `build/dist/kipida-bridge_v1.0.0.eext`。

---

## 注意事项

- KiPIDA 核心算法（`mesh.py`、`solver.py`）来自 [kbralten/KiPIDA](https://github.com/kbralten/KiPIDA)，首次运行脚本时自动下载
- 可通过环境变量 `KIPIDA_PATH` 自定义 KiPIDA 路径，`start.bat` 默认使用 `kipida-service/KiPIDA/`
- Python 服务必须在运行分析前启动，默认端口 5000
- 服务地址可在插件菜单 **PDN 分析 → 配置服务地址** 中修改
- `mesh_resolution` 越小精度越高，但分析时间显著增加（推荐 0.2~0.5mm）

## 更新 KiPIDA

删除 `kipida-service/KiPIDA/` 下的 `mesh.py` 和 `solver.py`，重新运行 `start.bat` 即可自动下载最新版本。也可手动从原项目下载覆盖：

**原项目地址**: [https://github.com/kbralten/KiPIDA](https://github.com/kbralten/KiPIDA)
