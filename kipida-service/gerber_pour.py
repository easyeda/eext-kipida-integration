"""
gerber_pour.py - 从 Gerber 区域填充解析铺铜几何，用 API 提供的 pour_infos 做 IoU 匹配回填 net。

背景：
  铺铜（覆铜）多边形由 EDA API 提取不可靠，改为从扩展逐层导出的 Gerber 文件中解析。
  Gerber 不含网络信息，但 API 能可靠获取每个 Pour 的 net + layer + 外轮廓包围盒。
  用 Gerber 区域的包围盒和 API pour_infos 的包围盒做 IoU 匹配来确定 net——
  这比靠节点投票猜 net 可靠得多（彻底避免 GND 大平面吞小铺铜、通孔过孔污染等问题）。

坐标系：
  - Gerber 导出单位为 mm。
  - API 节点/pour_infos 坐标单位为 mil（1 mil = 0.0254 mm）。
  - 最终输出的 copper_pours 顶点单位为 mil，与原 API 路径数据契约一致，
    下游 main.py 的 FEM 代码完全不用改。
"""

import base64
import io
import zipfile
from typing import List, Dict, Tuple, Optional

MM_TO_MIL = 1.0 / 0.0254  # ≈39.3701

# Gerber 铜层文件常见扩展名
_GERBER_EXTS = (
    '.gbr', '.ger', '.gb', '.art',
    '.gtl', '.gbl',                      # top/bottom copper
    '.g1', '.g2', '.g3', '.g4',          # inner layers
    '.gl1', '.gl2', '.gl3', '.gl4',
)


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
                # 优先匹配 Gerber 扩展名
                target = next((n for n in names if n.lower().endswith(_GERBER_EXTS)), None)
                if target is None and names:
                    target = names[0]
                if target is None:
                    print("[Gerber] zip 内无文件")
                    return None
                return z.read(target).decode('utf-8', errors='replace')
        except Exception as e:
            print(f"[Gerber] zip 解析失败: {e}")
            return None

    return raw.decode('utf-8', errors='replace')


def _extract_region_polygons(gerber_text: str) -> List[List[Tuple[float, float]]]:
    """用 gerbonara 解析 Gerber 文本，返回所有区域填充(G36/G37)多边形外环（mm 坐标）。

    实测 gerbonara 1.6.x：GerberFile.from_string(text).objects 是图元列表，
    Region 图元的 .outline 直接给出 [(x, y), ...] 顶点（文件原生单位），
    .unit 为 LengthUnit，用 MM.convert_from(unit, v) 转 mm。
    """
    try:
        from gerbonara import GerberFile
        from gerbonara.utils import MM
    except ImportError:
        print("[Gerber] 未安装 gerbonara，无法解析。请 pip install gerbonara")
        return []

    try:
        gf = GerberFile.from_string(gerber_text)
    except Exception as e:
        print(f"[Gerber] gerbonara 解析失败: {e}")
        return []

    objects = getattr(gf, 'objects', None)
    if objects is None:
        print("[Gerber] GerberFile 无 objects 属性，gerbonara 版本不兼容")
        return []

    polygons: List[List[Tuple[float, float]]] = []
    for obj in objects:
        if obj.__class__.__name__ != 'Region':
            continue
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

    print(f"[Gerber] 解析出 {len(polygons)} 个区域多边形")
    return polygons


def _is_num(v) -> bool:
    return isinstance(v, (int, float))


def _extract_lines(gerber_text: str) -> List[Tuple[float, float, float, float, float]]:
    """从 Gerber 文本提取走线 Line 图元，返回 [(x1_mm, y1_mm, x2_mm, y2_mm, width_mm), ...]"""
    try:
        from gerbonara import GerberFile
        from gerbonara.utils import MM
    except ImportError:
        return []

    try:
        gf = GerberFile.from_string(gerber_text)
    except Exception:
        return []

    objects = getattr(gf, 'objects', None)
    if not objects:
        return []

    lines = []
    for obj in objects:
        if obj.__class__.__name__ != 'Line':
            continue
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
    return lines


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
    # 1) 逐层提取走线（mm → mil，应用对齐变换）
    all_lines = []  # [(layer, x1_mil, y1_mil, x2_mil, y2_mil, width_mil)]
    for gl in gerber_layers:
        layer_id = int(gl.get('layer', 0))
        b64 = gl.get('data')
        if not b64:
            continue
        text = _decode_layer_text(b64)
        if not text:
            continue
        for (x1, y1, x2, y2, w) in _extract_lines(text):
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

    # 2) 建立坐标索引：把焊盘/过孔/走线端点(junction)的坐标 → net（作为 flood-fill 种子）
    # junction 节点来自 API 已正确提取的走线端点，坐标精确，大幅增加种子覆盖。
    TOLERANCE = 5.0  # mil，端点匹配容差（Gerber mm→mil 有精度累积，需较宽容差）
    # 用网格索引加速坐标查找，查找时检查周围 9 格避免边界遗漏
    seed_points: Dict[str, str] = {}  # "rx,ry" -> net（量化到容差格子）

    def _quantize(x, y):
        return f"{round(x / TOLERANCE)},{round(y / TOLERANCE)}"

    def _quantize_neighbors(x, y):
        """返回该坐标所在格子及周围 8 格的 key"""
        rx, ry = round(x / TOLERANCE), round(y / TOLERANCE)
        keys = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                keys.append(f"{rx+dx},{ry+dy}")
        return keys

    for n in nodes:
        if not n.net:
            continue
        # 焊盘、过孔、走线端点(junction)都是可靠的 net 种子
        key = _quantize(n.x, n.y)
        seed_points[key] = n.net

    # 3) Flood-fill：从种子出发，沿走线链传播 net
    # 建立端点 → 走线索引（每条走线注册到所在格子）
    from collections import defaultdict
    endpoint_to_lines: Dict[str, List[int]] = defaultdict(list)
    for i, (layer, x1, y1, x2, y2, w) in enumerate(all_lines):
        endpoint_to_lines[_quantize(x1, y1)].append(i)
        endpoint_to_lines[_quantize(x2, y2)].append(i)

    line_net = [None] * len(all_lines)  # 每条走线的 net

    # 初始化：走线端点碰到种子的直接赋 net（查周围 9 格避免边界遗漏）
    queue = []
    for i, (layer, x1, y1, x2, y2, w) in enumerate(all_lines):
        for (px, py) in [(x1, y1), (x2, y2)]:
            if line_net[i] is not None:
                break
            for nkey in _quantize_neighbors(px, py):
                if nkey in seed_points:
                    line_net[i] = seed_points[nkey]
                    queue.append(i)
                    break

    # BFS 传播（查周围 9 格找相邻走线）
    head = 0
    while head < len(queue):
        i = queue[head]
        head += 1
        net = line_net[i]
        layer, x1, y1, x2, y2, w = all_lines[i]
        # 从该走线的两个端点出发找相邻走线
        for (px, py) in [(x1, y1), (x2, y2)]:
            for nkey in _quantize_neighbors(px, py):
                for j in endpoint_to_lines.get(nkey, []):
                    if j == i or line_net[j] is not None:
                        continue
                    # 同层才传播
                    if all_lines[j][0] == layer:
                        line_net[j] = net
                        queue.append(j)

    # 统计
    assigned = sum(1 for n in line_net if n is not None)
    print(f"[Gerber] 走线 net 传播(端点BFS): {assigned}/{len(all_lines)} 条已分配 net")

    # 第二轮：T-junction + 子链传播。
    # 问题：自由角度走线在 API 里完全断链——不只丢它自己，还丢它之后整条链。
    # 因此大量未分配走线互相端点相连（Gerber 坐标自洽），但和 API 节点之间有 10-50+ mil 间隙。
    #
    # 策略：
    # (1) 先把未分配走线之间按端点连通性(5 mil)建子链，这样只需一条走线被匹配到，整链获得 net
    # (2) T-junction 容差放宽到 30 mil（诊断显示最近的间隙在 10-27 mil）
    # (3) 同时检查已分配走线端点是否落在未分配走线线段上（反向 T-junction）
    SEGMENT_TOLERANCE = 30.0  # mil

    # (1) 未分配走线之间建连通子链（端点 5 mil 容差，Gerber 内部坐标自洽）
    # union-find
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
    unassigned_set = set(i for i in range(len(all_lines)) if line_net[i] is None)
    # 用格子索引加速未分配走线之间的端点匹配
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

    # (2)(3) T-junction 传播 + 子链批量传播
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

            # 检查该走线端点是否落在已分配走线线段上
            for (px, py) in [(x1, y1), (x2, y2)]:
                if matched_net:
                    break
                for j, (jlayer, jx1, jy1, jx2, jy2, jw) in enumerate(all_lines):
                    if line_net[j] is None or jlayer != layer:
                        continue
                    d = _point_to_segment_dist(px, py, jx1, jy1, jx2, jy2)
                    if d <= SEGMENT_TOLERANCE:
                        matched_net = line_net[j]
                        break

            # 反向：已分配走线的端点是否落在该未分配走线线段上
            if not matched_net:
                for j, (jlayer, jx1, jy1, jx2, jy2, jw) in enumerate(all_lines):
                    if line_net[j] is None or jlayer != layer:
                        continue
                    for (jpx, jpy) in [(jx1, jy1), (jx2, jy2)]:
                        d = _point_to_segment_dist(jpx, jpy, x1, y1, x2, y2)
                        if d <= SEGMENT_TOLERANCE:
                            matched_net = line_net[j]
                            break
                    if matched_net:
                        break

            if matched_net:
                # 传播到整条子链
                root = _find(i)
                for k in list(unassigned_set):
                    if _find(k) == root and line_net[k] is None:
                        line_net[k] = matched_net
                        unassigned_set.discard(k)
                        changed = True

    assigned2 = sum(1 for n in line_net if n is not None)
    print(f"[Gerber] 走线 net 传播(T-junction+子链, {rounds}轮): {assigned2}/{len(all_lines)} 条已分配 net (新增 {assigned2-assigned})")

    # 诊断：未分配走线端点到最近已分配走线端点的距离分布
    if assigned > 0 and assigned < len(all_lines):
        assigned_endpoints = set()
        for i, (layer, x1, y1, x2, y2, w) in enumerate(all_lines):
            if line_net[i] is not None:
                assigned_endpoints.add((layer, round(x1, 1), round(y1, 1)))
                assigned_endpoints.add((layer, round(x2, 1), round(y2, 1)))
        # 采样前 20 条未分配走线
        min_dists = []
        sample_count = 0
        for i, (layer, x1, y1, x2, y2, w) in enumerate(all_lines):
            if line_net[i] is not None or sample_count >= 20:
                continue
            sample_count += 1
            best_d = float('inf')
            for (px, py) in [(x1, y1), (x2, y2)]:
                for (al, ax, ay) in assigned_endpoints:
                    if al != layer:
                        continue
                    d = ((px - ax)**2 + (py - ay)**2)**0.5
                    best_d = min(best_d, d)
            min_dists.append((best_d, layer, x1, y1, x2, y2))
        min_dists.sort()
        print(f"[Gerber] 未分配走线到最近已分配端点的距离 (前10):")
        for d, layer, x1, y1, x2, y2 in min_dists[:10]:
            print(f"  dist={d:.2f}mil layer={layer} ({x1:.1f},{y1:.1f})->({x2:.1f},{y2:.1f})")

    # 4) 输出
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
            'width': w if w > 0 else 8.0,  # 默认 8 mil
        })

    print(f"[Gerber] 走线输出: {len(tracks)} 条")
    return tracks


def _bbox(points: List[Tuple[float, float]]):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


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
    try:
        from shapely.geometry import Polygon, box
    except ImportError:
        print("[Gerber] 未安装 shapely，无法处理")
        return []

    if not pour_infos:
        print("[Gerber] 无 pour_infos，无法匹配 net（需要 API 提供 Pour 的 net+layer+bbox）")
        return []

    # 诊断：打印 pour_infos 概览
    print(f"[Gerber] 收到 {len(pour_infos)} 个 pour_infos:")
    for pi in pour_infos:
        bb = pi.get('bbox', {})
        print(f"  net={pi.get('net'):20s} layer={pi.get('layer')} bbox=[{bb.get('minx',0):.1f},{bb.get('miny',0):.1f},{bb.get('maxx',0):.1f},{bb.get('maxy',0):.1f}]")

    # 1) 逐层解析区域多边形（mm）
    layer_polys: List[Tuple[int, List[Tuple[float, float]]]] = []
    for gl in gerber_layers:
        layer_id = int(gl.get('layer', 0))
        b64 = gl.get('data')
        if not b64:
            continue
        text = _decode_layer_text(b64)
        if not text:
            continue
        for poly_mm in _extract_region_polygons(text):
            layer_polys.append((layer_id, poly_mm))

    if not layer_polys:
        print("[Gerber] 未解析出任何区域多边形")
        return []

    # 2) 坐标对齐：用节点包围盒 vs Gerber 包围盒，枚举候选变换取最优
    node_pts = [(n.x, n.y) for n in nodes if n.net]
    if not node_pts:
        print("[Gerber] 无节点坐标，无法对齐")
        return []
    n_minx = min(p[0] for p in node_pts)
    n_miny = min(p[1] for p in node_pts)
    n_maxx = max(p[0] for p in node_pts)
    n_maxy = max(p[1] for p in node_pts)

    g_minx, g_miny, g_maxx, g_maxy = _bbox([pt for _, poly in layer_polys for pt in poly])
    gm_minx, gm_miny = g_minx * MM_TO_MIL, g_miny * MM_TO_MIL
    gm_maxx, gm_maxy = g_maxx * MM_TO_MIL, g_maxy * MM_TO_MIL

    candidates = [
        ("原点重合", 0.0, 0.0, 1.0),
        ("Y翻转/原点", 0.0, 0.0, -1.0),
        ("左下角对齐", n_minx - gm_minx, n_miny - gm_miny, 1.0),
        ("Y翻转/左下角", n_minx - gm_minx, n_miny + gm_maxy, -1.0),
        ("中心对齐", (n_minx + n_maxx) / 2 - (gm_minx + gm_maxx) / 2,
                     (n_miny + n_maxy) / 2 - (gm_miny + gm_maxy) / 2, 1.0),
        ("Y翻转/中心", (n_minx + n_maxx) / 2 - (gm_minx + gm_maxx) / 2,
                       (n_miny + n_maxy) / 2 + (gm_miny + gm_maxy) / 2, -1.0),
    ]

    def _to_mil(poly_mm, dx, dy, ysign):
        return [(x * MM_TO_MIL + dx, ysign * y * MM_TO_MIL + dy) for (x, y) in poly_mm]

    def _build_polys(dx, dy, ysign):
        polys = []
        for layer_id, poly_mm in layer_polys:
            mil_pts = _to_mil(poly_mm, dx, dy, ysign)
            try:
                sp = Polygon(mil_pts)
                if not sp.is_valid:
                    sp = sp.buffer(0)
                if not sp.is_empty and sp.area > 0:
                    polys.append((layer_id, mil_pts, sp))
            except Exception:
                continue
        return polys

    # 对齐打分：用 pour_infos 的 bbox 和 Gerber 区域的 bbox IoU 总分
    def _score_iou(polys) -> float:
        if not polys:
            return 0.0
        total_iou = 0.0
        for layer_id, _, sp in polys:
            sp_bounds = sp.bounds  # (minx, miny, maxx, maxy)
            sp_box = box(*sp_bounds)
            best_iou = 0.0
            for pi in pour_infos:
                if pi.get('layer') != layer_id:
                    continue
                bb = pi.get('bbox', {})
                try:
                    pi_box = box(bb['minx'], bb['miny'], bb['maxx'], bb['maxy'])
                    inter = sp_box.intersection(pi_box).area
                    union = sp_box.area + pi_box.area - inter
                    iou = inter / union if union > 0 else 0.0
                    best_iou = max(best_iou, iou)
                except Exception:
                    continue
            total_iou += best_iou
        return total_iou

    best = None  # (score, desc, polys, dx, dy, ysign)
    for desc, dx, dy, ysign in candidates:
        polys = _build_polys(dx, dy, ysign)
        s = _score_iou(polys)
        if best is None or s > best[0]:
            best = (s, desc, polys, dx, dy, ysign)

    best_score, best_desc, best_polys, best_dx, best_dy, best_ysign = best
    print(f"[Gerber] 对齐方案: {best_desc} (IoU 总分 {best_score:.2f})")
    if best_score < 0.01:
        print("[Gerber] 警告: 所有对齐方案 IoU 极低，Gerber 与 Pour 包围盒无法匹配")

    # 3) 两轮匹配策略：
    #    第一轮：IoU 匹配（搞定坐标正确的小铺铜）
    #    第二轮：未匹配的 pour_infos 按面积从大到小，分配给同层未匹配的最大 Gerber 区域
    #    （搞定坐标系错误的大平面：API 对大平面返回的 bbox 坐标系不对，IoU 会失败，
    #     但大平面总是同层最大区域，靠面积排序即可正确配对）
    IOU_THRESHOLD = 0.05
    pours: List[Dict] = []
    matched_gerber_idx = set()   # 已匹配的 Gerber 区域索引
    matched_pi_idx = set()       # 已匹配的 pour_infos 索引

    print(f"[Gerber] 各 Gerber 区域 bbox (mil):")
    for layer_id, mil_pts, sp in best_polys:
        b = sp.bounds
        print(f"  layer={layer_id} bbox=[{b[0]:.1f},{b[1]:.1f},{b[2]:.1f},{b[3]:.1f}] area={sp.area:.0f}")

    # --- 第一轮：IoU 匹配 ---
    for gi, (layer_id, mil_pts, sp) in enumerate(best_polys):
        sp_box = box(*sp.bounds)
        best_iou = 0.0
        best_pi_idx = -1
        for pi_idx, pi in enumerate(pour_infos):
            if pi_idx in matched_pi_idx:
                continue
            if pi.get('layer') != layer_id:
                continue
            bb = pi.get('bbox', {})
            try:
                pi_box = box(bb['minx'], bb['miny'], bb['maxx'], bb['maxy'])
                inter = sp_box.intersection(pi_box).area
                union = sp_box.area + pi_box.area - inter
                iou = inter / union if union > 0 else 0.0
                if iou > best_iou:
                    best_iou = iou
                    best_pi_idx = pi_idx
            except Exception:
                continue

        if best_pi_idx >= 0 and best_iou >= IOU_THRESHOLD:
            net = pour_infos[best_pi_idx]['net']
            matched_gerber_idx.add(gi)
            matched_pi_idx.add(best_pi_idx)
            print(f"[Gerber] 区域 layer={layer_id} -> {net} (IoU={best_iou:.3f})")
            pours.append({
                'net': net,
                'layer': layer_id,
                'vertices': [{'x': px, 'y': py} for (px, py) in mil_pts],
            })

    # --- 第二轮：面积排序兜底匹配 ---
    # 对每层：未匹配的 pour_infos 按 bbox 面积从大到小，分配给同层未匹配的最大 Gerber 区域
    unmatched_pis = [(i, pi) for i, pi in enumerate(pour_infos) if i not in matched_pi_idx]
    if unmatched_pis:
        # 按层分组
        from collections import defaultdict
        layer_unmatched_gerber = defaultdict(list)  # layer -> [(gi, area, mil_pts)]
        for gi, (layer_id, mil_pts, sp) in enumerate(best_polys):
            if gi not in matched_gerber_idx:
                layer_unmatched_gerber[layer_id].append((gi, sp.area, mil_pts))

        # 每层内按面积降序
        for layer_id in layer_unmatched_gerber:
            layer_unmatched_gerber[layer_id].sort(key=lambda t: -t[1])

        # pour_infos 按 bbox 面积降序
        def _pi_area(pi):
            bb = pi.get('bbox', {})
            try:
                return (bb['maxx'] - bb['minx']) * (bb['maxy'] - bb['miny'])
            except (KeyError, TypeError):
                return 0
        unmatched_pis.sort(key=lambda t: -_pi_area(t[1]))

        for pi_idx, pi in unmatched_pis:
            layer_id = pi.get('layer')
            candidates_for_layer = layer_unmatched_gerber.get(layer_id, [])
            if not candidates_for_layer:
                print(f"[Gerber] Pour {pi.get('net')} layer={layer_id} 无可分配的 Gerber 区域")
                continue
            # 取该层最大的未匹配 Gerber 区域
            gi, area, mil_pts = candidates_for_layer.pop(0)
            matched_gerber_idx.add(gi)
            net = pi['net']
            print(f"[Gerber] 区域 layer={layer_id} -> {net} (面积兜底, area={area:.0f})")
            pours.append({
                'net': net,
                'layer': layer_id,
                'vertices': [{'x': px, 'y': py} for (px, py) in mil_pts],
            })

    # --- 第三轮：碎片归属 ---
    # 一个大铺铜(如 GND)在 Gerber 里会被走线/焊盘 clearance 切成多个碎片区域。
    # API 只看到一个 PrimitivePour，但 Gerber 有 N 个碎片——第一/二轮只能匹配到主区域，
    # 剩余碎片应归属到同层已匹配的最大面积 pour 的 net。
    if len(pours) < len(best_polys):
        # 建立 layer -> 已匹配的最大面积 net
        from collections import defaultdict as _dd
        layer_largest_net: Dict[int, str] = {}
        layer_largest_area: Dict[int, float] = {}
        for p in pours:
            layer = p['layer']
            # 计算该 pour 的面积
            try:
                area = Polygon([(v['x'], v['y']) for v in p['vertices']]).area
            except Exception:
                area = 0
            if area > layer_largest_area.get(layer, 0):
                layer_largest_area[layer] = area
                layer_largest_net[layer] = p['net']

        fragment_count = 0
        for gi, (layer_id, mil_pts, sp) in enumerate(best_polys):
            if gi in matched_gerber_idx:
                continue
            fallback_net = layer_largest_net.get(layer_id)
            if not fallback_net:
                continue
            fragment_count += 1
            matched_gerber_idx.add(gi)
            pours.append({
                'net': fallback_net,
                'layer': layer_id,
                'vertices': [{'x': px, 'y': py} for (px, py) in mil_pts],
            })
        if fragment_count:
            print(f"[Gerber] 碎片归属: {fragment_count} 个区域归入同层最大 pour 的 net")

    iou_count = len([1 for gi in matched_gerber_idx if gi < len(best_polys)])  # 第一轮匹配数
    fallback_count = len(pours) - iou_count
    print(f"[Gerber] 匹配完成: {len(pours)}/{len(best_polys)} 个区域关联到 net (IoU匹配:{iou_count}, 面积兜底:{fallback_count})")
    return pours, (best_dx, best_dy, best_ysign)
