import { GerberLayer } from './types';

/**
 * gerber.ts - 逐信号层导出 Gerber 制版文件
 *
 * 用途：铺铜（覆铜）几何由 API 提取不可靠，改为从 Gerber 区域填充（G36/G37）解析。
 * 这里只负责把每个信号层单独导出成 Gerber 文件并转为 base64，实际解析在 Python 服务端做。
 *
 * 关键约束：
 * - 每个信号层单独调用一次 getGerberFile，这样文件与 layerId 一一对应，
 *   服务端无需靠文件名猜测层号。
 * - objects 只导 'CopperFilled' / 'SolidRegion'，得到的就是纯铺铜区域，
 *   不含走线/焊盘，便于服务端把每个 region 当作一块 pour。
 * - getGerberFile 标记为 @beta，导出失败时返回空数组，分析流程仍可继续
 *   （只是缺少铺铜几何）。
 */

// ESYS_Unit.MILLIMETER 的运行时值为 "mm"。直接用字符串避免依赖运行时枚举对象。
const UNIT_MM = 'mm' as never;

async function fileToBase64(file: File): Promise<string> {
  // 优先 readAsDataURL，沙箱内最稳；result 形如 "data:...;base64,XXXX"
  const dataUrl: string = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
  const comma = dataUrl.indexOf(',');
  return comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl;
}

/**
 * 逐层导出 Gerber 并转 base64。
 * @param signalLayerIds 信号铜箔层 id 列表（来自 extract 的 SIGNAL/TOP/BOTTOM 层）
 */
export async function exportGerberLayers(signalLayerIds: number[]): Promise<GerberLayer[]> {
  const out: GerberLayer[] = [];
  const mfg = (eda as any)?.pcb_ManufactureData;
  if (!mfg || typeof mfg.getGerberFile !== 'function') {
    console.warn('[Gerber] pcb_ManufactureData.getGerberFile 不可用，跳过 Gerber 导出');
    return out;
  }

  for (const layerId of signalLayerIds) {
    try {
      const file: File | undefined = await mfg.getGerberFile(
        `kipida_layer_${layerId}`,
        false,                              // colorSilkscreen
        UNIT_MM,                            // 单位 mm
        { integerNumber: 4, decimalNumber: 4 },
        undefined,                          // 不要钻孔/飞针等附加文件
        [{ layerId, isMirror: false }],     // 只导这一层
        ['CopperFilled', 'SolidRegion', 'Track']     // 铺铜区域 + 走线
      );

      if (!file) {
        console.warn(`[Gerber] 层 ${layerId} 导出返回空`);
        continue;
      }

      const data = await fileToBase64(file);
      if (!data) {
        console.warn(`[Gerber] 层 ${layerId} base64 为空`);
        continue;
      }

      out.push({ layer: layerId, data, filename: (file as any).name });
      console.log(`[Gerber] 层 ${layerId} 导出成功: ${(file as any).name || ''} (${data.length} b64 chars)`);
    } catch (e) {
      console.warn(`[Gerber] 层 ${layerId} 导出失败:`, e);
    }
  }

  console.log(`[Gerber] 共导出 ${out.length}/${signalLayerIds.length} 层`);
  return out;
}
