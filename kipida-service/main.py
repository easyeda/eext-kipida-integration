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
    mesh_resolution: Optional[float] = 0.5
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

def build_mesh_and_solve(data: KipidaInput) -> Dict[str, float]:
    """
    将 EasyEDA 数据转换为 KiPIDA Mesh，调用真实 Solver 求解。
    只处理有电压源的 net，避免孤立子图导致矩阵奇异。
    返回 { node_id_str: voltage } 映射。
    """
    from mesh import Mesh
    from solver import Solver

    # 1. 找出有 Source 的 net
    source_node_ids = {s.node_id for s in data.sources}
    load_node_ids = {l.node_id for l in data.loads}
    active_node_ids = source_node_ids | load_node_ids

    # 找 source/load 节点所属的 net
    active_nets: set = set()
    for node in data.nodes:
        if node.id in active_node_ids:
            active_nets.add(node.net)

    if not active_nets:
        raise ValueError("没有有效的电压源节点，无法求解")

    print(f"[DEBUG] 活跃 nets: {active_nets}")

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

    print(f"[DEBUG] 过滤后节点数: {len(unique_nodes)}")

    # 3. 构建 Mesh，node_coords 统一存 (x, y, layer)
    # 额外维护 node_net dict 记录每个节点所属 net（含插值节点）
    m = Mesh()
    node_net: Dict[int, str] = {}
    m.nodes = list(range(len(unique_nodes)))
    for node in unique_nodes:
        idx = str_to_int[node.id]
        m.node_coords[idx] = (node.x, node.y, node.layer if node.layer is not None else -1)
        node_net[idx] = node.net

    # 4. 添加电阻边（按 mesh_resolution 插值，单位 mil）
    import math as _math
    MIL_TO_MM = 0.0254
    res_mm = (data.mesh_resolution or 0.5)
    res_mil = res_mm / MIL_TO_MM
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
        length_mil = _math.sqrt((vx - ux)**2 + (vy - uy)**2)
        n_seg = max(1, round(length_mil / res_mil))

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
    net_junctions: Dict[str, list] = {}
    for node in unique_nodes:
        if node.type == 'junction':
            net_junctions.setdefault(node.net, []).append(node)

    G_PAD = 1e6
    for node in unique_nodes:
        if node.type not in ('pad', 'via'):
            continue
        junctions = net_junctions.get(node.net, [])
        if not junctions:
            continue
        best = min(junctions, key=lambda j: (j.x - node.x)**2 + (j.y - node.y)**2)
        u = str_to_int[node.id]
        v = str_to_int[best.id]
        if u != v:
            m.add_edge_direct(u, v, G_PAD)

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

    print(f"[DEBUG] 连通分量数={n_comp}, 含Source的分量={source_comps}")

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

    print(f"[DEBUG] 过滤后节点={len(keep_indices)}, sources={len(sources2)}, loads={len(loads2)}")

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
    print(f"[DEBUG] 求解完成: 有效电压={valid_count}/{len(keep_indices)}")

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

    print(f"[DEBUG] mesh_points={len(mesh_points)}, NaN={nan_count}, pad有效={pad_valid}, pad_NaN={pad_nan}")
    return result, mesh_points


def generate_plot_images(mesh_points: list, mesh_resolution: float = 0.5, all_layers: list = None) -> Dict[str, 'NetPlotImages']:
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

        # 统计 layer 分布（调试用）
        from collections import Counter
        layer_dist = Counter(p[2] for p in points)
        print(f"[DEBUG] net={net} 总点数={len(points)}, layer分布={dict(layer_dist)}")

        # 过滤掉 layer=0，保留 layer=-1（via 哨兵值）和正常层
        valid_points = [p for p in points if p[2] != 0]
        if not valid_points:
            valid_points = points

        xs = [p[0] for p in valid_points]
        ys = [p[1] for p in valid_points]
        vs = [p[3] for p in valid_points]
        vmin, vmax = min(vs), max(vs)
        if abs(vmax - vmin) < 1e-9:
            vmin -= 0.001; vmax += 0.001

        net_img = NetPlotImages()

        # ── 3D 散点图 ─────────────────────────────────────────
        layer_ids = sorted(set(p[2] for p in valid_points))
        layer_to_z = {lid: 10.0 - i for i, lid in enumerate(layer_ids)}
        zs_mapped = [layer_to_z[p[2]] for p in valid_points]

        fig = plt.figure(figsize=(7, 5), constrained_layout=True)
        ax = fig.add_subplot(111, projection='3d')
        sc = ax.scatter(xs, ys, zs_mapped, c=vs, cmap='viridis', vmin=vmin, vmax=vmax, s=18)
        plt.colorbar(sc, ax=ax, label='Voltage (V)', shrink=0.7)
        ax.set_title(f"Rail: {net}  Drop: {vmax-vmin:.4f} V")
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
            ax2.set_title(f"Rail: {net}  Layer {layer_id}  Drop: {vmax-vmin:.4f} V")
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

    results = []
    for net, vs in net_voltages.items():
        min_v = min(vs)
        max_v = max(vs)
        drop = max_v - min_v
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

        net_results = compute_net_results(data, voltages)
        # 收集所有铜箔层 ID（从电阻数据中提取）
        all_layers = sorted(set(r.layer for r in data.resistances if r.layer is not None and r.layer > 0))
        plot_images = generate_plot_images(mesh_points, data.mesh_resolution or 0.5, all_layers)

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
