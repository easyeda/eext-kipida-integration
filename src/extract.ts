import { EasyEDA_PcbData, EasyEDA_Track, EasyEDA_Via, EasyEDA_Pad } from './types';

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

    console.log(`[PcbExtractor] 提取完成: tracks=${tracks.length}, vias=${vias.length}, pads=${pads.length}`);
    return { tracks, vias, pads, layerNames, outerLayerIds };
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

