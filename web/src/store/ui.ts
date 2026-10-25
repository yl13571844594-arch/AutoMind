// UI 弹窗状态：设置（模型/API Keys/通用/集成）、工作区、模板、引导、HTML 预览、Markdown 文档预览。
import { create } from 'zustand';

export type ModalName =
  | 'model' | 'apikeys' | 'general' | 'integrations'
  | 'workspaces' | 'templates' | 'tour' | 'update' | 'shortcuts' | 'runWorkflow' | 'goals'
  | 'license' | 'schedule' | 'bots' | 'sessions' | null;

/** HTML 预览的状态（iframe 沙箱）：内联 html 或一个可加载的 url 二选一。 */
export interface HtmlPreview {
  html?: string;
  url?: string;
  label: string;
}

/**
 * Markdown 文档预览的状态（v1.11.14）。
 *
 * 只存路径与展示名：文档内容随时可能被 Agent 改掉，**点开时现读**才是最新的；
 * 读取统一走 `/api/preview/file?path=...`（对 .md 返回 text/plain 原文）。
 * nonce 用于同一路径再次打开时强制重新拉取（点两次同一交付文件要能看到最新内容）。
 */
export interface DocPreview {
  path: string;
  label?: string;
  nonce: number;
}

interface UiState {
  modal: ModalName;
  /** 「新建定时任务」弹窗的预填内容（从输入框带过来） */
  scheduleDraft: string;
  openSchedule: (draft?: string) => void;
  preview: HtmlPreview | null;
  openModal: (m: ModalName) => void;
  closeModal: () => void;
  openPreview: (p: HtmlPreview) => void;
  closePreview: () => void;
  /** v1.11.14：📄 Markdown 文档视图（对话交付卡片 / 摘要产物 / 代码标签共用） */
  doc: DocPreview | null;
  openDoc: (p: { path: string; label?: string }) => void;
  closeDoc: () => void;
}

export const useUi = create<UiState>((set) => ({
  modal: null,
  scheduleDraft: '',
  openSchedule: (draft = '') => set({ modal: 'schedule', scheduleDraft: draft }),
  preview: null,
  openModal: (m) => set({ modal: m }),
  closeModal: () => set({ modal: null }),
  openPreview: (p) => set({ preview: p }),
  closePreview: () => set({ preview: null }),
  doc: null,
  openDoc: (p) => set((st) => ({
    doc: { path: p.path, label: p.label, nonce: (st.doc?.nonce || 0) + 1 },
  })),
  closeDoc: () => set({ doc: null }),
}));
