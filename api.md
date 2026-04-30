# KiPIDA 桥接插件 API 清单

## 需求概述

实现一个 EasyEDA 扩展插件，用于桥接 KiPIDA（Python PDN DC IR Drop 分析工具）。插件需要：

1. 从 EasyEDA 获取 PCB 数据（net / track / via / pad）
2. 发送数据到本地 Python 服务（http://localhost:5000/analyze）
3. 调用 KiPIDA 进行 IR Drop 分析
4. 返回结果并在 EasyEDA 中显示

## API 列表

### 功能点 1：获取所有网络名称

| 调用方式 | 方法签名 | 说明 |
|----------|---------|------|
| `eda.pcb_Net.getAllNetsName()` | `getAllNetsName(): Promise<Array<string>>` | 获取当前 PCB 所有网络的名称列表 |

### 功能点 2：获取指定网络的所有图元

| 调用方式 | 方法签名 | 说明 |
|----------|---------|------|
| `eda.pcb_Net.getAllPrimitivesByNet(net, types)` | `getAllPrimitivesByNet(net: string, primitiveTypes?: Array<EPCB_PrimitiveType>): Promise<Array<IPCB_Primitive>>` | 获取指定网络的所有图元，可筛选图元类型 |

### 功能点 3：走线/线条数据提取

从 `IPCB_PrimitiveLine` 获取：

| 方法 | 返回值 | 说明 |
|------|--------|------|
| `getState_StartX()` | number | 起点 X 坐标 |
| `getState_StartY()` | number | 起点 Y 坐标 |
| `getState_EndX()` | number | 终点 X 坐标 |
| `getState_EndY()` | number | 终点 Y 坐标 |
| `getState_LineWidth()` | number | 线宽 |
| `getState_Layer()` | TPCB_LayersOfLine | 所在层 |
| `getState_Net()` | string | 网络名称 |

### 功能点 4：过孔数据提取

从 `IPCB_PrimitiveVia` 获取：

| 方法 | 返回值 | 说明 |
|------|--------|------|
| `getState_X()` | number | X 坐标 |
| `getState_Y()` | number | Y 坐标 |
| `getState_Diameter()` | number | 过孔直径 |
| `getState_HoleDiameter()` | number | 孔径 |
| `getState_Net()` | string | 网络名称 |

### 功能点 5：焊盘数据提取

从 `IPCB_PrimitivePad` 获取：

| 方法 | 返回值 | 说明 |
|------|--------|------|
| `getState_X()` | number | X 坐标 |
| `getState_Y()` | number | Y 坐标 |
| `getState_PadNumber()` | string | 焊盘编号 |
| `getState_Net()` | string | 网络名称 |
| `getState_Pad()` | TPCB_PrimitivePadShape | 焊盘形状和尺寸 |

### 功能点 6：HTTP 请求发送数据

| 调用方式 | 方法签名 | 说明 |
|----------|---------|------|
| `eda.sys_ClientUrl.request(url, method, body)` | `request(url: string, method?: string, body?: any): Promise<Response>` | 发送 HTTP 请求到 Python 服务 |

### 功能点 7：显示 IFrame 结果页面

| 调用方式 | 方法签名 | 说明 |
|----------|---------|------|
| `eda.sys_IFrame.openIFrame(url, title, width, height)` | `openIFrame(url: string, title?: string, width?: number, height?: number): Promise<void>` | 打开内嵌框架显示分析结果 |

### 功能点 8：显示进度条

| 调用方式 | 方法签名 | 说明 |
|----------|---------|------|
| `eda.sys_LoadingAndProgressBar.showProgressBar(progress, id)` | `showProgressBar(progress: number, id?: string): void` | 显示/更新进度条 |

## 涉及的类型

### EPCB_PrimitiveType 枚举值

- `TRACK` / `LINE` = 走线/线条
- `VIA` = 过孔
- `PAD` = 焊盘
- `ARC` = 圆弧

### TPCB_LayersOfLine

- Top Layer (1)
- Bottom Layer (2)
- Inner Layer (n)

## 数据传输格式

发送到 Python 服务的 JSON 格式：

```json
{
  "tracks": [
    {
      "net": "GND",
      "x1": 0, "y1": 0,
      "x2": 100, "y2": 0,
      "width": 0.254,
      "layer": 1
    }
  ],
  "vias": [
    {
      "net": "GND",
      "x": 50,
      "y": 50,
      "diameter": 0.6,
      "hole_diameter": 0.3
    }
  ],
  "pads": [
    {
      "net": "GND",
      "x": 0,
      "y": 0,
      "pad_number": "1",
      "width": 0.6,
      "height": 0.6
    }
  ]
}
```

## 注意事项

1. 所有坐标单位为 mm
2. 需处理网络名称为空的图元（通常是铺铜等）
3. Python 服务返回结果后可使用 IFrame 展示 HTML 格式的报告
4. 需添加超时和错误处理机制
