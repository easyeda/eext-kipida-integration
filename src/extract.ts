import { EasyEDA_PcbData, EasyEDA_Track, EasyEDA_Via, EasyEDA_Pad, PourInfo } from './types';

const CONCURRENCY = 8;

async function runBatched<T, R>(items: T[], fn: (item: T) => Promise<R>): Promise<R[]> {
  const results: R[] = [];
  for (let i = 0; i < items.length; i += CONCURRENCY) {
    const batch = items.slice(i, i + CONCURRENCY);
    const batchResults = await Promise.all(batch.map(fn));
    results.push(...batchResults);
  }
  return results;
}

export class PcbExtractor {
  async extractAll(): Promise<EasyEDA_PcbData> {
    const netNames = await eda.pcb_Net.getAllNetsName();
    console.log(`[PcbExtractor] 找到 ${netNames.length} 个网络`);

    // ===== 诊断：遍历所有 PCB 图元类型，打印各类数量 =====
    const primitiveTypes = [
      'pcb_PrimitiveLine', 'pcb_PrimitivePolyline', 'pcb_PrimitiveArc',
      'pcb_PrimitiveVia', 'pcb_PrimitivePad', 'pcb_PrimitiveComponent',
      'pcb_PrimitiveFill', 'pcb_PrimitivePour', 'pcb_PrimitivePoured',
      'pcb_PrimitiveRegion', 'pcb_PrimitiveString', 'pcb_PrimitiveDimension',
      'pcb_PrimitiveImage', 'pcb_PrimitiveObject', 'pcb_PrimitiveAttribute',
    ];
    console.log('[PcbExtractor] ===== 图元类型诊断 =====');
    for (const typeName of primitiveTypes) {
      try {
        const api = (eda as any)[typeName];
        if (api && typeof api.getAll === 'function') {
          const items = await api.getAll();
          console.log(`[PcbExtractor]   ${typeName}: ${items?.length ?? 0} 个`);
          // 对 Polyline 额外打印前 3 条的结构
          if (typeName === 'pcb_PrimitivePolyline' && items && items.length > 0) {
            for (let pi = 0; pi < Math.min(3, items.length); pi++) {
              const p = items[pi];
              const methods = Object.getOwnPropertyNames(Object.getPrototypeOf(p))
                .filter(m => m.startsWith('getState_')).slice(0, 15);
              console.log(`[PcbExtractor]     Polyline[${pi}] methods: ${methods.join(', ')}`);
              try {
                const net = p.getState_Net?.();
                const layer = p.getState_Layer?.();
                console.log(`[PcbExtractor]     Polyline[${pi}] net=${net} layer=${layer}`);
              } catch {}
              try {
                const pts = p.getState_Points?.() ?? p.getState_PointList?.() ?? p.getState_Polyline?.();
                console.log(`[PcbExtractor]     Polyline[${pi}] points type=${typeof pts}, isArray=${Array.isArray(pts)}, len=${pts?.length}`);
                if (Array.isArray(pts) && pts.length > 0) {
                  console.log(`[PcbExtractor]     Polyline[${pi}] first point:`, pts[0]);
                }
              } catch (e) {
                console.log(`[PcbExtractor]     Polyline[${pi}] points extraction failed:`, e);
              }
            }
          }
          // 对 Arc 额外打印前 3 条的结构
          if (typeName === 'pcb_PrimitiveArc' && items && items.length > 0) {
            for (let ai = 0; ai < Math.min(3, items.length); ai++) {
              const a = items[ai];
              const methods = Object.getOwnPropertyNames(Object.getPrototypeOf(a))
                .filter(m => m.startsWith('getState_')).slice(0, 20);
              console.log(`[PcbExtractor]     Arc[${ai}] methods: ${methods.join(', ')}`);
              try {
                const net = a.getState_Net?.();
                const layer = a.getState_Layer?.();
                const width = a.getState_LineWidth?.();
                console.log(`[PcbExtractor]     Arc[${ai}] net=${net} layer=${layer} width=${width}`);
              } catch {}
              try {
                const sx = a.getState_StartX?.(), sy = a.getState_StartY?.();
                const ex = a.getState_EndX?.(), ey = a.getState_EndY?.();
                const cx = a.getState_CenterX?.() ?? a.getState_X?.();
                const cy = a.getState_CenterY?.() ?? a.getState_Y?.();
                const r = a.getState_Radius?.();
                console.log(`[PcbExtractor]     Arc[${ai}] start=(${sx},${sy}) end=(${ex},${ey}) center=(${cx},${cy}) radius=${r}`);
              } catch (e) {
                console.log(`[PcbExtractor]     Arc[${ai}] geometry failed:`, e);
              }
            }
          }
        }
      } catch {}
    }
    console.log('[PcbExtractor] ===== 诊断结束 =====');

    // 获取铜箔层名称映射（type=SIGNAL/TOP/BOTTOM），并识别外层
    const layerNames: Record<number, string> = {};
    const outerLayerIds = new Set<number>();
    const signalLayerIds: number[] = [];
    try {
      const allLayers = await eda.pcb_Layer.getAllLayers();
      for (const layer of allLayers) {
        const t = layer.type as string;
        if (t === 'SIGNAL' || t === 'TOP' || t === 'BOTTOM') {
          const id = layer.id as number;
          layerNames[id] = layer.name;
          signalLayerIds.push(id);
        }
      }
      if (signalLayerIds.length >= 2) {
        signalLayerIds.sort((a, b) => a - b);
        outerLayerIds.add(signalLayerIds[0]);
        outerLayerIds.add(signalLayerIds[signalLayerIds.length - 1]);
      } else {
        signalLayerIds.forEach(id => outerLayerIds.add(id));
      }
      console.log(`[PcbExtractor] 铜箔层:`, layerNames, '外层:', [...outerLayerIds]);
    } catch (e) {
      console.warn('[PcbExtractor] 获取层信息失败:', e);
    }

    const validNets = netNames.filter(n => n && n.trim() !== '');
    const t0 = Date.now();

    // 一次性获取所有走线和过孔
    const tracks: EasyEDA_Track[] = [];
    const vias: EasyEDA_Via[] = [];

    try {
      const allLines = await eda.pcb_PrimitiveLine.getAll();
      for (const line of allLines) {
        const track = this.extractTrack(line);
        if (track) tracks.push(track);
      }
    } catch (e) {
      console.warn('[PcbExtractor] 批量提取走线失败，回退到逐网络提取:', e);
      const trackResults = await runBatched(validNets, async (netName) => {
        const localTracks: EasyEDA_Track[] = [];
        try {
          const lines = await eda.pcb_PrimitiveLine.getAll(netName);
          for (const line of lines) {
            const track = this.extractTrack(line, netName);
            if (track) localTracks.push(track);
          }
        } catch {}
        return localTracks;
      });
      for (const r of trackResults) tracks.push(...r);
    }

    // 提取自由角度走线（Arc）——EasyEDA 把非 45°/90° 走线存为 PrimitiveArc，
    // 接口和 PrimitiveLine 完全相同（StartX/Y, EndX/Y, Net, Layer, LineWidth），直接复用 extractTrack。
    try {
      const allArcs = await eda.pcb_PrimitiveArc.getAll();
      for (const arc of allArcs) {
        const track = this.extractTrack(arc);
        if (track) tracks.push(track);
      }
      console.log(`[PcbExtractor] Arc (自由角度走线): ${allArcs.length} 个, 有效: ${tracks.length - (tracks.length - allArcs.length)} 条`);
    } catch (e) {
      console.warn('[PcbExtractor] 提取 Arc 失败:', e);
    }

    try {
      const allVias = await eda.pcb_PrimitiveVia.getAll();
      for (const via of allVias) {
        const v = this.extractVia(via);
        if (v) vias.push(v);
      }
    } catch (e) {
      console.warn('[PcbExtractor] 批量提取过孔失败，回退到逐网络提取:', e);
      const viaResults = await runBatched(validNets, async (netName) => {
        const localVias: EasyEDA_Via[] = [];
        try {
          const viaList = await eda.pcb_PrimitiveVia.getAll(netName);
          for (const via of viaList) {
            const v = this.extractVia(via, netName);
            if (v) localVias.push(v);
          }
        } catch {}
        return localVias;
      });
      for (const r of viaResults) vias.push(...r);
    }

    const t1 = Date.now();
    console.log(`[PcbExtractor] 走线/过孔提取耗时: ${t1 - t0}ms (tracks=${tracks.length}, vias=${vias.length})`);

    // 并发提取器件焊盘
    const pads: EasyEDA_Pad[] = [];
    const padKeySet = new Set<string>();

    try {
      const components = await eda.pcb_PrimitiveComponent.getAll();
      const compInfos = components.map(comp => {
        const refDes = typeof comp.getState_Designator === 'function'
          ? comp.getState_Designator() : undefined;
        const deviceName = typeof comp.getState_OtherProperty === 'function'
          ? (comp.getState_OtherProperty()?.['Device'] as string | undefined) : undefined;
        const compId = comp.getState_PrimitiveId();
        return { refDes, deviceName, compId };
      }).filter(c => !!c.compId);

      const padResults = await runBatched(compInfos, async ({ refDes, deviceName, compId }) => {
        const localPads: EasyEDA_Pad[] = [];
        try {
          const pins = await eda.pcb_PrimitiveComponent.getAllPinsByPrimitiveId(compId);
          if (!pins) return localPads;
          for (const pin of pins) {
            const pad = this.extractPad(pin, refDes, deviceName);
            if (pad) localPads.push(pad);
          }
        } catch (e) {
          console.warn(`[PcbExtractor] 提取器件 ${refDes} 焊盘失败:`, e);
        }
        return localPads;
      });

      for (const localPads of padResults) {
        for (const pad of localPads) {
          const key = `${pad.net}|${pad.x.toFixed(2)}|${pad.y.toFixed(2)}`;
          if (!padKeySet.has(key)) {
            pads.push(pad);
            padKeySet.add(key);
          }
        }
      }
    } catch (e) {
      console.warn('[PcbExtractor] 提取器件失败，回退到自由焊盘:', e);
      try {
        const allPadList = await eda.pcb_PrimitivePad.getAll();
        for (const pad of allPadList) {
          const p = this.extractPad(pad);
          if (!p) continue;
          const key = `${p.net}|${p.x.toFixed(2)}|${p.y.toFixed(2)}`;
          if (!padKeySet.has(key)) {
            pads.push(p);
            padKeySet.add(key);
          }
        }
      } catch {}
    }

    // 补充扫描：一次性获取所有 PrimitivePad 捕获直插式焊盘
    const t2 = Date.now();
    console.log(`[PcbExtractor] 器件焊盘提取耗时: ${t2 - t1}ms (pads=${pads.length})`);
    const beforeSupp = pads.length;
    try {
      const allPadList = await eda.pcb_PrimitivePad.getAll();
      for (const pad of allPadList) {
        const p = this.extractPad(pad);
        if (!p) continue;
        const key = `${p.net}|${p.x.toFixed(2)}|${p.y.toFixed(2)}`;
        if (!padKeySet.has(key)) {
          pads.push(p);
          padKeySet.add(key);
        }
      }
    } catch (e) {
      console.warn('[PcbExtractor] 补充焊盘扫描失败:', e);
    }
    console.log(`[PcbExtractor] 焊盘总数: ${pads.length} (组件焊盘=${beforeSupp}, 补充直插=${pads.length - beforeSupp})`);
    const t3 = Date.now();
    console.log(`[PcbExtractor] 补充焊盘扫描耗时: ${t3 - t2}ms`);

    // 轻量级 Pour 信息：只取 net + layer + 外轮廓包围盒（用于服务端和 Gerber 几何做 IoU 匹配）。
    // 完全不碰 PourFills / getState_PourFills（那是不可靠的部分）。
    const pourInfos: PourInfo[] = [];
    try {
      const pours = await eda.pcb_PrimitivePour.getAll();
      for (const pour of pours) {
        const net = pour.getState_Net();
        const layer = pour.getState_Layer() as number;
        if (!net || net.trim() === '') continue;
        // 外轮廓：getState_ComplexPolygon → getSource → 解析出包围盒即可
        try {
          const polygon = pour.getState_ComplexPolygon();
          const src = polygon.getSource();
          const bbox = this.computeBbox(src);
          if (bbox) {
            pourInfos.push({ net, layer, bbox });
          }
        } catch (e) {
          // 拿不到外轮廓也没关系，只是少一个匹配候选
          console.warn(`[PcbExtractor] Pour ${net} layer=${layer} 获取包围盒失败:`, e);
        }
      }
      console.log(`[PcbExtractor] Pour 轻量信息: ${pourInfos.length} 个`);
    } catch (e) {
      console.warn('[PcbExtractor] 获取 PrimitivePour 失败:', e);
    }
    const t4 = Date.now();
    console.log(`[PcbExtractor] Pour 信息提取耗时: ${t4 - t3}ms`);

    // 铺铜几何不再从 API 提取（解析不可靠）。改由 gerber.ts 逐层导出 Gerber，
    // 服务端从 Gerber 区域填充解析铺铜多边形，并用 pourInfos 做 IoU 匹配回填 net。
    console.log(`[PcbExtractor] 提取完成: tracks=${tracks.length}, vias=${vias.length}, pads=${pads.length}, pourInfos=${pourInfos.length}`);
    console.log(`[PcbExtractor] 总提取耗时: ${t4 - t0}ms`);

    // 保留全部信号层（含纯 plane 层）：它们可能只有铺铜没有走线，
    // 但正需要其 Gerber 几何，故不再按 tracks/pads 过滤。
    console.log(`[PcbExtractor] 信号铜箔层:`, layerNames, '外层:', [...outerLayerIds]);

    return { tracks, vias, pads, pourInfos, layerNames, outerLayerIds, signalLayerIds };
  }

  /** 从 ComplexPolygon source 数组计算包围盒（mil） */
  private computeBbox(source: any): { minx: number; miny: number; maxx: number; maxy: number } | null {
    if (!source || !Array.isArray(source)) return null;
    let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
    let count = 0;
    for (let i = 0; i < source.length; i++) {
      const token = source[i];
      if (typeof token === 'number' && i + 1 < source.length && typeof source[i + 1] === 'number') {
        const x = token;
        const y = source[i + 1];
        minx = Math.min(minx, x); miny = Math.min(miny, y);
        maxx = Math.max(maxx, x); maxy = Math.max(maxy, y);
        count++;
        i++; // skip y
      }
    }
    return count >= 3 ? { minx, miny, maxx, maxy } : null;
  }

  private extractTrack(primitive: any, netName?: string): EasyEDA_Track | null {
    try {
      const x1 = primitive.getState_StartX();
      const y1 = primitive.getState_StartY();
      const x2 = primitive.getState_EndX();
      const y2 = primitive.getState_EndY();
      const width = primitive.getState_LineWidth();
      const layer = primitive.getState_Layer();
      if (x1 === null || y1 === null || x2 === null || y2 === null) return null;
      let net = netName;
      if (!net) {
        try {
          const netObj = primitive.getState_Net?.() ?? primitive.getState_NetName?.();
          if (typeof netObj === 'string') net = netObj;
          else if (netObj && typeof netObj.name === 'string') net = netObj.name;
          else if (netObj && typeof netObj.getName === 'function') net = netObj.getName();
        } catch {}
      }
      if (!net || net.trim() === '') return null;
      return { net, x1, y1, x2, y2, width: width || 0.254, layer: layer || 1 };
    } catch { return null; }
  }

  private extractVia(primitive: any, netName?: string): EasyEDA_Via | null {
    try {
      const x = primitive.getState_X();
      const y = primitive.getState_Y();
      const diameter = primitive.getState_Diameter();
      const holeDiameter = primitive.getState_HoleDiameter();
      if (x === null || y === null) return null;
      let net = netName;
      if (!net) {
        try {
          const netObj = primitive.getState_Net?.() ?? primitive.getState_NetName?.();
          if (typeof netObj === 'string') net = netObj;
          else if (netObj && typeof netObj.name === 'string') net = netObj.name;
          else if (netObj && typeof netObj.getName === 'function') net = netObj.getName();
        } catch {}
      }
      if (!net || net.trim() === '') return null;
      return { net, x, y, diameter: diameter || 0.6, hole_diameter: holeDiameter || 0.3 };
    } catch { return null; }
  }

  private extractPad(primitive: any, refDes?: string, deviceName?: string, fallbackNet?: string): EasyEDA_Pad | null {
    try {
      const x = primitive.getState_X();
      const y = primitive.getState_Y();
      const padNumber = primitive.getState_PadNumber();
      const padShape = primitive.getState_Pad();
      const padW = Array.isArray(padShape) && typeof padShape[1] === 'number' ? padShape[1] : 0;
      const padH = Array.isArray(padShape) && typeof padShape[2] === 'number' ? padShape[2] : 0;

      const rawLayer = typeof primitive.getState_Layer === 'function'
        ? primitive.getState_Layer() as number
        : undefined;
      const layer = rawLayer === 12 ? undefined : rawLayer;

      let netName = fallbackNet;
      if (!netName) {
        try {
          const netObj = primitive.getState_Net?.() ?? primitive.getState_NetName?.();
          if (typeof netObj === 'string') netName = netObj;
          else if (netObj && typeof netObj.name === 'string') netName = netObj.name;
          else if (netObj && typeof netObj.getName === 'function') netName = netObj.getName();
        } catch {}
      }

      if (x === null || y === null || !netName) return null;

      return {
        net: netName,
        x, y,
        pad_number: padNumber || '?',
        width: padW || 0.6,
        height: padH || 0.6,
        layer: layer || undefined,
        ref_des: refDes || undefined,
        device_name: deviceName || undefined,
      };
    } catch { return null; }
  }
}
