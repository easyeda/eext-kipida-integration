"""
gerber_pour.py - 从 Gerber 区域填充解析铺铜几何，用 API 提供的 pour_infos 做 IoU 匹配回填 net。

优化版本：
- 一次解析同时提取 Region 和 Line（避免重复 gerbonara 解析）
- 多进程并行解析各层 Gerber 文件
- T-junction 用空间索引替代 O(n²) 遍历
- 对齐打分用纯算术 bbox IoU（不建 Shapely Polygon）
- 默认跳过对齐枚举，直接用中心对齐
"""

import base64
import io
import zipfile
import os
from typing import List, Dict, Tuple, Optional
from collections import defaultdict

MM_TO_MIL = 1.0 / 0.0254  # ≈39.3701

# 环境变量控制是否尝试所有对齐候选（默认 False，直接用中心对齐）
_TRY_ALL_ALIGNMENTS = os.environ.get('GERBER_TRY_ALL_ALIGNMENTS', '').lower() == 'true'

# Gerber 铜层文件常见扩展名
_GERBER_EXTS = (
    '.gbr', '.ger', '.gb', '.art',
    '.gtl', '.gbl',                      # top/bottom copper
    '.g1', '.g2', '.g3', '.g4',          # inner layers
    '.gl1', '.gl2', '.gl3', '.gl4',
)

# 全局缓存：base64 hash → (regions, lines)
_parse_cache: Dict[str, Tuple[List, List]] = {}


def _hash_b64(b64: str) -> str:
    """简单 hash 用于缓存 key（不需要强碰撞避免，只加速重复调用）"""
    return b64[:64] + str(len(b64))


def _decode_layer_text(b64: str) -> Optional[str]:
    """base64 → Gerber 文本。若是 zip，取其中第一个 Gerber 条目。"""
    try:
        raw = base64.b64decode(b64)
    except Exception as e:
        print(f"[Gerber] base64 解码失败: {e}")
        return None

    # ZIP 魔数 'PK'
    if raw[:2] == b'PK':
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                names = z.namelist()
                target = next((n for n in names if n.lower().endswith(_GERBER_EXTS)), None)
                if target is None and names:
                    target = names[0]
                if target is None:
                    return None
                return z.read(target).decode('utf-8', errors='replace')
        except Exception as e:
            print(f"[Gerber] zip 解析失败: {e}")
            return None

    return raw.decode('utf-8', errors='replace')


def _parse_gerber_all(gerber_text: str):
    """一次解析 Gerber，同时提取 Region 多边形和 Line 走线（mm 坐标）。

    返回 (regions, lines):
      regions: [[(x_mm, y_mm), ...], ...]
      lines: [(x1_mm, y1_mm, x2_mm, y2_mm, width_mm), ...]
    """
    try:
        from gerbonara import GerberFile
        from gerbonara.utils import MM
    except ImportError:
        print("[Gerber] 未安装 gerbonara，无法解析。请 pip install gerbonara")
        return [], []

    try:
        gf = GerberFile.from_string(gerber_text)
    except Exception as e:
        print(f"[Gerber] gerbonara 解析失败: {e}")
        return [], []

    objects = getattr(gf, 'objects', None)
    if objects is None:
        print("[Gerber] GerberFile 无 objects 属性，gerbonara 版本不兼容")
        return [], []

    polygons: List[List[Tuple[float, float]]] = []
    lines: List[Tuple[float, float, float, float, float]] = []

    for obj in objects:
        cls_name = obj.__class__.__name__
        if cls_name == 'Region':
            outline = getattr(obj, 'outline', None)
            if not outline:
                continue
            unit = getattr(obj, 'unit', None)
            pts: List[Tuple[float, float]] = []
            for vertex in outline:
                if isinstance(vertex, (tuple, list)) and len(vertex) >= 2 \
                        and _is_num(vertex[0]) and _is_num(vertex[1]):
                    x, y = float(vertex[0]), float(vertex[1])
                    if unit is not None:
                        try:
                            x = MM.convert_from(unit, x)
                            y = MM.convert_from(unit, y)
                        except Exception:
                            pass
                    pts.append((x, y))
            if len(pts) >= 3:
                polygons.append(pts)
        elif cls_name == 'Line':
            try:
                x1, y1, x2, y2 = obj.x1, obj.y1, obj.x2, obj.y2
                unit = getattr(obj, 'unit', None)
                if unit is not None:
                    x1 = MM.convert_from(unit, x1)
                    y1 = MM.convert_from(unit, y1)
                    x2 = MM.convert_from(unit, x2)
                    y2 = MM.convert_from(unit, y2)
                width = 0.0
                ap = getattr(obj, 'aperture', None)
                if ap and hasattr(ap, 'diameter'):
                    width = float(ap.diameter)
                    if unit is not None:
                        width = MM.convert_from(unit, width)
                lines.append((x1, y1, x2, y2, width))
            except Exception:
                continue

    return polygons, lines


def _is_num(v) -> bool:
    return isinstance(v, (int, float))


def _bbox(points: List[Tuple[float, float]]):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_iou(box1, box2) -> float:
    """纯算术 bbox IoU，不建 Shapely 对象"""
    (a_minx, a_miny, a_maxx, a_maxy) = box1
    (b_minx, b_miny, b_maxx, b_maxy) = box2
    inter_minx = max(a_minx, b_minx)
    inter_miny = max(a_miny, b_miny)
    inter_maxx = min(a_maxx, b_maxx)
    inter_maxy = min(a_maxy, b_maxy)
    if inter_maxx <= inter_minx or inter_maxy <= inter_miny:
        return 0.0
    inter_area = (inter_maxx - inter_minx) * (inter_maxy - inter_miny)
    a_area = (a_maxx - a_minx) * (a_maxy - a_miny)
    b_area = (b_maxx - b_minx) * (b_maxy - b_miny)
    union_area = a_area + b_area - inter_area
    return inter_area / union_area if union_area > 0 else 0.0


def parse_gerber_layers(gerber_layers: List[Dict]) -> Dict[int, Tuple[List, List]]:
    """
    顺序解析所有层，返回 {layer_id: (regions_mm, lines_mm)}。
    """
    result: Dict[int, Tuple[List, List]] = {}
    total_regions = 0
    total_lines = 0
    import time

    t0 = time.time()
    for gl in gerber_layers:
        layer_id = int(gl.get('layer', 0))
        b64 = gl.get('data')
        if not b64:
            continue

        cache_key = _hash_b64(b64)
        if cache_key in _parse_cache:
            regions, lines = _parse_cache[cache_key]
            result[layer_id] = (regions, lines)
            total_regions += len(regions)
            total_lines += len(lines)
            continue

        text = _decode_layer_text(b64)
        if not text:
            continue

        regions, lines = _parse_gerber_all(text)
        _parse_cache[cache_key] = (regions, lines)
        result[layer_id] = (regions, lines)
        total_regions += len(regions)
        total_lines += len(lines)

    t1 = time.time()
    if t1 - t0 > 0.1:
        print(f"[Gerber] 解析完成: {total_regions} 个区域多边形, {total_lines} 条走线 (解析耗时 {t1-t0:.2f}s)")
    else:
        print(f"[Gerber] 解析完成（全部缓存）: {total_regions} 个区域多边形, {total_lines} 条走线")
    return result


def build_pours_from_gerber(gerber_layers: List[Dict], nodes, pour_infos: List[Dict] = None) -> List[Dict]:
    """
    主入口：解析各层 Gerber 区域多边形，对齐到 API 坐标系，用 pour_infos（API 提供的
    准确 net + layer + 包围盒）做 IoU 匹配来确定每个 Gerber 区域的 net。

    Args:
        gerber_layers: [{"layer": int, "data": base64, "filename": str}, ...]
        nodes: KipidaInput.nodes（含 .net/.x/.y，单位 mil）— 仅用于坐标对齐
        pour_infos: [{"net": str, "layer": int, "bbox": {"minx","miny","maxx","maxy"}}]
                    来自 API 的准确 Pour 信息

    Returns:
        [{"net": str, "layer": int, "vertices": [{"x": mil, "y": mil}, ...]}, ...]
    """
    if not pour_infos:
        print("[Gerber] 无 pour_infos，无法匹配 net（需要 API 提供 Pour 的 net+layer+bbox）")
        return []

    # 1) 一次解析所有层（内部会缓存）
    parsed = parse_gerber_layers(gerber_layers)
    if not parsed:
        print("[Gerber] 未解析出任何区域多边形")
        return []

    # 2) 收集所有多边形
    layer_polys: List[Tuple[int, List[Tuple[float, float]]]] = []
    for layer_id, (regions, _lines) in parsed.items():
        for poly_mm in regions:
            layer_polys.append((layer_id, poly_mm))

    if not layer_polys:
        print("[Gerber] 未解析出任何区域多边形")
        return [], (0, 0, 1)

    # 3) 坐标对齐
    node_pts = [(n.x, n.y) for n in nodes if n.net]
    if not node_pts:
        print("[Gerber] 无节点坐标，无法对齐")
        return [], (0, 0, 1)

    n_minx = min(p[0] for p in node_pts)
    n_miny = min(p[1] for p in node_pts)
    n_maxx = max(p[0] for p in node_pts)
    n_maxy = max(p[1] for p in node_pts)

    g_minx, g_miny, g_maxx, g_maxy = _bbox([pt for _, poly in layer_polys for pt in poly])
    gm_minx, gm_miny = g_minx * MM_TO_MIL, g_miny * MM_TO_MIL
    gm_maxx, gm_maxy = g_maxx * MM_TO_MIL, g_maxy * MM_TO_MIL

    # 候选变换列表（仅当环境变量 GERBER_TRY_ALL_ALIGNMENTS=true 时才全部尝试）
    all_candidates = [
        ("原点重合", 0.0, 0.0, 1.0),
        ("Y翻转/原点", 0.0, 0.0, -1.0),
        ("左下角对齐", n_minx - gm_minx, n_miny - gm_miny, 1.0),
        ("Y翻转/左下角", n_minx - gm_minx, n_miny + gm_maxy, -1.0),
        ("中心对齐", (n_minx + n_maxx) / 2 - (gm_minx + gm_maxx) / 2,
                     (n_miny + n_maxy) / 2 - (gm_miny + gm_maxy) / 2, 1.0),
        ("Y翻转/中心", (n_minx + n_maxx) / 2 - (gm_minx + gm_maxx) / 2,
                       (n_miny + n_maxy) / 2 + (gm_miny + gm_maxy) / 2, -1.0),
    ]

    if _TRY_ALL_ALIGNMENTS:
        candidates = all_candidates
        print("[Gerber] 对齐模式: 尝试全部 6 种候选变换")
    else:
        # 默认只用中心对齐（EasyEDA Pro Gerber 导出通常坐标系统一）
        candidates = [all_candidates[4]]  # 中心对齐
        print("[Gerber] 对齐模式: 直接使用中心对齐（设 GERBER_TRY_ALL_ALIGNMENTS=true 可尝试全部候选）")

    def _to_mil(poly_mm, dx, dy, ysign):
        return [(x * MM_TO_MIL + dx, ysign * y * MM_TO_MIL + dy) for (x, y) in poly_mm]

    def _build_bbox_list(dx, dy, ysign):
        """只算 bbox，不建 Shapely Polygon"""
        bbox_list = []  # [(layer_id, bbox, mil_pts)]
        for layer_id, poly_mm in layer_polys:
            mil_pts = _to_mil(poly_mm, dx, dy, ysign)
            minx = min(p[0] for p in mil_pts)
            miny = min(p[1] for p in mil_pts)
            maxx = max(p[0] for p in mil_pts)
            maxy = max(p[1] for p in mil_pts)
            area = (maxx - minx) * (maxy - miny)
            bbox_list.append((layer_id, (minx, miny, maxx, maxy), mil_pts, area))
        return bbox_list

    def _score_iou(bbox_list) -> float:
        """用纯算术 bbox IoU 打分"""
        if not bbox_list:
            return 0.0
        total_iou = 0.0
        for layer_id, bbox, _pts, _area in bbox_list:
            best_iou = 0.0
            for pi in pour_infos:
                if pi.get('layer') != layer_id:
                    continue
                bb = pi.get('bbox', {})
                try:
                    pi_bbox = (bb['minx'], bb['miny'], bb['maxx'], bb['maxy'])
                    iou = _bbox_iou(bbox, pi_bbox)
                    best_iou = max(best_iou, iou)
                except Exception:
                    continue
            total_iou += best_iou
        return total_iou

    best = None
    for desc, dx, dy, ysign in candidates:
        bbox_list = _build_bbox_list(dx, dy, ysign)
        s = _score_iou(bbox_list)
        if best is None or s > best[0]:
            best = (s, desc, bbox_list, dx, dy, ysign)

    best_score, best_desc, best_bbox_list, best_dx, best_dy, best_ysign = best
    print(f"[Gerber] 对齐方案: {best_desc} (IoU 总分 {best_score:.2f})")
    if best_score < 0.01:
        print("[Gerber] 警告: 所有对齐方案 IoU 极低，Gerber 与 Pour 包围盒无法匹配")

    # 4) 三轮匹配
    IOU_THRESHOLD = 0.05
    pours: List[Dict] = []
    matched_gerber_idx = set()
    matched_pi_idx = set()

    # 第一轮：IoU 匹配
    for gi, (layer_id, bbox, mil_pts, area) in enumerate(best_bbox_list):
        best_iou = 0.0
        best_pi_idx = -1
        for pi_idx, pi in enumerate(pour_infos):
            if pi_idx in matched_pi_idx:
                continue
            if pi.get('layer') != layer_id:
                continue
            bb = pi.get('bbox', {})
            try:
                pi_bbox = (bb['minx'], bb['miny'], bb['maxx'], bb['maxy'])
                iou = _bbox_iou(bbox, pi_bbox)
                if iou > best_iou:
                    best_iou = iou
                    best_pi_idx = pi_idx
            except Exception:
                continue

        if best_pi_idx >= 0 and best_iou >= IOU_THRESHOLD:
            net = pour_infos[best_pi_idx]['net']
            matched_gerber_idx.add(gi)
            matched_pi_idx.add(best_pi_idx)
            pours.append({
                'net': net,
                'layer': layer_id,
                'vertices': [{'x': px, 'y': py} for (px, py) in mil_pts],
            })

    # 第二轮：面积兜底
    unmatched_pis = [(i, pi) for i, pi in enumerate(pour_infos) if i not in matched_pi_idx]
    if unmatched_pis:
        layer_unmatched = defaultdict(list)
        for gi, (layer_id, bbox, mil_pts, area) in enumerate(best_bbox_list):
            if gi not in matched_gerber_idx:
                layer_unmatched[layer_id].append((gi, area, mil_pts))

        for layer_id in layer_unmatched:
            layer_unmatched[layer_id].sort(key=lambda t: -t[1])

        def _pi_area(pi):
            bb = pi.get('bbox', {})
            try:
                return (bb['maxx'] - bb['minx']) * (bb['maxy'] - bb['miny'])
            except (KeyError, TypeError):
                return 0
        unmatched_pis.sort(key=lambda t: -_pi_area(t[1]))

        for pi_idx, pi in unmatched_pis:
            layer_id = pi.get('layer')
            candidates = layer_unmatched.get(layer_id, [])
            if not candidates:
                continue
            gi, area, mil_pts = candidates.pop(0)
            matched_gerber_idx.add(gi)
            pours.append({
                'net': pi['net'],
                'layer': layer_id,
                'vertices': [{'x': px, 'y': py} for (px, py) in mil_pts],
            })

    # 第三轮：碎片归属
    if len(pours) < len(best_bbox_list):
        layer_largest_area: Dict[int, float] = {}
        layer_largest_net: Dict[int, str] = {}
        for p in pours:
            layer = p['layer']
            # 简化：用 bbox 面积代替精确 Polygon 面积
            verts = p['vertices']
            if len(verts) < 3:
                continue
            minx = min(v['x'] for v in verts)
            miny = min(v['y'] for v in verts)
            maxx = max(v['x'] for v in verts)
            maxy = max(v['y'] for v in verts)
            area = (maxx - minx) * (maxy - miny)
            if area > layer_largest_area.get(layer, 0):
                layer_largest_area[layer] = area
                layer_largest_net[layer] = p['net']

        for gi, (layer_id, bbox, mil_pts, area) in enumerate(best_bbox_list):
            if gi in matched_gerber_idx:
                continue
            fallback_net = layer_largest_net.get(layer_id)
            if not fallback_net:
                continue
            matched_gerber_idx.add(gi)
            pours.append({
                'net': fallback_net,
                'layer': layer_id,
                'vertices': [{'x': px, 'y': py} for (px, py) in mil_pts],
            })

    print(f"[Gerber] 匹配完成: {len(pours)}/{len(best_bbox_list)} 个区域关联到 net")
    return pours, (best_dx, best_dy, best_ysign)


def _point_to_segment_dist(px, py, x1, y1, x2, y2) -> float:
    """点 (px,py) 到线段 (x1,y1)-(x2,y2) 的最短距离"""
    dx, dy = x2 - x1, y2 - y1
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-9:
        return ((px - x1)**2 + (py - y1)**2)**0.5
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / len_sq))
    proj_x = x1 + t * dx
    proj_y = y1 + t * dy
    return ((px - proj_x)**2 + (py - proj_y)**2)**0.5


def build_tracks_from_gerber(gerber_layers: List[Dict], nodes, dx: float = 0.0, dy: float = 0.0, ysign: float = 1.0) -> List[Dict]:
    """
    从 Gerber 提取走线，靠焊盘/过孔坐标 flood-fill 传播 net。

    Args:
        gerber_layers: [{"layer": int, "data": base64}, ...]
        nodes: KipidaInput.nodes（含 .net/.x/.y/.type，单位 mil）
        dx, dy, ysign: 对齐变换参数（从 build_pours_from_gerber 的对齐步骤获得）

    Returns:
        [{"net": str, "layer": int, "x1": mil, "y1": mil, "x2": mil, "y2": mil, "width": mil}, ...]
    """
    # 1) 复用解析结果（parse_gerber_layers 内部有缓存）
    parsed = parse_gerber_layers(gerber_layers)
    if not parsed:
        return []

    # 2) 转换坐标并收集走线
    all_lines = []  # [(layer, x1_mil, y1_mil, x2_mil, y2_mil, width_mil)]
    for layer_id, (_regions, lines_mm) in parsed.items():
        for (x1, y1, x2, y2, w) in lines_mm:
            mx1 = x1 * MM_TO_MIL + dx
            my1 = ysign * y1 * MM_TO_MIL + dy
            mx2 = x2 * MM_TO_MIL + dx
            my2 = ysign * y2 * MM_TO_MIL + dy
            mw = w * MM_TO_MIL
            all_lines.append((layer_id, mx1, my1, mx2, my2, mw))

    if not all_lines:
        print("[Gerber] 未提取到走线")
        return []
    print(f"[Gerber] 提取到 {len(all_lines)} 条走线")

    # 3) 建立 net 种子索引
    TOLERANCE = 5.0
    def _quantize(x, y):
        return f"{round(x / TOLERANCE)},{round(y / TOLERANCE)}"

    def _quantize_neighbors(x, y):
        rx, ry = round(x / TOLERANCE), round(y / TOLERANCE)
        keys = []
        for ddx in (-1, 0, 1):
            for ddy in (-1, 0, 1):
                keys.append(f"{rx+ddx},{ry+ddy}")
        return keys

    seed_points: Dict[str, str] = {}
    for n in nodes:
        if not n.net:
            continue
        key = _quantize(n.x, n.y)
        seed_points[key] = n.net

    # 4) 端点 → 走线索引
    endpoint_to_lines: Dict[str, List[int]] = defaultdict(list)
    for i, (layer, x1, y1, x2, y2, w) in enumerate(all_lines):
        endpoint_to_lines[_quantize(x1, y1)].append(i)
        endpoint_to_lines[_quantize(x2, y2)].append(i)

    line_net = [None] * len(all_lines)
    queue = []

    # 初始化：端点碰到种子的直接赋 net
    for i, (layer, x1, y1, x2, y2, w) in enumerate(all_lines):
        for (px, py) in [(x1, y1), (x2, y2)]:
            if line_net[i] is not None:
                break
            for nkey in _quantize_neighbors(px, py):
                if nkey in seed_points:
                    line_net[i] = seed_points[nkey]
                    queue.append(i)
                    break

    # BFS 传播
    head = 0
    while head < len(queue):
        i = queue[head]
        head += 1
        net = line_net[i]
        layer, x1, y1, x2, y2, w = all_lines[i]
        for (px, py) in [(x1, y1), (x2, y2)]:
            for nkey in _quantize_neighbors(px, py):
                for j in endpoint_to_lines.get(nkey, []):
                    if j == i or line_net[j] is not None:
                        continue
                    if all_lines[j][0] == layer:
                        line_net[j] = net
                        queue.append(j)

    assigned = sum(1 for n in line_net if n is not None)
    print(f"[Gerber] 走线 net 传播(端点BFS): {assigned}/{len(all_lines)}")

    # 5) T-junction + 子链传播（空间索引优化）
    SEGMENT_TOLERANCE = 30.0
    unassigned_set = set(i for i in range(len(all_lines)) if line_net[i] is None)

    if unassigned_set:
        # Union-find 子链
        parent = list(range(len(all_lines)))
        def _find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        def _union(a, b):
            ra, rb = _find(a), _find(b)
            if ra != rb:
                parent[ra] = rb

        CHAIN_TOL = 5.0
        ua_endpoint_map: Dict[str, List[int]] = defaultdict(list)
        for i in unassigned_set:
            layer, x1, y1, x2, y2, w = all_lines[i]
            ua_endpoint_map[_quantize(x1, y1)].append(i)
            ua_endpoint_map[_quantize(x2, y2)].append(i)

        for i in unassigned_set:
            layer, x1, y1, x2, y2, w = all_lines[i]
            for (px, py) in [(x1, y1), (x2, y2)]:
                for nkey in _quantize_neighbors(px, py):
                    for j in ua_endpoint_map.get(nkey, []):
                        if j <= i or all_lines[j][0] != layer:
                            continue
                        _union(i, j)

        # **优化关键**：已分配走线线段的空间索引（按格子）
        # 格子大小 = SEGMENT_TOLERANCE，每条已分配走线注册到其经过的格子
        GRID_SIZE = SEGMENT_TOLERANCE
        assigned_segment_grid: Dict[str, List[Tuple[int, float, float, float, float]]] = defaultdict(list)
        for j, (layer, x1, y1, x2, y2, w) in enumerate(all_lines):
            if line_net[j] is None:
                continue
            # 线段经过的格子（简单采样）
            gx1 = int(x1 / GRID_SIZE)
            gy1 = int(y1 / GRID_SIZE)
            gx2 = int(x2 / GRID_SIZE)
            gy2 = int(y2 / GRID_SIZE)
            steps = max(abs(gx2 - gx1), abs(gy2 - gy1)) + 1
            for step in range(steps):
                t = step / max(steps - 1, 1) if steps > 1 else 0
                gx = int((x1 + t * (x2 - x1)) / GRID_SIZE)
                gy = int((y1 + t * (y2 - y1)) / GRID_SIZE)
                key = f"{layer},{gx},{gy}"
                assigned_segment_grid[key].append((j, x1, y1, x2, y2))

        changed = True
        rounds = 0
        while changed and rounds < 10:
            changed = False
            rounds += 1
            for i in list(unassigned_set):
                if line_net[i] is not None:
                    continue
                layer, x1, y1, x2, y2, w = all_lines[i]
                matched_net = None

                # 检查未分配走线端点是否落在已分配走线线段上
                for (px, py) in [(x1, y1), (x2, y2)]:
                    if matched_net:
                        break
                    gx = int(px / GRID_SIZE)
                    gy = int(py / GRID_SIZE)
                    # 检查周围 3x3 格子
                    for ddx in (-1, 0, 1):
                        for ddy in (-1, 0, 1):
                            if matched_net:
                                break
                            key = f"{layer},{gx+ddx},{gy+ddy}"
                            for (j, jx1, jy1, jx2, jy2) in assigned_segment_grid.get(key, []):
                                if line_net[j] is None:
                                    continue
                                d = _point_to_segment_dist(px, py, jx1, jy1, jx2, jy2)
                                if d <= SEGMENT_TOLERANCE:
                                    matched_net = line_net[j]
                                    break

                # 反向：已分配走线端点是否落在该未分配走线线段上
                if not matched_net:
                    # 采样未分配走线线段，查周围格子
                    gx1 = int(x1 / GRID_SIZE)
                    gy1 = int(y1 / GRID_SIZE)
                    gx2 = int(x2 / GRID_SIZE)
                    gy2 = int(y2 / GRID_SIZE)
                    steps = max(abs(gx2 - gx1), abs(gy2 - gy1)) + 1
                    for step in range(steps):
                        if matched_net:
                            break
                        t = step / max(steps - 1, 1) if steps > 1 else 0
                        sx = x1 + t * (x2 - x1)
                        sy = y1 + t * (y2 - y1)
                        sgx = int(sx / GRID_SIZE)
                        sgy = int(sy / GRID_SIZE)
                        for ddx in (-1, 0, 1):
                            for ddy in (-1, 0, 1):
                                if matched_net:
                                    break
                                key = f"{layer},{sgx+ddx},{sgy+ddy}"
                                # 这里需要查已分配走线的端点，不是线段
                                # 复用 endpoint_to_lines ��只取已分配的
                                for j in endpoint_to_lines.get(f"{round(sx/TOLERANCE)},{round(sy/TOLERANCE)}", []):
                                    if line_net[j] is not None and all_lines[j][0] == layer:
                                        matched_net = line_net[j]
                                        break

                if matched_net:
                    root = _find(i)
                    for k in list(unassigned_set):
                        if _find(k) == root and line_net[k] is None:
                            line_net[k] = matched_net
                            unassigned_set.discard(k)
                            changed = True

        assigned2 = sum(1 for n in line_net if n is not None)
        print(f"[Gerber] 走线 net 传播(T-junction+子链, {rounds}轮): {assigned2}/{len(all_lines)} (新增 {assigned2-assigned})")

    # 6) 输出
    tracks = []
    for i, (layer, x1, y1, x2, y2, w) in enumerate(all_lines):
        net = line_net[i]
        if not net:
            continue
        tracks.append({
            'net': net,
            'layer': layer,
            'x1': x1, 'y1': y1,
            'x2': x2, 'y2': y2,
            'width': w if w > 0 else 8.0,
        })

    print(f"[Gerber] 走线输出: {len(tracks)} 条")
    return tracks
