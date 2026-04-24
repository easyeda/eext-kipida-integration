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

export interface EasyEDA_PcbData {
  tracks: EasyEDA_Track[];
  vias: EasyEDA_Via[];
  pads: EasyEDA_Pad[];
  layerNames?: Record<number, string>;
  outerLayerIds?: Set<number>;
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

export interface Kipida_PcbData {
  nodes: Kipida_Node[];
  resistances: Kipida_Resistance[];
  connections: Kipida_Connection[];
  sources: Kipida_Source[];
  loads: Kipida_Load[];
  mesh_resolution?: number;
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
