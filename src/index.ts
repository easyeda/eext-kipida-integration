import * as extensionConfig from '../extension.json';
import { Kipida_Source, Kipida_Load } from './types';
import { PcbExtractor } from './extract';
import { PcbDataConverter } from './convert';
import { KipidaApiClient } from './api';
import { ResultDisplay } from './display';

const CONFIG = {
  host: 'localhost',
  port: 5000,
  analyzeEndpoint: '/analyze',
  testEndpoint: '/test',
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

function buildNetInfos(nodes: ReturnType<PcbDataConverter['getNodes']>): NetInfo[] {
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

  const result: NetInfo[] = [];
  for (const [net, compMap] of netMap.entries()) {
    if (!isPowerNet(net)) continue;
    const components = Array.from(compMap.values()).sort((a, b) => a.ref_des.localeCompare(b.ref_des));
    result.push({ net, components });
  }
  result.sort((a, b) => a.net.localeCompare(b.net));
  return result;
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

async function showConfigPanel(netInfos: NetInfo[], allNetNames: string[]): Promise<UserConfig | null> {
  return new Promise((resolve) => {
    let resolved = false;
    let task: any = null;

    const cleanup = () => {
      if (task) { task.cancel(); task = null; }
    };

    task = eda.sys_MessageBus.subscribe('kipida-iframe', (msg: any) => {
      if (msg?.type === 'KIPIDA_READY') {
        eda.sys_MessageBus.publish('kipida-main', { type: 'KIPIDA_NET_DATA', nets: netInfos, allNetNames });
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

    eda.sys_IFrame.openIFrame('/ui/config.html', 520, 600, 'kipida-config', {
      maximizeButton: false,
      minimizeButton: false,
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
    const extractor = new PcbExtractor();
    const easyedaData = await extractor.extractAll();

    if (!easyedaData || (easyedaData.tracks.length === 0 && easyedaData.vias.length === 0 && easyedaData.pads.length === 0)) {
      eda.sys_Dialog.showInformationMessage('未找到 PCB 数据，请确保打开了 PCB 文件', '警告');
      eda.sys_LoadingAndProgressBar.showProgressBar(100, 'pdn-analysis');
      return;
    }

    eda.sys_LoadingAndProgressBar.showProgressBar(20, 'pdn-analysis');

    // Step 2: 转换数据（先不生成 sources/loads）
    const converter = new PcbDataConverter();
    const kipidaData = converter.convert(easyedaData);
    console.log('[KiPIDA] 提取完成:', easyedaData);
    eda.sys_LoadingAndProgressBar.showProgressBar(35, 'pdn-analysis');

    // Step 3: 打开配置面板
    const netInfos = buildNetInfos(kipidaData.nodes);
    const allNetNames = await eda.pcb_Net.getAllNetsName();
    eda.sys_LoadingAndProgressBar.showProgressBar(100, 'pdn-analysis');

    const userConfig = await showConfigPanel(netInfos, allNetNames);
    if (!userConfig) {
      console.log('[KiPIDA] 用户取消配置');
      return;
    }

    eda.sys_LoadingAndProgressBar.showProgressBar(10, 'pdn-analysis');

    // Step 4: 用用户配置替换 sources/loads
    const { sources, loads } = userConfigToSourcesLoads(userConfig);
    kipidaData.sources = sources;
    kipidaData.loads = loads;
    kipidaData.mesh_resolution = userConfig.mesh_resolution;

    console.log('[KiPIDA] 用户配置:', userConfig);
    eda.sys_LoadingAndProgressBar.showProgressBar(30, 'pdn-analysis');

    // Step 5: 调用分析服务
    const api = new KipidaApiClient(CONFIG.host, CONFIG.port, {
      analyzeEndpoint: CONFIG.analyzeEndpoint,
      testEndpoint: CONFIG.testEndpoint,
    });

    const isRunning = await api.checkService();
    if (!isRunning) {
      eda.sys_Dialog.showInformationMessage(
        `无法连接到 KiPIDA 服务 (${getServiceAddress()})\n请确保服务已启动`,
        '连接失败'
      );
      eda.sys_LoadingAndProgressBar.showProgressBar(100, 'pdn-analysis');
      return;
    }

    const result = await api.analyze(kipidaData);
    eda.sys_LoadingAndProgressBar.showProgressBar(90, 'pdn-analysis');

    // Step 6: 展示结果
    const display = new ResultDisplay();
    display.show(result, easyedaData.layerNames);
    eda.sys_LoadingAndProgressBar.showProgressBar(100, 'pdn-analysis');

    console.log('[KiPIDA] 分析完成');
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
