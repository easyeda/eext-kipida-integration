import { Kipida_PcbData, Kipida_AnalysisResult } from './types';

/**
 * api.ts - HTTP 通信模块
 * 负责与 KiPIDA Python 服务通信
 */

export interface ApiConfig {
  analyzeEndpoint: string;
  testEndpoint: string;
  plotsEndpoint: string;
}

export class KipidaApiClient {
  private host: string;
  private port: number;
  private config: ApiConfig;

  constructor(host: string, port: number, config: ApiConfig) {
    this.host = host;
    this.port = port;
    this.config = config;
  }

  /**
   * 自动扫描端口范围，找到运行中的服务
   */
  async discoverPort(startPort: number = 5000, endPort: number = 5099): Promise<boolean> {
    for (let p = startPort; p <= endPort; p++) {
      try {
        const url = `http://${this.host}:${p}${this.config.testEndpoint}`;
        const response = await eda.sys_ClientUrl.request(url);
        if (response.ok) {
          this.port = p;
          console.log(`[KipidaApiClient] 发现服务运行在端口 ${p}`);
          return true;
        }
      } catch {}
    }
    return false;
  }

  /**
   * 快速检测当前端口是否有服务运行（不扫描端口范围）
   */
  async checkService(): Promise<boolean> {
    try {
      const url = `http://${this.host}:${this.port}${this.config.testEndpoint}`;
      const response = await eda.sys_ClientUrl.request(url);
      if (response.ok) return true;
    } catch {}
    return false;
  }

  /**
   * 完整检测：先试当前端口，失败则扫描端口范围
   */
  async checkServiceWithDiscovery(): Promise<boolean> {
    if (await this.checkService()) return true;
    return this.discoverPort();
  }

  /**
   * 发送分析请求
   */
  async analyze(data: Kipida_PcbData, skipPlots: boolean = false): Promise<Kipida_AnalysisResult> {
    try {
      const params = skipPlots ? '?skip_plots=true' : '';
      const url = `http://${this.host}:${this.port}${this.config.analyzeEndpoint}${params}`;

      console.log('[KipidaApiClient] 发送请求到:', url);
      console.log('[KipidaApiClient] 请求数据:', JSON.stringify(data));

      const response = await eda.sys_ClientUrl.request(url, 'POST', JSON.stringify(data), {
        headers: { 'Content-Type': 'application/json' },
      });

      if (!response.ok) {
        const errorText = await response.text();
        console.error('[KipidaApiClient] HTTP 错误:', response.status, errorText);
        return {
          success: false,
          message: `HTTP 错误: ${response.status} - ${errorText}`,
        };
      }

      const result = await response.json();
      console.log('[KipidaApiClient] 响应数据:', result);

      return result as Kipida_AnalysisResult;
    } catch (error) {
      console.error('[KipidaApiClient] 请求失败:', error);
      return {
        success: false,
        message: `连接失败: ${error}`,
      };
    }
  }

  /**
   * 请求生成可视化图片（基于上次求解结果）
   */
  async fetchPlots(): Promise<Kipida_AnalysisResult> {
    try {
      const url = `http://${this.host}:${this.port}${this.config.plotsEndpoint}`;
      console.log('[KipidaApiClient] 请求图片生成:', url);

      const response = await eda.sys_ClientUrl.request(url, 'POST', '{}', {
        headers: { 'Content-Type': 'application/json' },
      });

      if (!response.ok) {
        const errorText = await response.text();
        return { success: false, message: `HTTP 错误: ${response.status} - ${errorText}` };
      }

      const result = await response.json();
      return result as Kipida_AnalysisResult;
    } catch (error) {
      return { success: false, message: `连接失败: ${error}` };
    }
  }

  /**
   * 获取服务 URL
   */
  getServiceUrl(): string {
    return `http://${this.host}:${this.port}`;
  }
}