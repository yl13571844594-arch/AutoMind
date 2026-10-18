// 保证"点了就能选"的下拉选择框（v1.11.8）。
//
// 用户反馈：部分电脑上「通用设置 → 审批超时处置 / 并发写冲突策略」点了不出下拉。
// 与 v1.11.4 的菜单问题同源 —— antd Select 的选项弹层同样渲染到 body 末尾、依赖 CSS-in-JS
// 定位与动画，任何一环出问题就"打开了却看不见"。
//
// 兼容路径用**浏览器原生 <select>**：它的选项列表由浏览器 / 操作系统绘制，与页面 CSS 无关，
// 在任何内核上都能弹出来。正常浏览器照常用 antd（外观一致），打开后自检可见性，
// 看不见就当场换成原生控件并记住（与 SafeDropdown 共用开关：?menu=simple / ?menu=antd）。
//
// 接口与 antd Select 的常用子集一致（value / onChange / options（含分组与 disabled）/
// placeholder / disabled / size / style / loading / notFoundContent / showSearch），
// 调用处只需把 import 换成本组件。
import { Select, type SelectProps } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { fallbackForced, markFallback, onFallback, probeAfterOpen, probeClass } from '../../lib/popupGuard';

type Opt = { value?: any; label?: React.ReactNode; disabled?: boolean; options?: Opt[] };

function flatten(options: Opt[] | undefined): Opt[] {
  const out: Opt[] = [];
  for (const o of options || []) {
    if (o.options) out.push(...flatten(o.options));
    else out.push(o);
  }
  return out;
}

function text(label: React.ReactNode, value: any): string {
  if (typeof label === 'string' || typeof label === 'number') return String(label);
  return String(value ?? '');
}

export default function SafeSelect<V = any>(props: SelectProps<V>) {
  const [native, setNative] = useState(fallbackForced);
  const probeCls = useRef(probeClass('ss')).current;
  const isOpen = useRef(false);
  useEffect(() => onFallback(() => setNative(true)), []);

  if (native) {
    const { value, onChange, options, placeholder, disabled, size, style, className, loading,
      notFoundContent } = props as any;
    const flat = flatten(options as Opt[]);
    const idx = flat.findIndex((o) => o.value === value);
    const fire = (i: number) => {
      const o = flat[i];
      if (o) onChange?.(o.value, o);
    };
    const renderOpts = (list: Opt[], base: { n: number }): React.ReactNode[] => list.map((o, k) => {
      if (o.options) {
        return (
          <optgroup key={`g${k}`} label={text(o.label, '')}>
            {renderOpts(o.options, base)}
          </optgroup>
        );
      }
      const i = base.n;
      base.n += 1;
      return <option key={i} value={i} disabled={o.disabled}>{text(o.label, o.value)}</option>;
    });
    return (
      <select
        className={`safe-select${size === 'small' ? ' sm' : ''} ${className || ''}`}
        style={style} disabled={disabled || loading} value={idx >= 0 ? String(idx) : ''}
        onChange={(e) => fire(Number(e.target.value))}>
        {idx < 0 && <option value="" disabled>{loading ? '正在读取…' : (placeholder as string) || '请选择'}</option>}
        {flat.length === 0 && !loading && (
          <option value="" disabled>{typeof notFoundContent === 'string' ? notFoundContent : '没有可选项'}</option>
        )}
        {renderOpts((options as Opt[]) || [], { n: 0 })}
      </select>
    );
  }

  const { popupClassName, onDropdownVisibleChange, onMouseDown, disabled, loading } = props as any;
  const fallback = () => {
    isOpen.current = false;
    markFallback('下拉选择框');
    setNative(true);
  };
  return (
    <Select<V>
      {...props}
      onMouseDown={(e: React.MouseEvent<HTMLDivElement>) => {
        onMouseDown?.(e);
        // 点击被吞掉、根本没打开（只在"本来是关着的"时判断，避免把"点一下关闭"误判）
        if (!isOpen.current && !disabled && !loading) {
          window.setTimeout(() => { if (!isOpen.current && !native) fallback(); }, 450);
        }
      }}
      popupClassName={`${popupClassName || ''} ${probeCls}`}
      onDropdownVisibleChange={(open: boolean) => {
        isOpen.current = open;
        onDropdownVisibleChange?.(open);
        if (open) probeAfterOpen(probeCls, () => isOpen.current, fallback);
      }}
    />
  );
}
