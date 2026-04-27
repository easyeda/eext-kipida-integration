"""
KiPIDA FastAPI Service
桥接 EasyEDA 扩展与 KiPIDA 分析引擎

运行方式:
    python -m uvicorn main:app --reload --port 5000
"""

import sys
import os
import time
import traceback

# 将 KiPIDA 源码目录加入 Python 路径
KIPIDA_PATH = os.environ.get("KIPIDA_PATH", r"D:\PDN\KiPIDA")
if KIPIDA_PATH not in sys.path:
    sys.path.insert(0, KIPIDA_PATH)

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict

# ============================================================
# 数据模型定义
# ============================================================

class CopperPour(BaseModel):
    net: str
    layer: int
    vertices: List[Dict[str, float]]  # [{"x": ..., "y": ...}, ...]


class Node(BaseModel):
    id: str
    net: str
    type: str
    x: float
    y: float
    layer: Optional[int] = None
    voltage: Optional[float] = None


class Resistance(BaseModel):
    id: str
    start_node: str
    end_node: str
    net: str
    length: float
    width: float
    thickness: float
    layer: int
    resistance: float


class Connection(BaseModel):
    from_node: str
    to: str
    type: str
    net: str
    resistance_id: Optional[str] = None


class Source(BaseModel):
    node_id: str
    voltage: float


class Load(BaseModel):
    node_id: str
    current: float


class Metadata(BaseModel):
    total_nets: int
    total_tracks: int
    total_vias: int
    total_pads: int
    extracted_at: str


class KipidaInput(BaseModel):
    nodes: List[Node]
    resistances: List[Resistance]
    connections: List[Connection]
    sources: List[Source] = []
    loads: List[Load] = []
    copper_pours: List[CopperPour] = []
    mesh_resolution: Optional[float] = 0.1
    max_drop_pct: Optional[float] = 5.0
    metadata: Optional[Metadata] = None


class NetResult(BaseModel):
    net: str
    max_drop: float
    avg_current: float
    min_voltage: float
    max_voltage: float


class NetPlotImages(BaseModel):
    view_3d: Optional[str] = None
    layers: Dict[str, str] = {}


class AnalysisResults(BaseModel):
    max_drop: float
    avg_current: float
    net_results: List[NetResult]
    plot_images: Dict[str, NetPlotImages] = {}


class AnalysisOutput(BaseModel):
    success: bool
    message: Optional[str] = None
    results: Optional[AnalysisResults] = None


# ============================================================
# FastAPI 应用
# ============================================================

app = FastAPI(
    title="KiPIDA Bridge API",
    description="桥接 EasyEDA 与 KiPIDA PDN IR Drop 分析",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

analysis_state = {
    "last_input": None,
    "last_result": None,
    "total_requests": 0
}


# ============================================================
# KiPIDA Solver 集成
# ============================================================

def mesh_copper_pours(
    data: 'KipidaInput',
    m,
    str_to_int: Dict[str, int],
    node_net: Dict[int, str],
    next_node_id: int,
    active_nets: set,
    unique_nodes: list,
    net_layer_junctions: Dict[str, Dict[int, list]],
) -> int:
    """
    将铺铜多边形网格化并融合到 Mesh 中。
    返回更新后的 next_node_id。
    """
    print(f"[DEBUG] mesh_copper_pours 调用: copper_pours={len(data.copper_pours)}, active_nets={active_nets}")
    if not data.copper_pours:
        return next_node_id

    try:
        from shapely.geometry import Polygon, Point
        import numpy as np
        import math as _math
    except ImportError:
        print("[KiPIDA] 警告: shapely 未安装，跳过铺铜网格化")
        return next_node_id

    COPPER_RESISTIVITY = 1.72e-5  # Ω·mm
    COPPER_THICKNESS = 0.035       # mm (1oz)
    R_SHEET = COPPER_RESISTIVITY / COPPER_THICKNESS  # Ω/square
    MIL_TO_MM = 0.0254
    res_mm = data.mesh_resolution or 0.1
    res_mil = res_mm / MIL_TO_MM   # 坐标系为 mil，网格步长转换为 mil
    G_CELL = 1.0 / R_SHEET         # 正方形网格 L/W=1，G = 1/R_sheet
    G_PAD = 1e6

    total_pour_nodes = 0

    for pour in data.copper_pours:
        if pour.net not in active_nets:
            continue
        if len(pour.vertices) < 3:
            continue

        try:
            poly = Polygon([(v['x'], v['y']) for v in pour.vertices])
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty:
                continue
        except Exception as e:
            print(f"[KiPIDA] 铺铜多边形构建失败 net={pour.net}: {e}")
            continue

        minx, miny, maxx, maxy = poly.bounds
        xs = np.arange(minx + res_mil * 0.5, maxx, res_mil)
        ys = np.arange(miny + res_mil * 0.5, maxy, res_mil)

        # 收集多边形内的网格点，用 (ix, iy) 索引快速查找相邻节点
        grid: Dict[tuple, int] = {}  # (ix, iy) → node_id
        for ix, x in enumerate(xs):
            for iy, y in enumerate(ys):
                if poly.contains(Point(x, y)):
                    nid = next_node_id
                    next_node_id += 1
                    m.nodes.append(nid)
                    m.node_coords[nid] = (float(x), float(y), pour.layer)
                    node_net[nid] = pour.net
                    grid[(ix, iy)] = nid

        if not grid:
            continue

        # 连接相邻网格节点（横向 + 纵向）
        # 铺铜方块电阻 R_cell = R_sheet（正方形，L/W=1），但坐标单位是 mil
        # 实际步长 res_mil mil = res_mm mm，电阻 = R_sheet * (res_mm/res_mm) = R_sheet
        for (ix, iy), nid in grid.items():
            right = grid.get((ix + 1, iy))
            if right is not None:
                m.add_edge_direct(nid, right, G_CELL)
            up = grid.get((ix, iy + 1))
            if up is not None:
                m.add_edge_direct(nid, up, G_CELL)

        pour_node_ids = list(grid.values())
        total_pour_nodes += len(pour_node_ids)

        # 将铺铜网格节点连接到同层同网络的走线 junction 节点
        # 无距离阈值：同网络的 junction 必然与铺铜电气相连，连接最近的网格节点
        layer_juncs = net_layer_junctions.get(pour.net, {}).get(pour.layer, [])
        for junc in layer_juncs:
            jx, jy = junc.x, junc.y
            best_nid = min(pour_node_ids, key=lambda nid: (m.node_coords[nid][0]-jx)**2 + (m.node_coords[nid][1]-jy)**2)
            junc_idx = str_to_int.get(junc.id)
            if junc_idx is not None:
                m.add_edge_direct(best_nid, junc_idx, G_PAD)

        # 将同网络的 pad/via 节点连接到本铺铜最近的网格节点
        # 不做层过滤：pad 可能在不同层但电气上连接到铺铜（如通孔焊盘）
        for node in unique_nodes:
            if node.net != pour.net or node.type not in ('pad', 'via'):
                continue
            node_idx = str_to_int.get(node.id)
            if node_idx is None:
                continue
            best_nid = min(pour_node_ids, key=lambda nid: (m.node_coords[nid][0]-node.x)**2 + (m.node_coords[nid][1]-node.y)**2)
            m.add_edge_direct(node_idx, best_nid, G_PAD)

    print(f"[KiPIDA] 铺铜网格化完成: {len(data.copper_pours)} 个铺铜, {total_pour_nodes} 个网格节点")
    return next_node_id


def build_mesh_and_solve(data: KipidaInput) -> Dict[str, float]:
    """
    将 EasyEDA 数据转换为 KiPIDA Mesh，调用真实 Solver 求解。
    只处理有电压源的 net，避免孤立子图导致矩阵奇异。
    返回 { node_id_str: voltage } 映射。
    """
    from mesh import Mesh
    from solver import Solver

    print(f"[DEBUG] 输入: nodes={len(data.nodes)}, resistances={len(data.resistances)}, sources={len(data.sources)}, loads={len(data.loads)}, copper_pours={len(data.copper_pours)}")
    print(f"[DEBUG] sources: {[(s.node_id, s.voltage) for s in data.sources]}")
    print(f"[DEBUG] loads: {[(l.node_id, l.current) for l in data.loads]}")

    # 1. 找出有 Source 的 net
    source_node_ids = {s.node_id for s in data.sources}
    load_node_ids = {l.node_id for l in data.loads}
    active_node_ids = source_node_ids | load_node_ids

    # 找 source/load 节点所属的 net
    active_nets: set = set()
    for node in data.nodes:
        if node.id in active_node_ids:
            active_nets.add(node.net)

    print(f"[DEBUG] active_nets: {active_nets}")
    # 检查 source/load node_id 是否能在 nodes 列表中找到
    all_node_ids = {n.id for n in data.nodes}
    missing_sources = source_node_ids - all_node_ids
    missing_loads = load_node_ids - all_node_ids
    if missing_sources:
        print(f"[DEBUG] 警告: source node_id 在 nodes 中找不到: {missing_sources}")
    if missing_loads:
        print(f"[DEBUG] 警告: load node_id 在 nodes 中找不到: {missing_loads}")

    if not active_nets:
        raise ValueError("没有有效的电压源节点，无法求解")

    # 2. 只保留活跃 net 的节点（去重）
    seen = {}
    unique_nodes = []
    for node in data.nodes:
        if node.net not in active_nets:
            continue
        if node.id not in seen:
            seen[node.id] = len(unique_nodes)
            unique_nodes.append(node)
    str_to_int: Dict[str, int] = seen

    # 3. 构建 Mesh，node_coords 统一存 (x, y, layer)
    # 额外维护 node_net dict 记录每个节点所属 net（含插值节点）
    m = Mesh()
    node_net: Dict[int, str] = {}
    m.nodes = list(range(len(unique_nodes)))
    for node in unique_nodes:
        idx = str_to_int[node.id]
        m.node_coords[idx] = (node.x, node.y, node.layer if node.layer is not None else -1)
        node_net[idx] = node.net

    # 4. 添加电阻边（按 mesh_resolution 插值，坐标单位 mm）
    import math as _math
    res_mm = (data.mesh_resolution or 0.1)
    next_node_id = len(unique_nodes)

    for res in data.resistances:
        if res.net not in active_nets or res.resistance <= 0:
            continue
        u = str_to_int.get(res.start_node)
        v = str_to_int.get(res.end_node)
        if u is None or v is None or u == v:
            continue

        ux, uy = m.node_coords[u][0], m.node_coords[u][1]
        vx, vy = m.node_coords[v][0], m.node_coords[v][1]
        length_mm = _math.sqrt((vx - ux)**2 + (vy - uy)**2)
        n_seg = max(1, round(length_mm / res_mm))

        if n_seg <= 1:
            m.add_edge_direct(u, v, 1.0 / res.resistance)
        else:
            seg_g = n_seg / res.resistance  # 每段电导 = 总电导 × 段数
            layer = res.layer if res.layer is not None else 0
            prev = u
            for i in range(1, n_seg):
                t = i / n_seg
                nid = next_node_id
                next_node_id += 1
                m.nodes.append(nid)
                m.node_coords[nid] = (ux + t*(vx-ux), uy + t*(vy-uy), layer)
                node_net[nid] = res.net
                m.add_edge_direct(prev, nid, seg_g)
                prev = nid
            m.add_edge_direct(prev, v, seg_g)

    # 5. 将所有 pad 和 via 节点连接到同 net 最近的 junction 节点
    # pad: 只连同层最近的 junction
    # via: 连接每一层上最近的 junction（via 跨层桥接）
    net_layer_junctions: Dict[str, Dict[int, list]] = {}
    for node in unique_nodes:
        if node.type == 'junction':
            layer = node.layer if node.layer is not None else -1
            net_layer_junctions.setdefault(node.net, {}).setdefault(layer, []).append(node)

    G_PAD = 1e6
    for node in unique_nodes:
        if node.type not in ('pad', 'via'):
            continue
        u = str_to_int[node.id]
        net_layers = net_layer_junctions.get(node.net, {})
        if not net_layers:
            continue

        if node.type == 'pad':
            # 只连同层
            layer = node.layer if node.layer is not None else -1
            candidates = net_layers.get(layer, [])
            if not candidates:
                # 回退：连全局最近
                candidates = [j for jlist in net_layers.values() for j in jlist]
            if candidates:
                best = min(candidates, key=lambda j: (j.x - node.x)**2 + (j.y - node.y)**2)
                v = str_to_int[best.id]
                if u != v:
                    m.add_edge_direct(u, v, G_PAD)
        else:
            # via: 连每一层上最近的 junction
            for layer_junctions in net_layers.values():
                if not layer_junctions:
                    continue
                best = min(layer_junctions, key=lambda j: (j.x - node.x)**2 + (j.y - node.y)**2)
                v = str_to_int[best.id]
                if u != v:
                    m.add_edge_direct(u, v, G_PAD)

    # 5b. 铺铜网格化与融合
    next_node_id = mesh_copper_pours(
        data, m, str_to_int, node_net, next_node_id,
        active_nets, unique_nodes, net_layer_junctions,
    )

    # 6. 转换 sources / loads
    sources = []
    for s in data.sources:
        idx = str_to_int.get(s.node_id)
        if idx is not None:
            sources.append({"node_id": idx, "voltage": s.voltage})

    loads = []
    for l in data.loads:
        idx = str_to_int.get(l.node_id)
        if idx is not None:
            loads.append({"node_id": idx, "current": l.current})

    if not sources:
        raise ValueError("没有有效的电压源节点，无法求解")

    # 7. 求解前：用连通分量分析，只保留含 Source 的子图
    import numpy as np
    import scipy.sparse
    from scipy.sparse.csgraph import connected_components

    N = len(m.nodes)  # 插值后节点总数
    # 从 mesh 的 COO 数据构建邻接矩阵（只需要连通性，用绝对值）
    if m.G_coo_data:
        rows = np.array(m.G_coo_row)
        cols = np.array(m.G_coo_col)
        data_arr = np.ones(len(rows))
        adj = scipy.sparse.coo_matrix((data_arr, (rows, cols)), shape=(N, N))
        adj = (adj + adj.T)  # 确保对称
        n_comp, labels = connected_components(adj, directed=False)
    else:
        n_comp, labels = 1, np.zeros(N, dtype=int)

    # 找含 Source 的连通分量
    source_comps = set()
    for s in sources:
        source_comps.add(int(labels[s['node_id']]))

    print(f"[KiPIDA] 连通分量: {n_comp} 个, 含Source: {source_comps}")

    # 过滤：只保留含 Source 的分量的节点
    keep_mask = np.array([labels[i] in source_comps for i in range(N)])
    keep_indices = np.where(keep_mask)[0]
    keep_set = set(keep_indices.tolist())

    if len(keep_indices) == 0:
        raise ValueError("Source 节点所在连通分量为空")

    # 重新映射索引
    old_to_new = {old: new for new, old in enumerate(keep_indices)}

    # 重建 Mesh（只含有效节点）
    m2 = Mesh()
    m2.nodes = list(range(len(keep_indices)))
    for old_idx in keep_indices:
        new_idx = old_to_new[old_idx]
        m2.node_coords[new_idx] = m.node_coords[old_idx]

    # 过滤边
    for r, c, d in zip(m.G_coo_row, m.G_coo_col, m.G_coo_data):
        if r in keep_set and c in keep_set:
            m2.G_coo_row.append(old_to_new[r])
            m2.G_coo_col.append(old_to_new[c])
            m2.G_coo_data.append(d)

    # 重映射 sources / loads
    sources2 = [{"node_id": old_to_new[s["node_id"]], "voltage": s["voltage"]}
                for s in sources if s["node_id"] in keep_set]
    loads2 = [{"node_id": old_to_new[l["node_id"]], "current": l["current"]}
              for l in loads if l["node_id"] in keep_set]

    print(f"[KiPIDA] 有效节点: {len(keep_indices)}, sources={len(sources2)}, loads={len(loads2)}")

    solver = Solver(debug=False)
    int_voltages2 = solver.solve(m2, sources2, loads2)

    # 映射回原始字符串 node_id（只映射原始节点，插值节点不在 int_to_str 里）
    int_to_str = {v: k for k, v in str_to_int.items()}
    result = {}
    for new_idx, voltage in int_voltages2.items():
        old_idx = keep_indices[new_idx]
        if old_idx in int_to_str:
            result[int_to_str[old_idx]] = voltage

    import math as _math
    valid_count = sum(1 for v in result.values() if _math.isfinite(v))
    print(f"[KiPIDA] 求解完成: 有效电压={valid_count}/{len(keep_indices)}")

    # 收集所有节点（含插值节点）的坐标+电压，用于绘图
    # tuple: (x_mil, y_mil, layer, net, voltage, node_type)
    mesh_points = []
    nan_count = 0
    pad_valid = 0
    pad_nan = 0
    for new_idx, voltage in int_voltages2.items():
        old_idx = keep_indices[new_idx]
        coords = m.node_coords.get(old_idx)
        if coords is None:
            continue
        x_mil, y_mil, layer = coords
        net = node_net.get(old_idx, '')
        is_pad = old_idx < len(unique_nodes) and unique_nodes[old_idx].type == 'pad'
        is_via = old_idx < len(unique_nodes) and unique_nodes[old_idx].type == 'via'
        node_type = 'pad' if is_pad else ('via' if is_via else 'junction')
        if not _math.isfinite(voltage):
            nan_count += 1
            if is_pad: pad_nan += 1
            continue
        if net:
            mesh_points.append((x_mil, y_mil, layer, net, voltage, node_type))
            if is_pad: pad_valid += 1

    print(f"[KiPIDA] 绘图节点: {len(mesh_points)}, pad={pad_valid}, via_NaN={pad_nan}")
    return result, mesh_points


def generate_plot_images(mesh_points: list, mesh_resolution: float = 0.5, all_layers: list = None, net_results: list = None, max_drop_pct: float = 5.0) -> Dict[str, 'NetPlotImages']:
    """
    mesh_points: [(x_mil, y_mil, layer, net, voltage), ...]
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    import io, base64, math, os

    OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    def fig_to_b64(fig, filename: str = None) -> str:
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=100)
        if filename:
            safe_name = filename.replace('/', '_').replace('\\', '_')
            out_path = os.path.join(OUTPUT_DIR, safe_name)
            with open(out_path, 'wb') as f:
                buf.seek(0)
                f.write(buf.read())
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode('utf-8')

    MIL_TO_MM = 0.0254
    if all_layers is None:
        all_layers = []
    if net_results is None:
        net_results = []

    # 按 net 建立固定色阶：vmax=source电压, vmin=source电压*(1-max_drop_pct/100)
    net_scale: Dict[str, tuple] = {}
    for r in net_results:
        scale_max = r.max_voltage
        scale_min = scale_max * (1.0 - max_drop_pct / 100.0)
        net_scale[r.net] = (scale_min, scale_max)

    net_points: Dict[str, list] = {}
    for item in mesh_points:
        x_mil, y_mil, layer, net, voltage = item[0], item[1], item[2], item[3], item[4]
        node_type = item[5] if len(item) > 5 else 'junction'
        if not math.isfinite(voltage) or not net:
            continue
        x_mm = x_mil * MIL_TO_MM
        y_mm = y_mil * MIL_TO_MM
        net_points.setdefault(net, []).append((x_mm, y_mm, layer, voltage, node_type))

    result = {}
    for net, points in net_points.items():
        if not points:
            continue

        # 过滤掉 layer=0，保留 layer=-1（via 哨兵值）和正常层
        valid_points = [p for p in points if p[2] != 0]
        if not valid_points:
            valid_points = points

        xs = [p[0] for p in valid_points]
        ys = [p[1] for p in valid_points]
        vs = [p[3] for p in valid_points]

        # 使用固定色阶（基于最大压降），与 KIPIDA 保持一致
        if net in net_scale:
            vmin, vmax = net_scale[net]
        else:
            vmin, vmax = min(vs), max(vs)
            if abs(vmax - vmin) < 1e-9:
                vmin -= 0.001; vmax += 0.001

        # 实际压降（用于标题显示）
        actual_drop = next((r.max_drop for r in net_results if r.net == net), vmax - vmin)

        net_img = NetPlotImages()

        # ── 3D 散点图 ─────────────────────────────────────────
        layer_ids = sorted(set(p[2] for p in valid_points))
        layer_to_z = {lid: 10.0 - i for i, lid in enumerate(layer_ids)}
        zs_mapped = [layer_to_z[p[2]] for p in valid_points]

        fig = plt.figure(figsize=(7, 5), constrained_layout=True)
        ax = fig.add_subplot(111, projection='3d')
        sc = ax.scatter(xs, ys, zs_mapped, c=vs, cmap='viridis', vmin=vmin, vmax=vmax, s=18)
        plt.colorbar(sc, ax=ax, label='Voltage (V)', shrink=0.7)
        ax.set_title(f"Rail: {net}  Drop: {actual_drop:.4f} V")
        ax.set_xlabel('X (mm)'); ax.set_ylabel('Y (mm)'); ax.set_zlabel('L (pseudo)')
        net_img.view_3d = fig_to_b64(fig, f"{net}_3d.png")

        # ── 各层 2D 图 ────────────────────────────────────────
        # via 节点 (layer=-1)：出现在所有层
        # pad 节点 (layer>0)：只出现在自己所属的层
        # junction/插值节点 (layer>0)：只出现在自己所属的层
        # 绘制范围：all_layers（含只有过孔的层）
        via_pts = [(p[0], p[1], p[3]) for p in valid_points
                   if len(p) > 4 and p[4] == 'via' and p[2] == -1]
        for layer_id in sorted(all_layers):
            specific_pts = [(p[0], p[1], p[3]) for p in valid_points if p[2] == layer_id]
            layer_all = specific_pts + via_pts
            if not layer_all:
                continue
            lxs = [p[0] for p in layer_all]
            lys = [p[1] for p in layer_all]
            lvs = [p[2] for p in layer_all]

            fig2, ax2 = plt.subplots(figsize=(7, 5), constrained_layout=True)
            sc2 = ax2.scatter(lxs, lys, c=lvs, cmap='viridis', vmin=vmin, vmax=vmax, s=40)
            plt.colorbar(sc2, ax=ax2, label='Voltage (V)')
            ax2.set_title(f"Rail: {net}  Layer {layer_id}  Drop: {actual_drop:.4f} V")
            ax2.set_xlabel('X (mm)'); ax2.set_ylabel('Y (mm)')
            ax2.set_aspect('equal', 'box')
            net_img.layers[str(layer_id)] = fig_to_b64(fig2, f"{net}_layer{layer_id}.png")

        result[net] = net_img

    return result


def compute_net_results(data: KipidaInput, voltages: Dict[str, float]) -> List[NetResult]:
    """从节点电压计算每个 net 的 IR Drop 统计。"""
    net_voltages: Dict[str, List[float]] = {}
    for node in data.nodes:
        v = voltages.get(node.id)
        if v is None:
            continue
        net_voltages.setdefault(node.net, []).append(v)

    # 每个 net 的总电流 = 该 net 所有 load 的电流之和
    net_currents: Dict[str, float] = {}
    load_ids = {l.node_id for l in data.loads}
    for node in data.nodes:
        if node.id in load_ids:
            load = next((l for l in data.loads if l.node_id == node.id), None)
            if load:
                net_currents[node.net] = net_currents.get(node.net, 0.0) + load.current

    # source 节点电压（每个 net 取最高电压作为参考，即 source 电压）
    net_source_voltage: Dict[str, float] = {}
    source_ids = {s.node_id for s in data.sources}
    for node in data.nodes:
        if node.id in source_ids:
            v = voltages.get(node.id)
            if v is not None:
                cur = net_source_voltage.get(node.net)
                if cur is None or v > cur:
                    net_source_voltage[node.net] = v

    results = []
    for net, vs in net_voltages.items():
        min_v = min(vs)
        max_v = max(vs)
        source_v = net_source_voltage.get(net, max_v)
        drop = source_v - min_v
        avg_current = net_currents.get(net, 0.0)
        results.append(NetResult(
            net=net,
            max_drop=round(drop, 6),
            avg_current=round(avg_current, 6),
            min_voltage=round(min_v, 6),
            max_voltage=round(max_v, 6),
        ))

    results.sort(key=lambda x: x.max_drop, reverse=True)
    return results


# ============================================================
# 路由
# ============================================================

@app.get("/")
async def root():
    return {"service": "KiPIDA Bridge API", "version": "2.0.0", "status": "running", "docs": "/docs"}


@app.get("/test")
async def test_connection():
    return {"status": "ok", "message": "KiPIDA 服务正常运行"}


@app.post("/analyze", response_model=AnalysisOutput)
async def analyze_pcb(data: KipidaInput):
    try:
        analysis_state["total_requests"] += 1
        analysis_state["last_input"] = data.model_dump()

        if not data.nodes:
            return AnalysisOutput(success=False, message="没有找到有效节点")

        start_time = time.time()

        voltages, mesh_points = build_mesh_and_solve(data)

        if not voltages:
            return AnalysisOutput(success=False, message="求解器返回空结果，请检查电路连通性")

        # 过滤 NaN/Inf 结果
        import math
        voltages = {k: v for k, v in voltages.items() if math.isfinite(v)}

        print(f"[DEBUG] 有效电压节点数: {len(voltages)}")
        if voltages:
            vvals = list(voltages.values())
            print(f"[DEBUG] 电压范围: min={min(vvals):.6f}V, max={max(vvals):.6f}V")
        # 检查 source/load 节点是否有电压
        for s in data.sources:
            print(f"[DEBUG] source {s.node_id} voltage={voltages.get(s.node_id, 'NOT FOUND')}")
        for l in data.loads[:5]:  # 只打印前5个
            print(f"[DEBUG] load {l.node_id} voltage={voltages.get(l.node_id, 'NOT FOUND')}")

        net_results = compute_net_results(data, voltages)
        print(f"[DEBUG] net_results: {[(r.net, r.max_drop, r.min_voltage, r.max_voltage) for r in net_results]}")
        # 收集所有铜箔层 ID（走线 + 铺铜层）
        all_layers = sorted(
            set(r.layer for r in data.resistances if r.layer is not None and r.layer > 0)
            | set(p.layer for p in data.copper_pours if p.layer is not None and p.layer > 0)
        )
        plot_images = generate_plot_images(mesh_points, data.mesh_resolution or 0.5, all_layers, net_results, data.max_drop_pct or 5.0)

        overall_max_drop = max((r.max_drop for r in net_results), default=0.0)
        total_current = sum(l.current for l in data.loads)

        results = AnalysisResults(
            max_drop=round(overall_max_drop, 6),
            avg_current=round(total_current, 6),
            net_results=net_results,
            plot_images=plot_images,
        )

        output = AnalysisOutput(
            success=True,
            message=f"分析完成。{len(data.nodes)} 个节点, {len(data.resistances)} 个电阻, 耗时 {time.time()-start_time:.3f}s",
            results=results,
        )

        analysis_state["last_result"] = output.model_dump()
        print(f"[KiPIDA] 分析完成: 最大压降={overall_max_drop:.6f}V, 耗时={time.time()-start_time:.3f}s")
        return output

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[KiPIDA] 分析错误: {tb}")
        return AnalysisOutput(success=False, message=f"分析失败: {str(e)}\n{tb}")


@app.get("/status")
async def get_status():
    return {
        "status": "running",
        "total_requests": analysis_state["total_requests"],
        "has_last_input": analysis_state["last_input"] is not None,
        "has_last_result": analysis_state["last_result"] is not None,
        "kipida_path": KIPIDA_PATH,
    }


@app.get("/last-result")
async def get_last_result():
    if analysis_state["last_result"] is None:
        raise HTTPException(status_code=404, detail="没有分析结果")
    return analysis_state["last_result"]


@app.get("/last-input")
async def get_last_input():
    if analysis_state["last_input"] is None:
        raise HTTPException(status_code=404, detail="没有输入数据")
    return analysis_state["last_input"]


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    import uvicorn
    print("=" * 50)
    print("KiPIDA Bridge API 服务启动中...")
    print(f"KiPIDA 路径: {KIPIDA_PATH}")
    print("访问地址: http://localhost:5000")
    print("API 文档: http://localhost:5000/docs")
    print("=" * 50)
    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=True, log_level="info")
