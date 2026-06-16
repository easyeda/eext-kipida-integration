import * as extensionConfig from '../extension.json';
import { Kipida_Source, Kipida_Load } from './types';
import { PcbExtractor } from './extract';
import { PcbDataConverter } from './convert';
import { exportGerberLayers } from './gerber';
import { KipidaApiClient } from './api';
import { ResultDisplay } from './display';

const CONFIG = {
  host: 'localhost',
  port: 5000,
  analyzeEndpoint: '/analyze',
  testEndpoint: '/test',
  plotsEndpoint: '/plots',
};

function getServiceAddress(): string {
  return `${CONFIG.host}:${CONFIG.port}`;
}

// ============================================================
// 配置面板通信
// ============================================================

interface ComponentInfo {
  ref_des: string;
  device_name?: string;
  node_ids: string[];
  pad_numbers: string[];
}

interface NetInfo {
  net: string;
  components: ComponentInfo[];
}

interface UserConfig {
  mesh_resolution: number;
  max_drop_pct: number;
  board_thickness: number;
  layer_cu_thickness: Record<string, number>;
  rails: Array<{
    net: string;
    voltage: number;
    sources: Array<{ ref_des: string; node_ids: string[] }>;
    loads: Array<{ ref_des: string; node_ids: string[]; current: number }>;
  }>;
}

const POWER_NET_PATTERN = /^(\+|-|VCC|VDD|VEE|VSS|GND|PWR|VBUS|VBAT|V\d|[0-9]+V|[A-Z]+_[0-9]+V[0-9]*|[A-Z]+[0-9]+V)/i;

function isPowerNet(netName: string): boolean {
  return POWER_NET_PATTERN.test(netName);
}

function buildNetInfos(nodes: ReturnType<PcbDataConverter['getNodes']>): { powerNets: NetInfo[]; allNetComponents: Record<string, ComponentInfo[]> } {
  // Group by net → ref_des
  const netMap = new Map<string, Map<string, ComponentInfo>>();
  for (const node of nodes) {
    if (node.type !== 'pad' || !node.ref_des) continue;
    if (!netMap.has(node.net)) netMap.set(node.net, new Map());
    const compMap = netMap.get(node.net)!;
    if (!compMap.has(node.ref_des)) {
      compMap.set(node.ref_des, { ref_des: node.ref_des, device_name: node.device_name, node_ids: [], pad_numbers: [] });
    }
    const comp = compMap.get(node.ref_des)!;
    comp.node_ids.push(node.id);
    comp.pad_numbers.push(node.pad_number || '?');
  }

  const allNetComponents: Record<string, ComponentInfo[]> = {};
  const powerNets: NetInfo[] = [];
  for (const [net, compMap] of netMap.entries()) {
    const components = Array.from(compMap.values()).sort((a, b) => a.ref_des.localeCompare(b.ref_des));
    allNetComponents[net] = components;
    if (isPowerNet(net)) {
      powerNets.push({ net, components });
    }
  }
  powerNets.sort((a, b) => a.net.localeCompare(b.net));
  return { powerNets, allNetComponents };
}

function userConfigToSourcesLoads(
  config: UserConfig
): { sources: Kipida_Source[]; loads: Kipida_Load[] } {
  const sources: Kipida_Source[] = [];
  const loads: Kipida_Load[] = [];

  for (const rail of config.rails) {
    for (const src of rail.sources) {
      for (const nodeId of src.node_ids) {
        sources.push({ node_id: nodeId, voltage: rail.voltage });
      }
    }
    for (const load of rail.loads) {
      for (const nodeId of load.node_ids) {
        loads.push({ node_id: nodeId, current: load.current });
      }
    }
  }

  return { sources, loads };
}

async function showServiceNotFoundDialog(api: KipidaApiClient): Promise<boolean> {
  return new Promise((resolve) => {
    let resolved = false;
    let task: any = null;

    const cleanup = () => {
      if (task) { task.cancel(); task = null; }
    };

    task = eda.sys_MessageBus.subscribe('kipida-service-dialog', async (msg: any) => {
      if (msg?.type === 'retry' && !resolved) {
        const ok = await api.checkServiceWithDiscovery();
        if (ok) {
          resolved = true;
          cleanup();
          eda.sys_IFrame.closeIFrame('kipida-service-guide');
          resolve(true);
        } else {
          eda.sys_Dialog.showInformationMessage('服务仍未检测到，请确认已启动', '检测失败');
        }
      }
    });

    eda.sys_IFrame.openIFrame('/ui/service-not-found.html', 480, 520, 'kipida-service-guide', {
      maximizeButton: false,
      minimizeButton: false,
      buttonCallbackFn: (btn: string) => {
        if (btn === 'close' && !resolved) {
          resolved = true;
          cleanup();
          resolve(false);
        }
      },
    }).catch(() => {
      cleanup();
      resolve(false);
    });
  });
}

async function showConfigPanel(netInfos: NetInfo[], allNetNames: string[], allNetComponents: Record<string, ComponentInfo[]>, layerNames: Record<number, string>): Promise<UserConfig | null> {
  return new Promise((resolve) => {
    let resolved = false;
    let task: any = null;

    const cleanup = () => {
      if (task) { task.cancel(); task = null; }
    };

    task = eda.sys_MessageBus.subscribe('kipida-iframe', (msg: any) => {
      if (msg?.type === 'KIPIDA_READY') {
        eda.sys_MessageBus.publish('kipida-main', { type: 'KIPIDA_NET_DATA', nets: netInfos, allNetNames, allNetComponents, layerNames });
      } else if (msg?.type === 'KIPIDA_RUN' && !resolved) {
        resolved = true;
        cleanup();
        eda.sys_IFrame.closeIFrame('kipida-config');
        resolve(msg.config as UserConfig);
      } else if (msg?.type === 'KIPIDA_CANCEL' && !resolved) {
        resolved = true;
        cleanup();
        eda.sys_IFrame.closeIFrame('kipida-config');
        resolve(null);
      }
    });

    eda.sys_IFrame.openIFrame('/ui/config.html', 1000, 600, 'kipida-config', {
      maximizeButton: false,
      minimizeButton: true,
      buttonCallbackFn: (btn) => {
        if (btn === 'close' && !resolved) {
          resolved = true;
          cleanup();
          resolve(null);
        }
      },
    }).catch(() => {
      cleanup();
      resolve(null);
    });
  });
}

// ============================================================
// 导出函数
// ============================================================

export async function runIRDropAnalysis(): Promise<void> {
  try {
    console.log('[KiPIDA] 开始 PDN 分析...');
    eda.sys_LoadingAndProgressBar.showProgressBar(5, 'pdn-analysis');

    // Step 1: 提取 PCB 数据
    const tExtract0 = Date.now();
    const extractor = new PcbExtractor();
    const easyedaData = await extractor.extractAll();
    const tExtract1 = Date.now();
    console.log(`[KiPIDA] PCB 数据提取耗时: ${tExtract1 - tExtract0}ms`);

    if (!easyedaData || (easyedaData.tracks.length === 0 && easyedaData.vias.length === 0 && easyedaData.pads.length === 0)) {
      eda.sys_Dialog.showInformationMessage('未找到 PCB 数据，请确保打开了 PCB 文件', '警告');
      eda.sys_LoadingAndProgressBar.showProgressBar(100, 'pdn-analysis');
      return;
    }

    eda.sys_LoadingAndProgressBar.showProgressBar(20, 'pdn-analysis');

    // Step 2: 转换数据（先不生成 sources/loads）
    const tConvert0 = Date.now();
    const converter = new PcbDataConverter();
    const kipidaData = converter.convert(easyedaData);
    const tConvert1 = Date.now();
    console.log(`[KiPIDA] 数据转换耗时: ${tConvert1 - tConvert0}ms`);
    console.log('[KiPIDA] 提取完成:', easyedaData);
    eda.sys_LoadingAndProgressBar.showProgressBar(35, 'pdn-analysis');

    // Step 3: 检测服务是否运行（在配置面板之前）
    const api = new KipidaApiClient(CONFIG.host, CONFIG.port, {
      analyzeEndpoint: CONFIG.analyzeEndpoint,
      testEndpoint: CONFIG.testEndpoint,
      plotsEndpoint: CONFIG.plotsEndpoint,
    });

    const isRunning = await api.checkService();
    if (!isRunning) {
      eda.sys_LoadingAndProgressBar.showProgressBar(100, 'pdn-analysis');
      const retrySuccess = await showServiceNotFoundDialog(api);
      if (!retrySuccess) return;
    }

    // Step 4: 打开配置面板
    const { powerNets: netInfos, allNetComponents } = buildNetInfos(kipidaData.nodes);
    const allNetNames = await eda.pcb_Net.getAllNetsName();
    eda.sys_LoadingAndProgressBar.showProgressBar(100, 'pdn-analysis');

    const userConfig = await showConfigPanel(netInfos, allNetNames, allNetComponents, easyedaData.layerNames || {});
    if (!userConfig) {
      console.log('[KiPIDA] 用户取消配置');
      return;
    }

    eda.sys_LoadingAndProgressBar.showProgressBar(10, 'pdn-analysis');

    // Step 5: 用用户配置替换 sources/loads
    const { sources, loads } = userConfigToSourcesLoads(userConfig);
    kipidaData.sources = sources;
    kipidaData.loads = loads;
    kipidaData.mesh_resolution = userConfig.mesh_resolution;
    kipidaData.max_drop_pct = userConfig.max_drop_pct;
    (kipidaData as any).board_thickness = userConfig.board_thickness;
    (kipidaData as any).layer_cu_thickness = userConfig.layer_cu_thickness;

    console.log('[KiPIDA] 用户配置:', userConfig);
    eda.sys_LoadingAndProgressBar.showProgressBar(30, 'pdn-analysis');

    // Step 5.5: 逐信号层导出 Gerber，铺铜几何由服务端从中解析
    try {
      const tGerber0 = Date.now();
      const gerberLayers = await exportGerberLayers(easyedaData.signalLayerIds || []);
      const tGerber1 = Date.now();
      console.log(`[KiPIDA] Gerber 导出耗时: ${tGerber1 - tGerber0}ms (${gerberLayers.length} 层)`);
      kipidaData.gerber_layers = gerberLayers;
      kipidaData.pour_infos = easyedaData.pourInfos || [];
    } catch (e) {
      console.warn('[KiPIDA] Gerber 导出失败，将无铺铜几何继续分析:', e);
      kipidaData.gerber_layers = [];
    }
    eda.sys_LoadingAndProgressBar.showProgressBar(50, 'pdn-analysis');

    // Step 6: 调用分析服务
    const tApi0 = Date.now();
    const result = await api.analyze(kipidaData);
    const tApi1 = Date.now();
    console.log(`[KiPIDA] API 分析耗时: ${tApi1 - tApi0}ms`);
    eda.sys_LoadingAndProgressBar.showProgressBar(90, 'pdn-analysis');

    // Step 7: 展示结果
    const tDisplay0 = Date.now();
    const display = new ResultDisplay();
    display.show(result, easyedaData.layerNames);
    const tDisplay1 = Date.now();
    console.log(`[KiPIDA] 结果展示耗时: ${tDisplay1 - tDisplay0}ms`);
    eda.sys_LoadingAndProgressBar.showProgressBar(100, 'pdn-analysis');

    console.log(`[KiPIDA] 分析完成, 总耗时: ${tDisplay1 - tExtract0}ms`);
  } catch (error) {
    console.error('[KiPIDA] 分析失败:', error);
    eda.sys_Dialog.showInformationMessage(`分析失败: ${error}`, '错误');
    eda.sys_LoadingAndProgressBar.showProgressBar(100, 'pdn-analysis');
  }
}

export async function configureService(): Promise<void> {
  const current = getServiceAddress();
  const input = await eda.sys_Dialog.showInputDialog(
    '请输入 KiPIDA 服务地址（格式: host:port）',
    '',
    '配置 KiPIDA 服务',
    'text',
    current
  );

  if (input === undefined || input === null) return;

  const parts = (input as string).trim().split(':');
  if (parts.length !== 2 || !parts[1] || isNaN(Number(parts[1]))) {
    eda.sys_Dialog.showInformationMessage('地址格式无效，请使用 host:port 格式', '错误');
    return;
  }

  CONFIG.host = parts[0];
  CONFIG.port = Number(parts[1]);
  eda.sys_Dialog.showInformationMessage(`服务地址已更新为: ${getServiceAddress()}`, '配置成功');
}

export function about(): void {
  const content = `KiPIDA 桥接插件 v${extensionConfig.version}

用于桥接 EasyEDA 与 KiPIDA PDN IR Drop 分析工具

功能：
• 从 EasyEDA 提取 PCB 数据
• 配置电压源与电流负载
• 调用本地 Python 服务进行分析
• 展示 IR Drop 分析结果`;
  eda.sys_Dialog.showInformationMessage(content, '关于');
}
