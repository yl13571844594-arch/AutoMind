// 保证"点了就能确认"的确认气泡（v1.11.8）。
//
// antd Popconfirm 也是渲染到 body 末尾的弹层，部分电脑上同样会"弹了却看不见"——
// 表现是点「删除」毫无反应。兼容路径用浏览器原生 confirm()：系统对话框，与页面 CSS 无关。
// 与 SafeDropdown / SafeSelect 共用开关（?menu=simple / ?menu=antd）。
import { Popconfirm, type PopconfirmProps } from 'antd';
import { cloneElement, useEffect, useRef, useState, type ReactElement } from 'react';
import { fallbackForced, markFallback, onFallback, probeAfterOpen, probeClass } from '../../lib/popupGuard';

type Props = PopconfirmProps & { children: ReactElement };

export default function SafePopconfirm(props: Props) {
  const [native, setNative] = useState(fallbackForced);
  const probeCls = useRef(probeClass('sp')).current;
  const isOpen = useRef(false);
  useEffect(() => onFallback(() => setNative(true)), []);
  const { children, title, onConfirm, onCancel, disabled, overlayClassName, onOpenChange } = props;

  if (native) {
    return cloneElement(children, {
      onClick: (e: React.MouseEvent<HTMLElement>) => {
        children.props.onClick?.(e);
        if (disabled) return;
        const msg = typeof title === 'string' ? title : '确定执行该操作？';
        if (window.confirm(msg)) onConfirm?.(e);
        else onCancel?.(e);
      },
    });
  }
  return (
    <Popconfirm {...props} overlayClassName={`${overlayClassName || ''} ${probeCls}`}
      onOpenChange={(open, e) => {
        isOpen.current = open;
        onOpenChange?.(open, e);
        if (open) {
          probeAfterOpen(probeCls, () => isOpen.current, () => {
            isOpen.current = false;
            markFallback('确认气泡');
            setNative(true);
            // 用户刚才那一下点击没得到任何反馈：直接用原生确认框补问一次
            const msg = typeof title === 'string' ? title : '确定执行该操作？';
            if (window.confirm(msg)) onConfirm?.();
          });
        }
      }}>
      {children}
    </Popconfirm>
  );
}
