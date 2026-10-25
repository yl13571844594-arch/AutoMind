// v1.11.15 输入框模型选择器状态（zustand）。
//
// 为什么单独一个 store 而不是塞进 useApp：它是"这台浏览器上这个人用哪个模型"
// 的**常驻偏好**（localStorage），与 Agent 运行状态无关；而且模型清单要能从
// 「模型配置」弹窗、许可证激活等外部变化后刷新，独立 store 更好触发。
import { create } from 'zustand';
import {
  fetchAvailableModels, isValidAvailable, loadChoice, saveChoice,
  type AvailableModels, type ModelChoice,
} from '../lib/modelChoice';

/** 清单缓存时长：短时间内反复打开/关闭下拉不重复打服务端。 */
const REFRESH_MS = 15_000;

interface ModelChoiceState {
  choice: ModelChoice;
  available: AvailableModels | null;
  loading: boolean;
  /** 加载失败原因（非空时选择器显示可点击的重试，而不是一个空菜单） */
  error: string;
  loadedAt: number;
  load: (force?: boolean) => Promise<void>;
  setChoice: (c: ModelChoice) => void;
}

export const useModelChoice = create<ModelChoiceState>((set, get) => ({
  choice: loadChoice(),
  available: null,
  loading: false,
  error: '',
  loadedAt: 0,

  load: async (force = false) => {
    const st = get();
    if (st.loading) return;
    if (!force && st.available && Date.now() - st.loadedAt < REFRESH_MS) return;
    set({ loading: true, error: '' });
    try {
      const d = await fetchAvailableModels();
      if (!isValidAvailable(d)) {
        throw new Error('服务端返回的模型清单格式不正确（可能是前后端版本不一致，请刷新页面）');
      }
      set({ available: d, loading: false, error: '', loadedAt: Date.now() });
    } catch (e: any) {
      // 失败不覆盖已有清单（断网时旧的仍可用），但必须把原因交给 UI 展示
      set({ loading: false, error: e?.friendly || e?.message || String(e) });
    }
  },

  setChoice: (c) => {
    saveChoice(c);
    set({ choice: { ...c } });
  },
}));
