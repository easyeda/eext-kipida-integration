import { EasyEDA_PcbData, EasyEDA_Track, EasyEDA_Via, EasyEDA_Pad, EasyEDA_CopperPour } from './types';

export class PcbExtractor {
  async extractAll(): Promise<EasyEDA_PcbData> {
    const tracks: EasyEDA_Track[] = [];
    const vias: EasyEDA_Via[] = [];
    const pads: EasyEDA_Pad[] = [];

    const netNames = await eda.pcb_Net.getAllNetsName();
    console.log(`[PcbExtractor] 找到 ${netNames.length} 个网络`);

    // 获取铜箔层名称映射（type=SIGNAL），并识别外层（最小和最大层ID）
    const layerNames: Record<number, string> = {};
    const outerLayerIds = new Set<number>();
    try {
      const allLayers = await eda.pcb_Layer.getAllLayers();
      const signalLayerIds: number[] = [];
      for (const layer of allLayers) {
        if ((layer.type as string) === 'SIGNAL') {
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

    // 提取走线和过孔（按网络遍历）
    for (const netName of netNames) {
      if (!netName || netName.trim() === '') continue;

      try {
        const lines = await eda.pcb_PrimitiveLine.getAll(netName);
        for (const line of lines) {
          const track = this.extractTrack(line, netName);
          if (track) tracks.push(track);
        }
      } catch (e) {
        console.warn(`[PcbExtractor] 提取走线 ${netName} 失败:`, e);
      }

      try {
        const viaList = await eda.pcb_PrimitiveVia.getAll(netName);
        for (const via of viaList) {
          const v = this.extractVia(via, netName);
          if (v) vias.push(v);
        }
      } catch (e) {
        console.warn(`[PcbExtractor] 提取过孔 ${netName} 失败:`, e);
      }
    }

    // 提取器件焊盘（含 ref_des）
    try {
      const components = await eda.pcb_PrimitiveComponent.getAll();
      for (const comp of components) {
        const refDes = typeof comp.getState_Designator === 'function'
          ? comp.getState_Designator() : undefined;
        const deviceName = typeof comp.getState_Name === 'function'
          ? comp.getState_Name() : undefined;
        const compId = comp.getState_PrimitiveId();
        if (!compId) continue;

        try {
          const pins = await eda.pcb_PrimitiveComponent.getAllPinsByPrimitiveId(compId);
          if (!pins) continue;
          for (const pin of pins) {
            const pad = this.extractPad(pin, refDes, deviceName);
            if (pad) pads.push(pad);
          }
        } catch (e) {
          console.warn(`[PcbExtractor] 提取器件 ${refDes} 焊盘失败:`, e);
        }
      }
    } catch (e) {
      console.warn('[PcbExtractor] 提取器件失败，回退到自由焊盘:', e);
      for (const netName of netNames) {
        if (!netName || netName.trim() === '') continue;
        try {
          const padList = await eda.pcb_PrimitivePad.getAll(undefined, netName);
          for (const pad of padList) {
            const p = this.extractPad(pad, undefined, undefined, netName);
            if (p) pads.push(p);
          }
        } catch {}
      }
    }

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
        const vertices = this.parsePolygonVertices(polygon.getSource());
        if (vertices.length >= 3) {
          copperPours.push({ net, layer, vertices, is_fill: true });
        }
      }
      console.log(`[PcbExtractor] PrimitiveFill: ${fills.length} 个, 有效: ${copperPours.length} 个`);
    } catch (e) {
      console.warn('[PcbExtractor] 提取 PrimitiveFill 失败:', e);
    }

    // 覆铜实际填充区域（PrimitivePoured）
    // 先建立 pourId → {net, layer} 映射
    try {
      const pourMap = new Map<string, { net: string; layer: number }>();
      const pours = await eda.pcb_PrimitivePour.getAll();
      for (const pour of pours) {
        const net = pour.getState_Net();
        if (!net || net.trim() === '') continue;
        const layer = pour.getState_Layer() as number;
        const id = pour.getState_PrimitiveId();
        pourMap.set(id, { net, layer });
      }

      // getAll 在类型定义中被标记为 Excluded，但运行时可用
      const pouredList: any[] = await (eda.pcb_PrimitivePoured as any).getAll();
      const beforeCount = copperPours.length;
      for (const poured of pouredList) {
        const pourId = poured.getState_PourPrimitiveId();
        const info = pourMap.get(pourId);
        if (!info) continue;
        const fills: any[] = poured.getState_PourFills();
        for (const pourFill of fills) {
          const vertices = this.parsePolygonVertices(pourFill.path.getSource());
          if (vertices.length >= 3) {
            copperPours.push({ net: info.net, layer: info.layer, vertices, is_fill: false });
          }
        }
      }
      console.log(`[PcbExtractor] PrimitivePoured: ${pouredList.length} 个, 新增铺铜: ${copperPours.length - beforeCount} 个`);
    } catch (e) {
      console.warn('[PcbExtractor] 提取 PrimitivePoured 失败:', e);
    }

    console.log(`[PcbExtractor] 提取完成: tracks=${tracks.length}, vias=${vias.length}, pads=${pads.length}, copperPours=${copperPours.length}`);
    return { tracks, vias, pads, copperPours, layerNames, outerLayerIds };
  }

  private parsePolygonVertices(source: any): Array<{ x: number; y: number }> {
    if (!source) return [];
    const arr: Array<any> = Array.isArray(source) ? source : [];
    const vertices: Array<{ x: number; y: number }> = [];
    let i = 0;

    while (i < arr.length) {
      const token = arr[i];

      if (token === 'R') {
        // R x y width height rotation round → 展开为4个矩形顶点
        const x = arr[i + 1], y = arr[i + 2], w = arr[i + 3], h = arr[i + 4];
        if (typeof x === 'number' && typeof y === 'number' && typeof w === 'number' && typeof h === 'number') {
          vertices.push({ x, y }, { x: x + w, y }, { x: x + w, y: y + h }, { x, y: y + h });
        }
        i += 7;
      } else if (token === 'CIRCLE') {
        // CIRCLE cx cy r → 近似为8边形
        const cx = arr[i + 1], cy = arr[i + 2], r = arr[i + 3];
        if (typeof cx === 'number' && typeof cy === 'number' && typeof r === 'number') {
          for (let k = 0; k < 8; k++) {
            const angle = (k / 8) * 2 * Math.PI;
            vertices.push({ x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) });
          }
        }
        i += 4;
      } else if (token === 'L' || token === 'ARC' || token === 'CARC' || token === 'C') {
        i += 1; // 跳过命令符，后续数字对会在下一轮被收集
      } else if (typeof token === 'number') {
        // 数字对 x, y
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

  private extractTrack(primitive: any, netName: string): EasyEDA_Track | null {
    try {
      const x1 = primitive.getState_StartX();
      const y1 = primitive.getState_StartY();
      const x2 = primitive.getState_EndX();
      const y2 = primitive.getState_EndY();
      const width = primitive.getState_LineWidth();
      const layer = primitive.getState_Layer();
      if (x1 === null || y1 === null || x2 === null || y2 === null) return null;
      return { net: netName, x1, y1, x2, y2, width: width || 0.254, layer: layer || 1 };
    } catch { return null; }
  }

  private extractVia(primitive: any, netName: string): EasyEDA_Via | null {
    try {
      const x = primitive.getState_X();
      const y = primitive.getState_Y();
      const diameter = primitive.getState_Diameter();
      const holeDiameter = primitive.getState_HoleDiameter();
      if (x === null || y === null) return null;
      return { net: netName, x, y, diameter: diameter || 0.6, hole_diameter: holeDiameter || 0.3 };
    } catch { return null; }
  }

  private extractPad(primitive: any, refDes?: string, deviceName?: string, fallbackNet?: string): EasyEDA_Pad | null {
    try {
      const x = primitive.getState_X();
      const y = primitive.getState_Y();
      const padNumber = primitive.getState_PadNumber();
      const padShape = primitive.getState_Pad();
      const layer = typeof primitive.getState_Layer === 'function'
        ? primitive.getState_Layer() as number
        : undefined;

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
        width: padShape?.xSize || 0.6,
        height: padShape?.ySize || 0.6,
        layer: layer || undefined,
        ref_des: refDes || undefined,
        device_name: deviceName || undefined,
      };
    } catch { return null; }
  }
}

