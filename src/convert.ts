import {
  EasyEDA_PcbData,
  Kipida_PcbData,
  Kipida_Node,
  Kipida_Resistance,
  Kipida_Connection,
  Kipida_CopperPour,
  Kipida_Source,
  Kipida_Load,
} from './types';

/**
 * convert.ts - 数据格式转换模块
 * 将 EasyEDA 格式转换为 KiPIDA 格式
 */

export class PcbDataConverter {
  // 铜的电阻率 (Ω·mm)
  private readonly COPPER_RESISTIVITY = 1.72e-5;
  // 外层铜厚 1oz = 0.035mm，内层铜厚默认也取 0.035mm
  private readonly OUTER_THICKNESS = 0.035;
  private readonly INNER_THICKNESS = 0.035;
  // 坐标精度
  private readonly COORD_PRECISION = 0.01;

  private outerLayerIds: Set<number> = new Set();

  // 节点去重映射
  private nodeMap: Map<string, Kipida_Node> = new Map();
  private nodeIdMap: Map<string, string> = new Map();
  private _lastNodes: Kipida_Node[] = [];

  getNodes(): Kipida_Node[] {
    return this._lastNodes;
  }

  /**
   * 转换入口
   */
  convert(data: EasyEDA_PcbData): Kipida_PcbData {
    this.nodeMap = new Map();
    this.nodeIdMap = new Map();
    this.outerLayerIds = data.outerLayerIds ?? new Set();

    const nodes: Kipida_Node[] = [];
    const resistances: Kipida_Resistance[] = [];
    const connections: Kipida_Connection[] = [];

    this.extractNodes(data.pads, data.vias, nodes);
    this.extractResistances(data.tracks, nodes, resistances, connections);
    this.addViaConnections(data.vias, nodes, connections);

    this._lastNodes = nodes;
    const { sources, loads } = this.generateSourcesAndLoads(nodes);

    const copperPours: Kipida_CopperPour[] = (data.copperPours || []).map(p => ({
      net: p.net,
      layer: p.layer,
      vertices: p.vertices,
    }));

    return {
      nodes,
      resistances,
      connections,
      sources,
      loads,
      copper_pours: copperPours,
      metadata: {
        total_nets: this.countNets(data),
        total_tracks: data.tracks.length,
        total_vias: data.vias.length,
        total_pads: data.pads.length,
        extracted_at: new Date().toISOString(),
      },
    };
  }

  private generateSourcesAndLoads(nodes: Kipida_Node[]): {
    sources: Kipida_Source[];
    loads: Kipida_Load[];
  } {
    const sources: Kipida_Source[] = [];
    const loads: Kipida_Load[] = [];

    // Group pad nodes by net
    const netPads = new Map<string, Kipida_Node[]>();
    for (const node of nodes) {
      if (node.type !== 'pad') continue;
      if (!netPads.has(node.net)) netPads.set(node.net, []);
      netPads.get(node.net)!.push(node);
    }

    // For each net: first pad = 1V source, rest = 0.1A loads
    for (const [, pads] of netPads) {
      if (pads.length === 0) continue;
      sources.push({ node_id: pads[0].id, voltage: 1.0 });
      for (let i = 1; i < pads.length; i++) {
        loads.push({ node_id: pads[i].id, current: 0.1 });
      }
    }

    return { sources, loads };
  }

  /**
   * 提取节点
   */
  private extractNodes(
    pads: EasyEDA_PcbData['pads'],
    vias: EasyEDA_PcbData['vias'],
    nodes: Kipida_Node[]
  ) {
    const addedIds = new Set<string>();

    for (const pad of pads) {
      const node = this.createNode(pad.x, pad.y, pad.net, 'pad', pad.pad_number, pad.ref_des, pad.device_name, pad.layer);
      if (node && !addedIds.has(node.id)) {
        nodes.push(node);
        addedIds.add(node.id);
      }
    }

    for (const via of vias) {
      const node = this.createNode(via.x, via.y, via.net, 'via');
      if (node && !addedIds.has(node.id)) {
        nodes.push(node);
        addedIds.add(node.id);
      }
    }
  }

  /**
   * 创建节点
   */
  private createNode(
    x: number,
    y: number,
    net: string,
    type: 'pad' | 'via' | 'junction',
    padNumber?: string,
    refDes?: string,
    deviceName?: string,
    layer?: number
  ): Kipida_Node | null {
    const key = this.makeNodeKey(net, x, y, type, layer);

    if (this.nodeMap.has(key)) {
      return this.nodeMap.get(key)!;
    }

    const id = this.generateId('node');
    const node: Kipida_Node = { id, net, type, x, y, pad_number: padNumber, ref_des: refDes, device_name: deviceName, layer };

    this.nodeMap.set(key, node);
    return node;
  }

  /**
   * 提取走线电阻
   */
  private extractResistances(
    tracks: EasyEDA_PcbData['tracks'],
    nodes: Kipida_Node[],
    resistances: Kipida_Resistance[],
    connections: Kipida_Connection[]
  ) {
    const addedIds = new Set<string>(nodes.map(n => n.id));

    for (const track of tracks) {
      if (!track.net || track.net.trim() === '') continue;

      const startNode = this.createNode(track.x1, track.y1, track.net, 'junction', undefined, undefined, undefined, track.layer);
      const endNode = this.createNode(track.x2, track.y2, track.net, 'junction', undefined, undefined, undefined, track.layer);

      if (!startNode || !endNode) continue;

      if (!addedIds.has(startNode.id)) { nodes.push(startNode); addedIds.add(startNode.id); }
      if (!addedIds.has(endNode.id)) { nodes.push(endNode); addedIds.add(endNode.id); }

      const thickness = (this.outerLayerIds.size === 0 || this.outerLayerIds.has(track.layer))
        ? this.OUTER_THICKNESS
        : this.INNER_THICKNESS;
      const length = this.calculateLength(track.x1, track.y1, track.x2, track.y2);
      const area = track.width * thickness;
      const resistance = (this.COPPER_RESISTIVITY * length) / area;

      // 创建电阻
      const resId = this.generateId('res');
      const res: Kipida_Resistance = {
        id: resId,
        start_node: startNode.id,
        end_node: endNode.id,
        net: track.net,
        length,
        width: track.width,
        thickness: thickness,
        layer: track.layer,
        resistance,
      };
      resistances.push(res);

      // 创建连接
      connections.push({
        from_node: startNode.id,
        to: endNode.id,
        type: 'track',
        net: track.net,
        resistance_id: resId,
      });
    }
  }

  /**
   * 添加过孔连接
   */
  private addViaConnections(
    vias: EasyEDA_PcbData['vias'],
    nodes: Kipida_Node[],
    connections: Kipida_Connection[]
  ) {
    for (const via of vias) {
      if (!via.net || via.net.trim() === '') continue;

      const nodeKey = this.makeNodeKey(via.net, via.x, via.y, 'via');
      const node = this.nodeMap.get(nodeKey);

      if (node) {
        connections.push({
          from_node: node.id,
          to: node.id,
          type: 'via',
          net: via.net,
        });
      }
    }
  }

  /**
   * 生成节点唯一键
   */
  private makeNodeKey(net: string, x: number, y: number, type: string, layer?: number): string {
    const px = x.toFixed(2);
    const py = y.toFixed(2);
    const l = layer !== undefined ? layer : 'any';
    return `${net}|${px}|${py}|${type}|${l}`;
  }

  /**
   * 生成唯一ID
   */
  private generateId(prefix: string): string {
    return `${prefix}_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
  }

  /**
   * 计算长度
   */
  private calculateLength(x1: number, y1: number, x2: number, y2: number): number {
    const dx = x2 - x1;
    const dy = y2 - y1;
    return Math.sqrt(dx * dx + dy * dy);
  }

  /**
   * 统计网络数量
   */
  private countNets(data: EasyEDA_PcbData): number {
    const nets = new Set<string>();
    data.tracks.forEach(t => nets.add(t.net));
    data.vias.forEach(v => nets.add(v.net));
    data.pads.forEach(p => nets.add(p.net));
    return nets.size;
  }
}