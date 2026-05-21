import { EasyEDA_PcbData, EasyEDA_Track, EasyEDA_Via, EasyEDA_Pad, EasyEDA_CopperPour } from './types';

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

    // 获取铜箔层名称映射（type=SIGNAL/TOP/BOTTOM），并识别外层
    const layerNames: Record<number, string> = {};
    const outerLayerIds = new Set<number>();
    try {
      const allLayers = await eda.pcb_Layer.getAllLayers();
      const signalLayerIds: number[] = [];
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

    // 提取铺铜（PrimitiveFill + PrimitivePoured 实际填充区域）
    const copperPours: EasyEDA_CopperPour[] = [];

    // 静态填充
    try {
      const fills = await eda.pcb_PrimitiveFill.getAll();
      for (const fill of fills) {
        const net = fill.getState_Net();
        if (!net || net.trim() === '') continue;
        const layer = fill.getState_Layer() as number;
        const polygon = fill.getState_ComplexPolygon();
        const rawVertices = this.parsePolygonVertices(polygon.getSource());
        if (rawVertices.length >= 3) {
          copperPours.push({ net, layer, vertices: rawVertices, is_fill: true });
        }
      }
      console.log(`[PcbExtractor] PrimitiveFill: ${fills.length} 个, 有效: ${copperPours.length} 个`);
    } catch (e) {
      console.warn('[PcbExtractor] 提取 PrimitiveFill 失败:', e);
    }

    // 覆铜填充区域（PrimitivePoured.getState_PourFills）
    const pourInfoMap = new Map<string, { net: string; layer: number }>();
    try {
      const pours = await eda.pcb_PrimitivePour.getAll();
      for (const pour of pours) {
        const net = pour.getState_Net();
        const layer = pour.getState_Layer() as number;
        const pid = pour.getState_PrimitiveId();
        if (pid && net) pourInfoMap.set(pid, { net, layer });
        console.log(`[PcbExtractor] Pour边框: pid=${pid} net=${net} layer=${layer}`);
      }
      console.log(`[PcbExtractor] PrimitivePour 映射: ${pourInfoMap.size} 个`);
    } catch (e) {
      console.warn('[PcbExtractor] 建立 PrimitivePour 映射失败:', e);
    }

    // 并发遍历 PrimitivePoured
    try {
      const poureds = await eda.pcb_PrimitivePoured.getAll();
      const beforeCount = copperPours.length;

      const pouredResults = await runBatched(poureds, async (poured) => {
        const localPours: EasyEDA_CopperPour[] = [];
        const pourPid = poured.getState_PourPrimitiveId();
        const pouredPid = poured.getState_PrimitiveId();
        const info = pourInfoMap.get(pourPid);
        if (!info) {
          console.warn(`[PcbExtractor] Poured pid=${pouredPid} 的 pourPid=${pourPid} 未找到对应 Pour`);
          return localPours;
        }
        if (!info.net || info.net.trim() === '') return localPours;
        const { net, layer } = info;
        try {
          const pourFills = poured.getState_PourFills();
          if (!pourFills || !Array.isArray(pourFills)) {
            console.warn(`[PcbExtractor] PourFills 非数组: net=${net} layer=${layer}`, typeof pourFills);
            return localPours;
          }
          console.log(`[PcbExtractor] Poured: net=${net} layer=${layer} pourFills.length=${pourFills.length}`);
          if (pourFills.length === 0) {
            const pourObj = await eda.pcb_PrimitivePour.get(pourPid);
            if (pourObj) {
              const polygon = pourObj.getState_ComplexPolygon();
              const parsed = this.parsePolygonVertices(polygon.getSource());
              const fallbackVertices = parsed.map(v => ({ x: v.x * 10, y: v.y * 10 }));
              if (fallbackVertices.length >= 3) {
                localPours.push({ net, layer, vertices: fallbackVertices, is_fill: false });
                const xs = fallbackVertices.map(v => v.x);
                const ys = fallbackVertices.map(v => v.y);
                console.log(`[PcbExtractor] PourFill(fallback边框): net=${net} layer=${layer} pts=${fallbackVertices.length} x=[${Math.min(...xs).toFixed(2)},${Math.max(...xs).toFixed(2)}] y=[${Math.min(...ys).toFixed(2)},${Math.max(...ys).toFixed(2)}]`);
              }
            }
            return localPours;
          }
          for (let fi = 0; fi < pourFills.length; fi++) {
            const fill = pourFills[fi];
            if (fill && fill.fill === false) continue;
            let parsed: Array<{ x: number; y: number }> = [];
            if (fill && fill.path) {
              let src = fill.path.getSource();
              if (Array.isArray(src)) {
                while (src.length === 1 && Array.isArray(src[0])) {
                  src = src[0];
                }
                if (src.length > 0 && Array.isArray(src[0])) {
                  for (const sub of src) {
                    if (Array.isArray(sub)) {
                      parsed.push(...this.parsePolygonVertices(sub));
                    }
                  }
                } else {
                  parsed = this.parsePolygonVertices(src);
                }
              }
              if (parsed.length < 3) {
                const srcRaw = fill.path.getSource();
                const isNested = Array.isArray(srcRaw) && srcRaw.length > 0 && Array.isArray(srcRaw[0]);
                const innerLen = isNested ? (srcRaw[0] as any[]).length : 0;
                console.warn(`[PcbExtractor] PourFill解析不足(v3): net=${net} layer=${layer} fill#${fi} pts=${parsed.length} nested=${isNested} innerLen=${innerLen} srcType=${typeof srcRaw} srcIsArr=${Array.isArray(srcRaw)} src=${JSON.stringify(srcRaw).substring(0, 500)}`);
              }
            } else {
              parsed = this.parsePourFillVertices(fill);
            }
            const rawVertices = parsed.map(v => ({ x: v.x * 10, y: v.y * 10 }));
            if (rawVertices.length >= 3) {
              localPours.push({ net, layer, vertices: rawVertices, is_fill: false });
              const xs = rawVertices.map(v => v.x);
              const ys = rawVertices.map(v => v.y);
              console.log(`[PcbExtractor] PourFill: net=${net} layer=${layer} fill#${fi} pts=${rawVertices.length} x=[${Math.min(...xs).toFixed(2)},${Math.max(...xs).toFixed(2)}] y=[${Math.min(...ys).toFixed(2)},${Math.max(...ys).toFixed(2)}]`);
            }
          }
        } catch (e) {
          console.warn(`[PcbExtractor] getState_PourFills 失败: net=${net} layer=${layer}`, e);
        }
        return localPours;
      });

      for (const localPours of pouredResults) {
        copperPours.push(...localPours);
      }
      console.log(`[PcbExtractor] PrimitivePoured: ${poureds.length} 个覆铜, 新增填充区域: ${copperPours.length - beforeCount} 个`);
    } catch (e) {
      console.warn('[PcbExtractor] 提取 PrimitivePoured 失败:', e);
    }

    console.log(`[PcbExtractor] 提取完成: tracks=${tracks.length}, vias=${vias.length}, pads=${pads.length}, copperPours=${copperPours.length}`);
    const t4 = Date.now();
    console.log(`[PcbExtractor] 铺铜提取耗时: ${t4 - t3}ms`);
    console.log(`[PcbExtractor] 总提取耗时: ${t4 - t0}ms`);

    // 过滤 layerNames：只保留实际有数据的铜箔层
    const usedLayers = new Set<number>();
    for (const t of tracks) usedLayers.add(t.layer);
    for (const p of copperPours) usedLayers.add(p.layer);
    for (const p of pads) { if (p.layer) usedLayers.add(p.layer); }
    const filteredLayerNames: Record<number, string> = {};
    for (const lid of Object.keys(layerNames)) {
      const id = Number(lid);
      if (usedLayers.has(id)) filteredLayerNames[id] = layerNames[id];
    }
    const filteredOuterLayerIds = new Set<number>();
    const filteredIds = Object.keys(filteredLayerNames).map(Number).sort((a, b) => a - b);
    if (filteredIds.length >= 2) {
      filteredOuterLayerIds.add(filteredIds[0]);
      filteredOuterLayerIds.add(filteredIds[filteredIds.length - 1]);
    } else {
      filteredIds.forEach(id => filteredOuterLayerIds.add(id));
    }
    console.log(`[PcbExtractor] 实际使用铜箔层:`, filteredLayerNames);

    return { tracks, vias, pads, copperPours, layerNames: filteredLayerNames, outerLayerIds: filteredOuterLayerIds };
  }

  private parsePolygonVertices(source: any): Array<{ x: number; y: number }> {
    if (!source) return [];
    const arr: Array<any> = Array.isArray(source) ? source : [];
    const vertices: Array<{ x: number; y: number }> = [];
    let i = 0;

    while (i < arr.length) {
      const token = arr[i];

      if (token === 'R') {
        const x = arr[i + 1], y = arr[i + 2], w = arr[i + 3], h = arr[i + 4];
        if (typeof x === 'number' && typeof y === 'number' && typeof w === 'number' && typeof h === 'number') {
          vertices.push({ x, y }, { x: x + w, y }, { x: x + w, y: y + h }, { x, y: y + h });
        }
        i += 7;
      } else if (token === 'CIRCLE') {
        const cx = arr[i + 1], cy = arr[i + 2], r = arr[i + 3];
        if (typeof cx === 'number' && typeof cy === 'number' && typeof r === 'number') {
          for (let k = 0; k < 8; k++) {
            const angle = (k / 8) * 2 * Math.PI;
            vertices.push({ x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) });
          }
        }
        i += 4;
      } else if (token === 'L') {
        i += 1;
      } else if (token === 'ARC' || token === 'CARC') {
        const endX = arr[i + 2], endY = arr[i + 3];
        if (typeof endX === 'number' && typeof endY === 'number') {
          vertices.push({ x: endX, y: endY });
        }
        i += 4;
      } else if (token === 'C') {
        const x4 = arr[i + 5], y4 = arr[i + 6];
        if (typeof x4 === 'number' && typeof y4 === 'number') {
          vertices.push({ x: x4, y: y4 });
        }
        i += 7;
      } else if (typeof token === 'number') {
        const x = token;
        const y = arr[i + 1];
        if (typeof y === 'number') {
          vertices.push({ x, y });
          i += 2;
        } else {
          i += 1;
        }
      } else {
        i += 1;
      }
    }

    return vertices;
  }

  private parsePourFillVertices(fill: any): Array<{ x: number; y: number }> {
    if (!fill) return [];
    const vertices: Array<{ x: number; y: number }> = [];

    if (Array.isArray(fill)) {
      for (const item of fill) {
        if (item && typeof item.x === 'number' && typeof item.y === 'number') {
          vertices.push({ x: item.x, y: item.y });
        } else if (Array.isArray(item) && item.length >= 2 && typeof item[0] === 'number') {
          vertices.push({ x: item[0], y: item[1] });
        }
      }
      if (vertices.length === 0 && fill.length >= 4 && typeof fill[0] === 'number') {
        for (let i = 0; i + 1 < fill.length; i += 2) {
          if (typeof fill[i] === 'number' && typeof fill[i + 1] === 'number') {
            vertices.push({ x: fill[i], y: fill[i + 1] });
          }
        }
      }
      if (vertices.length === 0) {
        return this.parsePolygonVertices(fill);
      }
    }

    if (vertices.length === 0 && fill.getSource) {
      return this.parsePolygonVertices(fill.getSource());
    }
    if (vertices.length === 0 && fill.points) {
      return this.parsePourFillVertices(fill.points);
    }

    if (vertices.length === 0) {
      console.warn('[PcbExtractor] Unknown PourFill format:', JSON.stringify(fill).substring(0, 200));
    }

    return vertices;
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
