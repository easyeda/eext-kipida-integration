import { Kipida_AnalysisResult } from './types';

export class ResultDisplay {
  show(result: Kipida_AnalysisResult, layerNames?: Record<number, string>): void {
    if (!result.success) {
      eda.sys_Dialog.showInformationMessage(result.message || '分析失败', '错误');
      return;
    }

    const task = eda.sys_MessageBus.subscribe('kipida-results-ready', () => {
      task.cancel();
      eda.sys_MessageBus.publish('kipida-results-data', { result, layerNames: layerNames || {} });
    });

    eda.sys_IFrame.openIFrame('/ui/results.html', 860, 600, 'kipida-results', {
      maximizeButton: true,
      buttonCallbackFn: (btn) => {
        if (btn === 'close') task.cancel();
      },
    }).catch(() => task.cancel());
  }
}
