// UI 弹窗状态：设置（模型/API Keys/通用/集成）、工作区、模板、引导、HTML 预览。
import { create } from 'zustand';

export type ModalName =
  | 'model' | 'apikeys' | 'general' | 'integrations'
  | 'workspaces' | 'templates' | 'tour' | 'update' | null;

interface UiState {
  modal: ModalName;
  preview: { html?: string; url?: string; label: string } | null;
  openModal: (m: ModalName) => void;
  closeModal: () => void;
  openPreview: (p: { html?: string; url?: string; label: string }) => void;
  closePreview: () => void;
}

export const useUi = create<UiState>((set) => ({
  modal: null,
  preview: null,
  openModal: (m) => set({ modal: m }),
  closeModal: () => set({ modal: null }),
  openPreview: (p) => set({ preview: p }),
  closePreview: () => set({ preview: null }),
}));
