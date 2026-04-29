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
    width: Optional[float] = None
    height: Optional[float] = None


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

MIL_TO_MM = 0.0254
COPPER_RESISTIVITY = 1.68e-5   # Ω·mm (match KiPIDA)
COPPER_THICKNESS = 0.035       # mm (1oz)
GRID_SIZE_MM = 0.5             # match KiPIDA default
VIA_PLATING_THICKNESS = 0.025  # mm
SUBSTRATE_HEIGHT = 0.5         # mm (default inter-layer distance)


def _snap_to_grid(mesh, x_mm, y_mm, layer, net, grid_size_mm, grid_origin):
    """Find nearest mesh grid node for a specific net. Returns node_id or None."""
    ix = int(round((x_mm - grid_origin[0]) / grid_size_mm))
    iy = int(round((y_mm - grid_origin[1]) / grid_size_mm))

    nid = mesh.node_map.get((ix, iy, layer, net))
    if nid is not None:
        return nid

    for dx in range(-1, 2):
        for dy in range(-1, 2):
            if dx == 0 and dy == 0:
                continue
            nid = mesh.node_map.get((ix + dx, iy + dy, layer, net))
            if nid is not None:
                return nid

    for r in range(2, 6):
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if abs(dx) < r and abs(dy) < r:
                    continue
                nid = mesh.node_map.get((ix + dx, iy + dy, layer, net))
                if nid is not None:
                    return nid
    return None


def _snap_to_grid_any_layer(mesh, x_mm, y_mm, all_layers, net, grid_size_mm, grid_origin):
    """Snap to nearest grid node across all layers for a specific net. Returns node_id or None."""
    for layer in all_layers:
        nid = _snap_to_grid(mesh, x_mm, y_mm, layer, net, grid_size_mm, grid_origin)
        if nid is not None:
            return nid
    return None


def _build_geometry(data, active_nets):
    """
    Build Shapely geometry per (net, layer) from EasyEDA data.
    Coordinates converted from mil to mm.
    Returns Dict[(net, layer), Polygon/MultiPolygon].
    """
    from shapely.geometry import Polygon, LineString, box
    from shapely.ops import unary_union

    node_by_id = {n.id: n for n in data.nodes}
    geom_lists: Dict[tuple, list] = {}

    def _add(net, layer, poly):
        if poly is not None and not poly.is_empty:
            geom_lists.setdefault((net, layer), []).append(poly)

    for pour in data.copper_pours:
        if pour.net not in active_nets or len(pour.vertices) < 3:
            continue
        coords = [(v['x'] * MIL_TO_MM, v['y'] * MIL_TO_MM) for v in pour.vertices]
        try:
            poly = Polygon(coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            _add(pour.net, pour.layer, poly)
        except Exception as e:
            print(f"[FEM] copper pour polygon failed net={pour.net}: {e}")

    for res in data.resistances:
        if res.net not in active_nets or res.layer is None or res.layer <= 0:
            continue
        sn = node_by_id.get(res.start_node)
        en = node_by_id.get(res.end_node)
        if sn is None or en is None:
            continue
        x1, y1 = sn.x * MIL_TO_MM, sn.y * MIL_TO_MM
        x2, y2 = en.x * MIL_TO_MM, en.y * MIL_TO_MM
        half_w = res.width * MIL_TO_MM / 2
        if half_w < 1e-6:
            half_w = 0.05
        try:
            line = LineString([(x1, y1), (x2, y2)])
            track_poly = line.buffer(half_w, cap_style=2)
            _add(res.net, res.layer, track_poly)
        except Exception:
            pass

    for node in data.nodes:
        if node.net not in active_nets or node.type != 'pad':
            continue
        w_mm = (node.width or 0) * MIL_TO_MM
        h_mm = (node.height or 0) * MIL_TO_MM
        if w_mm < 1e-6 or h_mm < 1e-6:
            continue
        cx, cy = node.x * MIL_TO_MM, node.y * MIL_TO_MM
        pad_poly = box(cx - w_mm / 2, cy - h_mm / 2, cx + w_mm / 2, cy + h_mm / 2)
        layer = node.layer if node.layer and node.layer > 0 else None
        if layer is not None:
            _add(node.net, layer, pad_poly)
        else:
            for key in list(geom_lists.keys()):
                if key[0] == node.net:
                    _add(node.net, key[1], pad_poly)

    result = {}
    for key, polys in geom_lists.items():
        try:
            merged = unary_union(polys)
            if not merged.is_empty:
                result[key] = merged
        except Exception as e:
            print(f"[FEM] union failed {key}: {e}")
    return result


def _rasterize(geometry, grid_size_mm):
    """
    Rasterize geometry onto a regular grid, create Mesh with lateral conductance edges.
    Replicates KiPIDA Mesher logic.
    Returns (mesh, node_net_map, grid_origin, sorted_layers).
    """
    import numpy as np
    import matplotlib.path
    from mesh import Mesh
    import math as _math

    mesh = Mesh()
    mesh.grid_step = grid_size_mm
    node_net_map: Dict[int, str] = {}

    if not geometry:
        return mesh, node_net_map, (0, 0), []

    min_x = min_y = float('inf')
    max_x = max_y = float('-inf')
    for (net, layer), poly in geometry.items():
        b = poly.bounds
        min_x = min(min_x, b[0])
        min_y = min(min_y, b[1])
        max_x = max(max_x, b[2])
        max_y = max(max_y, b[3])

    pad = grid_size_mm
    min_x -= pad
    min_y -= pad
    max_x += pad
    max_y += pad
    grid_origin = (min_x, min_y)
    mesh.grid_origin = grid_origin

    nx = int(_math.ceil((max_x - min_x) / grid_size_mm))
    ny = int(_math.ceil((max_y - min_y) / grid_size_mm))
    x_coords = np.linspace(min_x, min_x + nx * grid_size_mm, nx + 1)
    y_coords = np.linspace(min_y, min_y + ny * grid_size_mm, ny + 1)
    xv, yv = np.meshgrid(x_coords, y_coords)
    grid_points = np.column_stack((xv.ravel(), yv.ravel()))

    all_layers = sorted(set(layer for (net, layer) in geometry.keys()))
    all_nets = sorted(set(net for (net, layer) in geometry.keys()))

    g_lat = COPPER_THICKNESS / COPPER_RESISTIVITY

    node_counter = 0

    for net in all_nets:
        net_layers = sorted(layer for (n, layer) in geometry.keys() if n == net)
        for layer_id in net_layers:
            poly = geometry.get((net, layer_id))
            if poly is None or poly.is_empty:
                continue

            polys_to_check = [poly] if poly.geom_type == 'Polygon' else list(poly.geoms)
            layer_mask = np.zeros(len(grid_points), dtype=bool)

            for p in polys_to_check:
                pb = p.buffer(1e-5)
                if pb.is_empty:
                    continue
                codes = []
                verts = []
                ext_coords = list(pb.exterior.coords)
                verts.extend(ext_coords)
                codes.append(matplotlib.path.Path.MOVETO)
                codes.extend([matplotlib.path.Path.LINETO] * (len(ext_coords) - 2))
                codes.append(matplotlib.path.Path.CLOSEPOLY)
                for interior in pb.interiors:
                    int_coords = list(interior.coords)
                    verts.extend(int_coords)
                    codes.append(matplotlib.path.Path.MOVETO)
                    codes.extend([matplotlib.path.Path.LINETO] * (len(int_coords) - 2))
                    codes.append(matplotlib.path.Path.CLOSEPOLY)
                path = matplotlib.path.Path(verts, codes)
                mask = path.contains_points(grid_points, radius=1e-9)
                layer_mask |= mask

            mask_2d = layer_mask.reshape((ny + 1, nx + 1))
            count_on_layer = np.count_nonzero(mask_2d)
            if count_on_layer == 0:
                continue

            y_idxs, x_idxs = np.nonzero(mask_2d)
            new_ids = np.arange(node_counter, node_counter + count_on_layer)
            mesh.nodes.extend(new_ids.tolist())

            for i in range(count_on_layer):
                nid = int(new_ids[i])
                xi = int(x_idxs[i])
                yi = int(y_idxs[i])
                mesh.node_map[(xi, yi, layer_id, net)] = nid
                mesh.node_coords[nid] = (
                    float(min_x + xi * grid_size_mm),
                    float(min_y + yi * grid_size_mm),
                    layer_id,
                )
                node_net_map[nid] = net

            node_counter += count_on_layer

            # node_grid for vectorized neighbor lookup
            node_grid = np.full((ny + 1, nx + 1), -1, dtype=np.int64)
            node_grid[y_idxs, x_idxs] = new_ids

            right_mask = mask_2d[:, :-1] & mask_2d[:, 1:]
            if np.any(right_mask):
                yr, xr = np.nonzero(right_mask)
                u_ids = node_grid[yr, xr]
                v_ids = node_grid[yr, xr + 1]
                for u, v in zip(u_ids, v_ids):
                    mesh.add_edge_direct(int(u), int(v), g_lat)

            top_mask = mask_2d[:-1, :] & mask_2d[1:, :]
            if np.any(top_mask):
                yt, xt = np.nonzero(top_mask)
                u_ids = node_grid[yt, xt]
                v_ids = node_grid[yt + 1, xt]
                for u, v in zip(u_ids, v_ids):
                    mesh.add_edge_direct(int(u), int(v), g_lat)

            print(f"[FEM] net={net} layer={layer_id}: {count_on_layer} grid nodes")

    print(f"[FEM] Rasterization done: {node_counter} total nodes, grid {nx+1}x{ny+1}")
    return mesh, node_net_map, grid_origin, all_layers


def _add_vias(mesh, data, active_nets, all_layers, grid_size_mm, grid_origin):
    """Add vertical via/PTH connections between layers."""
    import math as _math

    if len(all_layers) < 2:
        return

    for node in data.nodes:
        if node.net not in active_nets:
            continue
        is_via = node.type == 'via'
        is_pth = node.type == 'pad' and (node.layer is None or node.layer <= 0)
        if not is_via and not is_pth:
            continue

        x_mm = node.x * MIL_TO_MM
        y_mm = node.y * MIL_TO_MM
        dia_mm = (node.width or 20) * MIL_TO_MM

        nodes_in_stack = []
        for layer_id in all_layers:
            nid = _snap_to_grid(mesh, x_mm, y_mm, layer_id, node.net, grid_size_mm, grid_origin)
            if nid is not None:
                nodes_in_stack.append((layer_id, nid))

        for i in range(len(nodes_in_stack) - 1):
            la, nid_a = nodes_in_stack[i]
            lb, nid_b = nodes_in_stack[i + 1]
            area = _math.pi * (dia_mm * VIA_PLATING_THICKNESS - VIA_PLATING_THICKNESS ** 2)
            if area <= 0:
                area = 1e-6
            g_via = area / (COPPER_RESISTIVITY * SUBSTRATE_HEIGHT)
            mesh.add_edge_direct(nid_a, nid_b, g_via)

    # Also connect junction nodes that bridge layers (track layer transitions)
    from collections import defaultdict
    junction_by_coord: Dict[tuple, list] = defaultdict(list)
    for node in data.nodes:
        if node.net not in active_nets or node.type != 'junction':
            continue
        if node.layer is None or node.layer <= 0:
            continue
        x_mm = node.x * MIL_TO_MM
        y_mm = node.y * MIL_TO_MM
        key = (node.net, round(x_mm, 4), round(y_mm, 4))
        junction_by_coord[key].append((node.layer, x_mm, y_mm))

    for key, layer_entries in junction_by_coord.items():
        if len(layer_entries) < 2:
            continue
        net = key[0]
        layer_entries.sort()
        nids = []
        for layer_id, x_mm, y_mm in layer_entries:
            nid = _snap_to_grid(mesh, x_mm, y_mm, layer_id, net, grid_size_mm, grid_origin)
            if nid is not None:
                nids.append((layer_id, nid))
        for i in range(len(nids) - 1):
            la, nid_a = nids[i]
            lb, nid_b = nids[i + 1]
            g_via = 1e4
            mesh.add_edge_direct(nid_a, nid_b, g_via)


def _log_input_summary(data: 'KipidaInput') -> None:
    """打印结构化输入摘要，用于与 KiCad 原始数据对比，验证传输完整性。"""
    sep = "=" * 60

    # ── 1. 总览 ──────────────────────────────────────────────
    print(sep)
    print("[INPUT] 总览")
    print(f"  nodes        : {len(data.nodes)}")
    print(f"  resistances  : {len(data.resistances)}")
    print(f"  connections  : {len(data.connections)}")
    print(f"  sources      : {len(data.sources)}")
    print(f"  loads        : {len(data.loads)}")
    print(f"  copper_pours : {len(data.copper_pours)}")
    if data.metadata:
        m = data.metadata
        print(f"  metadata     : nets={m.total_nets} tracks={m.total_tracks} vias={m.total_vias} pads={m.total_pads}")

    # ── 2. 节点分类 ──────────────────────────────────────────
    from collections import defaultdict
    type_count: dict = defaultdict(int)
    net_node_count: dict = defaultdict(int)
    for n in data.nodes:
        type_count[n.type] += 1
        net_node_count[n.net] += 1
    print("\n[INPUT] 节点类型分布")
    for t, c in sorted(type_count.items()):
        print(f"  {t:10s}: {c}")
    print("\n[INPUT] 各 net 节点数")
    for net, c in sorted(net_node_count.items()):
        print(f"  {net:20s}: {c}")

    # ── 3. 焊盘详情 ──────────────────────────────────────────
    pads = [n for n in data.nodes if n.type == 'pad']
    print(f"\n[INPUT] 焊盘 ({len(pads)} 个)")
    for p in pads:
        w = p.width or 0.0
        h = p.height or 0.0
        print(f"  net={p.net:20s} layer={str(p.layer):4s} pos=({p.x:.1f},{p.y:.1f})mil  size={w:.1f}x{h:.1f}mil")

    # ── 4. 过孔详情 ──────────────────────────────────────────
    vias = [n for n in data.nodes if n.type == 'via']
    print(f"\n[INPUT] 过孔 ({len(vias)} 个)")
    for v in vias:
        d = v.width or 0.0
        print(f"  net={v.net:20s} pos=({v.x:.1f},{v.y:.1f})mil  diameter={d:.1f}mil")

    # ── 5. 走线电阻统计 ──────────────────────────────────────
    print(f"\n[INPUT] 走线电阻 ({len(data.resistances)} 条)")
    net_res: dict = defaultdict(list)
    for r in data.resistances:
        net_res[r.net].append(r)
    for net, rs in sorted(net_res.items()):
        widths = [r.width for r in rs]
        lengths = [r.length for r in rs]
        layers = sorted(set(r.layer for r in rs))
        print(f"  net={net:20s} count={len(rs):4d}  width=[{min(widths):.2f},{max(widths):.2f}]mil"
              f"  length=[{min(lengths):.1f},{max(lengths):.1f}]mil  layers={layers}")

    # ── 6. 电源 / 负载 ───────────────────────────────────────
    print(f"\n[INPUT] Sources ({len(data.sources)} 个)")
    for s in data.sources:
        node = next((n for n in data.nodes if n.id == s.node_id), None)
        net = node.net if node else "?"
        print(f"  node={s.node_id}  net={net}  voltage={s.voltage}V")

    print(f"\n[INPUT] Loads ({len(data.loads)} 个)")
    net_load: dict = defaultdict(list)
    for l in data.loads:
        node = next((n for n in data.nodes if n.id == l.node_id), None)
        net = node.net if node else "?"
        net_load[net].append(l.current)
    for net, currents in sorted(net_load.items()):
        print(f"  net={net:20s} count={len(currents):4d}  total={sum(currents):.3f}A  each={currents[0]:.3f}A")

    # ── 7. 铺铜 ──────────────────────────────────────────────
    if data.copper_pours:
        print(f"\n[INPUT] 铺铜 ({len(data.copper_pours)} 个)")
        for cp in data.copper_pours:
            print(f"  net={cp.net:20s} layer={cp.layer}  vertices={len(cp.vertices)}")

    print(sep)


def build_mesh_and_solve(data: KipidaInput) -> Dict[str, float]:
    """
    FEM grid mesh approach: build Shapely geometry from EasyEDA data,
    rasterize onto a regular grid, solve with KiPIDA Solver.
    Returns (voltages_by_str_id, mesh_points_for_plotting).
    """
    from mesh import Mesh
    from solver import Solver
    import numpy as np
    import scipy.sparse
    from scipy.sparse.csgraph import connected_components
    import math as _math

    _log_input_summary(data)

    # 1. Active nets
    source_node_ids = {s.node_id for s in data.sources}
    load_node_ids = {l.node_id for l in data.loads}
    active_node_ids = source_node_ids | load_node_ids
    active_nets: set = set()
    for node in data.nodes:
        if node.id in active_node_ids:
            active_nets.add(node.net)
    if not active_nets:
        raise ValueError("没有有效的电压源节点，无法求解")

    node_by_id = {n.id: n for n in data.nodes}

    # 2. Build geometry & rasterize
    geometry = _build_geometry(data, active_nets)
    if not geometry:
        raise ValueError("无法从输入数据构建铜皮几何")

    grid_size_mm = data.mesh_resolution or GRID_SIZE_MM
    mesh, node_net_map, grid_origin, all_layers = _rasterize(geometry, grid_size_mm)

    if not mesh.nodes:
        raise ValueError("栅格化后没有网格节点")

    # 3. Via connections
    _add_vias(mesh, data, active_nets, all_layers, grid_size_mm, grid_origin)

    # 4. Snap source/load nodes to nearest grid nodes
    sources = []
    source_snap = {}
    for s in data.sources:
        orig = node_by_id.get(s.node_id)
        if orig is None:
            continue
        x_mm = orig.x * MIL_TO_MM
        y_mm = orig.y * MIL_TO_MM
        layer = orig.layer if orig.layer and orig.layer > 0 else None
        if layer is not None:
            nid = _snap_to_grid(mesh, x_mm, y_mm, layer, orig.net, grid_size_mm, grid_origin)
        else:
            nid = _snap_to_grid_any_layer(mesh, x_mm, y_mm, all_layers, orig.net, grid_size_mm, grid_origin)
        if nid is not None:
            sources.append({"node_id": nid, "voltage": s.voltage})
            source_snap[s.node_id] = nid
            print(f"[FEM] Source {s.node_id} -> grid node {nid}")

    loads = []
    load_snap = {}
    for l in data.loads:
        orig = node_by_id.get(l.node_id)
        if orig is None:
            continue
        x_mm = orig.x * MIL_TO_MM
        y_mm = orig.y * MIL_TO_MM
        layer = orig.layer if orig.layer and orig.layer > 0 else None
        if layer is not None:
            nid = _snap_to_grid(mesh, x_mm, y_mm, layer, orig.net, grid_size_mm, grid_origin)
        else:
            nid = _snap_to_grid_any_layer(mesh, x_mm, y_mm, all_layers, orig.net, grid_size_mm, grid_origin)
        if nid is not None:
            loads.append({"node_id": nid, "current": l.current})
            load_snap[l.node_id] = nid

    if not sources:
        raise ValueError("没有有效的电压源节点可以 snap 到网格")

    print(f"[FEM] Snapped: {len(sources)} sources, {len(loads)} loads")

    # 5. Connected component filtering
    N = len(mesh.nodes)
    if mesh.G_coo_data:
        rows = np.array(mesh.G_coo_row)
        cols = np.array(mesh.G_coo_col)
        data_arr = np.ones(len(rows))
        adj = scipy.sparse.coo_matrix((data_arr, (rows, cols)), shape=(N, N))
        adj = adj + adj.T
        n_comp, labels = connected_components(adj, directed=False)
    else:
        n_comp, labels = 1, np.zeros(N, dtype=int)

    source_comps = set()
    for s in sources:
        source_comps.add(int(labels[s['node_id']]))

    print(f"[FEM] Connected components: {n_comp}, with source: {source_comps}")

    keep_mask = np.array([labels[i] in source_comps for i in range(N)])
    keep_indices = np.where(keep_mask)[0]
    keep_set = set(keep_indices.tolist())

    if len(keep_indices) == 0:
        raise ValueError("Source 节点所在连通分量为空")

    old_to_new = {int(old): new for new, old in enumerate(keep_indices)}

    m2 = Mesh()
    m2.nodes = list(range(len(keep_indices)))
    for old_idx in keep_indices:
        m2.node_coords[old_to_new[int(old_idx)]] = mesh.node_coords[int(old_idx)]

    for r, c, d in zip(mesh.G_coo_row, mesh.G_coo_col, mesh.G_coo_data):
        if r in keep_set and c in keep_set:
            m2.G_coo_row.append(old_to_new[r])
            m2.G_coo_col.append(old_to_new[c])
            m2.G_coo_data.append(d)

    sources2 = [{"node_id": old_to_new[s["node_id"]], "voltage": s["voltage"]}
                for s in sources if s["node_id"] in keep_set]
    loads2 = [{"node_id": old_to_new[l["node_id"]], "current": l["current"]}
              for l in loads if l["node_id"] in keep_set]

    print(f"[FEM] Solving: {len(keep_indices)} nodes, {len(sources2)} sources, {len(loads2)} loads")

    # 6. Solve
    solver = Solver(debug=False)
    int_voltages = solver.solve(m2, sources2, loads2)

    # 7. Map voltages back to original string node IDs
    snap_map = {}
    snap_map.update(source_snap)
    snap_map.update(load_snap)

    for node in data.nodes:
        if node.net not in active_nets or node.id in snap_map:
            continue
        x_mm = node.x * MIL_TO_MM
        y_mm = node.y * MIL_TO_MM
        layer = node.layer if node.layer and node.layer > 0 else None
        if layer is not None:
            nid = _snap_to_grid(mesh, x_mm, y_mm, layer, node.net, grid_size_mm, grid_origin)
        else:
            nid = _snap_to_grid_any_layer(mesh, x_mm, y_mm, all_layers, node.net, grid_size_mm, grid_origin)
        if nid is not None:
            snap_map[node.id] = nid

    voltages = {}
    for str_id, grid_nid in snap_map.items():
        if grid_nid not in keep_set:
            continue
        new_idx = old_to_new[grid_nid]
        v = int_voltages.get(new_idx)
        if v is not None and _math.isfinite(v):
            voltages[str_id] = v

    valid_count = len(voltages)
    print(f"[FEM] Solved: {valid_count} original nodes mapped, {len(int_voltages)} total grid voltages")

    # 8. Build mesh_points for plotting (coordinates in mil)
    mesh_points = []
    for new_idx, voltage in int_voltages.items():
        if not _math.isfinite(voltage):
            continue
        old_idx = int(keep_indices[new_idx])
        coords = mesh.node_coords.get(old_idx)
        if coords is None:
            continue
        x_mm, y_mm, layer = coords
        x_mil = x_mm / MIL_TO_MM
        y_mil = y_mm / MIL_TO_MM
        net = node_net_map.get(old_idx, '')
        if not net:
            continue
        mesh_points.append((x_mil, y_mil, layer, net, voltage, 'junction', 0.0, 0.0))

    for node in data.nodes:
        if node.id not in voltages or node.type not in ('pad', 'via'):
            continue
        v = voltages[node.id]
        w = node.width or 0.0
        h = node.height or 0.0
        layer = node.layer if node.layer and node.layer > 0 else (all_layers[0] if all_layers else 1)
        mesh_points.append((node.x, node.y, layer, node.net, v, node.type, w, h))

    return voltages, mesh_points


def generate_plot_images(mesh_points: list, mesh_resolution: float = 0.5, all_layers: list = None, net_results: list = None, max_drop_pct: float = 5.0, resistances=None, node_voltages=None, nodes=None) -> Dict[str, 'NetPlotImages']:
    """
    mesh_points: [(x_mil, y_mil, layer, net, voltage), ...]
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    from matplotlib.patches import Rectangle, Circle
    from matplotlib.colors import Normalize
    from matplotlib.cm import ScalarMappable
    from matplotlib.collections import LineCollection
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

    # Pre-compute track segments per net/layer from resistances
    # track_segs[net][layer] = [(x1,y1,x2,y2,width_mm,avg_voltage), ...]
    track_segs: Dict[str, Dict[int, list]] = {}
    if resistances and node_voltages and nodes:
        node_pos = {n.id: (n.x * MIL_TO_MM, n.y * MIL_TO_MM) for n in nodes}
        for res in resistances:
            p1 = node_pos.get(res.start_node)
            p2 = node_pos.get(res.end_node)
            if not p1 or not p2:
                continue
            v1 = node_voltages.get(res.start_node, float('nan'))
            v2 = node_voltages.get(res.end_node, float('nan'))
            if not math.isfinite(v1) and not math.isfinite(v2):
                continue
            avg_v = v1 if math.isfinite(v1) else v2
            if math.isfinite(v1) and math.isfinite(v2):
                avg_v = (v1 + v2) / 2
            w_mm = res.width * MIL_TO_MM
            track_segs.setdefault(res.net, {}).setdefault(res.layer, []).append(
                (p1[0], p1[1], p2[0], p2[1], w_mm, avg_v)
            )

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
        pad_w = item[6] if len(item) > 6 else 0.0
        pad_h = item[7] if len(item) > 7 else 0.0
        if not math.isfinite(voltage) or not net:
            continue
        x_mm = x_mil * MIL_TO_MM
        y_mm = y_mil * MIL_TO_MM
        w_mm = pad_w * MIL_TO_MM
        h_mm = pad_h * MIL_TO_MM
        net_points.setdefault(net, []).append((x_mm, y_mm, layer, voltage, node_type, w_mm, h_mm))

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

        # ── 3D 散点图（快速，表达层分布与电压分布）─────────────
        # 几何精度（走线宽度/焊盘/过孔尺寸）在 2D 层图中体现
        all_layer_ids = sorted(set(p[2] for p in valid_points if p[2] != -1))
        all_layer_pts_3d = [p for p in valid_points if p[2] == -1]
        layer_to_z = {lid: float(len(all_layer_ids) - i) for i, lid in enumerate(all_layer_ids)}

        fig = plt.figure(figsize=(8, 6), constrained_layout=True)
        ax = fig.add_subplot(111, projection='3d')

        for layer_id in all_layer_ids:
            z = layer_to_z[layer_id]
            layer_pts = [p for p in valid_points if p[2] == layer_id] + all_layer_pts_3d
            if not layer_pts:
                continue
            lxs = [p[0] for p in layer_pts]
            lys = [p[1] for p in layer_pts]
            lvs = [p[3] for p in layer_pts]
            sizes = [30 if p[4] in ('pad', 'via') else 8 for p in layer_pts]
            ax.scatter(lxs, lys, [z] * len(lxs), c=lvs, cmap='viridis', vmin=vmin, vmax=vmax, s=sizes)

        sm = ScalarMappable(cmap=plt.cm.viridis, norm=Normalize(vmin=vmin, vmax=vmax))
        sm.set_array([])
        plt.colorbar(sm, ax=ax, label='Voltage (V)', shrink=0.7)
        ax.set_title(f"Rail: {net}  Drop: {actual_drop:.4f} V")
        ax.set_xlabel('X (mm)'); ax.set_ylabel('Y (mm)'); ax.set_zlabel('Layer')
        if all_layer_ids:
            ax.set_zticks(list(layer_to_z.values()))
            ax.set_zticklabels([str(lid) for lid in all_layer_ids])
        net_img.view_3d = fig_to_b64(fig, f"{net}_3d.png")

        # ── 各层 2D 图 ────────────────────────────────────────
        # via 节点 (layer=-1)：出现在所有层
        # pad 节点 (layer>0)：只出现在自己所属的层
        # junction/插值节点 (layer>0)：只出现在自己所属的层
        # 绘制范围：all_layers（含只有过孔的层）
        # layer=-1 means spans all layers (via or through-hole pad)
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection

        all_layer_pts = [p for p in valid_points if p[2] == -1]
        norm = Normalize(vmin=vmin, vmax=vmax)
        cmap_obj = plt.cm.viridis

        for layer_id in sorted(all_layers):
            specific_pts = [p for p in valid_points if p[2] == layer_id]
            layer_all = specific_pts + all_layer_pts
            if not layer_all and not track_segs.get(net, {}).get(layer_id):
                continue

            fig2, ax2 = plt.subplots(figsize=(7, 5), constrained_layout=True)

            # ── 走线（数据坐标填充矩形，按实际宽度）────────────────
            segs = track_segs.get(net, {}).get(layer_id, [])
            if segs:
                import numpy as np
                for s in segs:
                    x1, y1, x2, y2, w_mm, avg_v = s
                    dx, dy = x2 - x1, y2 - y1
                    seg_len = math.sqrt(dx * dx + dy * dy)
                    color = cmap_obj(norm(avg_v))
                    hw = w_mm / 2
                    if seg_len >= 1e-9:
                        px, py = -dy / seg_len, dx / seg_len
                        corners = [
                            (x1 + px * hw, y1 + py * hw),
                            (x2 + px * hw, y2 + py * hw),
                            (x2 - px * hw, y2 - py * hw),
                            (x1 - px * hw, y1 - py * hw),
                        ]
                        ax2.add_patch(plt.Polygon(corners, facecolor=color, edgecolor='none', zorder=1))
                    # 圆形端帽：填充转折处三角缺口
                    ax2.add_patch(Circle((x1, y1), hw, facecolor=color, edgecolor='none', zorder=1))
                    ax2.add_patch(Circle((x2, y2), hw, facecolor=color, edgecolor='none', zorder=1))

            # ── 焊盘（Rectangle）和过孔（Circle）────────────────
            def _is_rect_pad(p):
                return p[4] == 'pad' and len(p) > 6 and p[5] > 0 and p[6] > 0

            def _is_via(p):
                return p[4] == 'via' and len(p) > 6 and p[5] > 0

            pad_pts   = [p for p in layer_all if _is_rect_pad(p)]
            via_pts   = [p for p in layer_all if _is_via(p)]
            other_pts = [p for p in layer_all if not _is_rect_pad(p) and not _is_via(p)]

            for p in pad_pts:
                px, py, pv, pw, ph = p[0], p[1], p[3], p[5], p[6]
                color = cmap_obj(norm(pv))
                ax2.add_patch(Rectangle(
                    (px - pw / 2, py - ph / 2), pw, ph,
                    facecolor=color, edgecolor='none', zorder=3
                ))

            for p in via_pts:
                px, py, pv, pr = p[0], p[1], p[3], p[5] / 2
                color = cmap_obj(norm(pv))
                ax2.add_patch(Circle(
                    (px, py), pr,
                    facecolor=color, edgecolor='none', zorder=3
                ))

            if other_pts:
                lxs = [p[0] for p in other_pts]
                lys = [p[1] for p in other_pts]
                lvs = [p[3] for p in other_pts]
                ax2.scatter(lxs, lys, c=lvs, cmap='viridis', vmin=vmin, vmax=vmax, s=10, zorder=2)

            sm = ScalarMappable(cmap=cmap_obj, norm=norm)
            sm.set_array([])
            plt.colorbar(sm, ax=ax2, label='Voltage (V)')
            ax2.set_title(f"Rail: {net}  Layer {layer_id}  Drop: {actual_drop:.4f} V")
            ax2.set_xlabel('X (mm)'); ax2.set_ylabel('Y (mm)')
            ax2.set_aspect('equal', 'box')
            ax2.autoscale_view()
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

        net_results = compute_net_results(data, voltages)
        # 收集所有铜箔层 ID（走线 + 铺铜层）
        all_layers = sorted(
            set(r.layer for r in data.resistances if r.layer is not None and r.layer > 0)
            | set(p.layer for p in data.copper_pours if p.layer is not None and p.layer > 0)
        )
        plot_images = generate_plot_images(mesh_points, data.mesh_resolution or 0.5, all_layers, net_results, data.max_drop_pct or 5.0, data.resistances, voltages, data.nodes)

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
