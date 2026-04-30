import { Kipida_PcbData, Kipida_AnalysisResult } from './types';

/**
 * api.ts - HTTP 通信模块
 * 负责与 KiPIDA Python 服务通信
 */

export interface ApiConfig {
  analyzeEndpoint: string;
  testEndpoint: string;
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
   * 检测服务是否运行
   */
  async checkService(): Promise<boolean> {
    try {
      const url = `http://${this.host}:${this.port}${this.config.testEndpoint}`;
      const response = await eda.sys_ClientUrl.request(url);
      return response.ok;
    } catch {
      return false;
    }
  }

  /**
   * 发送分析请求
   */
  async analyze(data: Kipida_PcbData): Promise<Kipida_AnalysisResult> {
    try {
      const url = `http://${this.host}:${this.port}${this.config.analyzeEndpoint}`;

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
   * 获取服务 URL
   */
  getServiceUrl(): string {
    return `http://${this.host}:${this.port}`;
  }
}