// ============================================================
// EasyEDA 原始数据类型
// ============================================================

export interface EasyEDA_Track {
  net: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  width: number;
  layer: number;
}

export interface EasyEDA_Via {
  net: string;
  x: number;
  y: number;
  diameter: number;
  hole_diameter: number;
}

export interface EasyEDA_Pad {
  net: string;
  x: number;
  y: number;
  pad_number: string;
  width: number;
  height: number;
  layer?: number;
  ref_des?: string;
  device_name?: string;
}

export interface EasyEDA_CopperPour {
  net: string;
  layer: number;
  vertices: Array<{ x: number; y: number }>;
  is_fill: boolean;
}

/** Pour 的轻量信息（只从 API 取 net/layer/外轮廓包围盒，不碰不可靠的 PourFills 几何） */
export interface PourInfo {
  net: string;
  layer: number;
  /** 外轮廓包围盒 (mil)，用于和 Gerber region 做 IoU 匹配 */
  bbox: { minx: number; miny: number; maxx: number; maxy: number };
}

export interface EasyEDA_PcbData {
  tracks: EasyEDA_Track[];
  vias: EasyEDA_Via[];
  pads: EasyEDA_Pad[];
  copperPours?: EasyEDA_CopperPour[];
  /** Pour 轻量信息（net + layer + bbox），用于服务端和 Gerber 几何做 IoU 匹配 */
  pourInfos?: PourInfo[];
  layerNames?: Record<number, string>;
  outerLayerIds?: Set<number>;
  /** 所有信号铜箔层 id（SIGNAL/TOP/BOTTOM），不经使用过滤，供 Gerber 导出用 */
  signalLayerIds?: number[];
}

/** 单层 Gerber 导出结果（base64），铺铜几何由服务端从中解析 */
export interface GerberLayer {
  layer: number;
  data: string;
  filename?: string;
}

// ============================================================
// KiPIDA 分析数据类型
// ============================================================

export interface Kipida_Node {
  id: string;
  net: string;
  type: 'pad' | 'via' | 'junction';
  x: number;
  y: number;
  layer?: number;
  pad_number?: string;
  ref_des?: string;
  device_name?: string;
  voltage?: number;
  width?: number;
  height?: number;
}

export interface Kipida_Resistance {
  id: string;
  start_node: string;
  end_node: string;
  net: string;
  length: number;
  width: number;
  thickness: number;
  layer: number;
  resistance: number;
}

export interface Kipida_Connection {
  from_node: string;
  to: string;
  type: 'track' | 'via';
  net: string;
  resistance_id?: string;
}

export interface Kipida_Metadata {
  total_nets: number;
  total_tracks: number;
  total_vias: number;
  total_pads: number;
  extracted_at: string;
}

export interface Kipida_Source {
  node_id: string;
  voltage: number;
}

export interface Kipida_Load {
  node_id: string;
  current: number;
}

export interface Kipida_CopperPour {
  net: string;
  layer: number;
  vertices: Array<{ x: number; y: number }>;
}

export interface Kipida_PcbData {
  nodes: Kipida_Node[];
  resistances: Kipida_Resistance[];
  connections: Kipida_Connection[];
  sources: Kipida_Source[];
  loads: Kipida_Load[];
  copper_pours?: Kipida_CopperPour[];
  /** 各信号层的 Gerber（base64），服务端解析出铺铜几何后回填 copper_pours */
  gerber_layers?: GerberLayer[];
  /** Pour 轻量信息（net + layer + bbox），服务端用于和 Gerber 区域做 IoU 匹配回填 net */
  pour_infos?: PourInfo[];
  mesh_resolution?: number;
  max_drop_pct?: number;
  metadata?: Kipida_Metadata;
}

// ============================================================
// KiPIDA 分析结果类型
// ============================================================

export interface Kipida_NetResult {
  net: string;
  max_drop: number;
  avg_current: number;
  min_voltage: number;
  max_voltage: number;
}

export interface Kipida_NetPlotImages {
  view_3d?: string;
  layers: Record<string, string>;
}

export interface Kipida_AnalysisResults {
  max_drop: number;
  avg_current: number;
  net_results: Kipida_NetResult[];
  plot_images: Record<string, Kipida_NetPlotImages>;
}

export interface Kipida_AnalysisResult {
  success: boolean;
  message?: string;
  results?: Kipida_AnalysisResults;
}
