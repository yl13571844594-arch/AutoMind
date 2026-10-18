// 保证"点了就有菜单"的下拉（v1.11.4）。
//
// 背景：「＋」与「设置」用的是 antd Dropdown —— 它依赖 CSS-in-JS 生成的定位样式、
// 进出场动画（animationend 才算出场完成）与 rc-trigger 的对齐计算。旧内核里这三样
// 任何一环出问题，菜单都会"打开了却看不见"（落在屏幕外 / 停在 opacity:0）。
// v1.11.3 修了样式那一环，但用户的内核千差万别，必须有一条**不依赖 antd 的保底路径**：
//
//   · 旧内核（lib/compat 判定）直接用自绘的轻量菜单；
//   · 其余浏览器照常用 antd，但每次点击后 450ms 检查弹层是否真的可见（在视口内、
//     不透明）；不可见就当场换成自绘菜单，并记住（localStorage），以后直接用它。
//
// 自绘菜单只用最朴素的 CSS（fixed 定位、无动画、无 :where / 逻辑属性 / gap），
// 接受与 antd 相同的 items 结构（group / divider / children 子菜单 / disabled / danger），
// 子菜单以"就地展开"呈现 —— 比悬停飞出更稳，触屏也能用。
import { Dropdown, type MenuProps } from 'antd';
import { cloneElement, useEffect, useLayoutEffect, useRef, useState, type ReactElement } from 'react';
import { createPortal } from 'react-dom';
import { fallbackForced, markFallback, onFallback, popupVisible, probeClass } from '../../lib/popupGuard';

type Placement = 'topLeft' | 'bottomLeft' | 'bottomRight' | 'topRight';

interface Props {
  menu: { items?: MenuProps['items'] };
  children: ReactElement;
  placement?: Placement;
  /** antd 弹层的 className（也用来定位弹层做可见性自检） */
  overlayClassName?: string;
  onOpenChange?: (open: boolean) => void;
}

export default function SafeDropdown({ menu, children, placement = 'bottomLeft', overlayClassName, onOpenChange }: Props) {
  const [simple, setSimple] = useState(fallbackForced);
  const [open, setOpen] = useState(false);
  const antdOpen = useRef(false);
  const probeCls = useRef(probeClass('sd')).current;
  // 别处的下拉 / 选择框自检失败时，本组件也一起切到兼容菜单（同一个浏览器，同一种毛病）
  useEffect(() => onFallback(() => setSimple(true)), []);

  const setOpenBoth = (v: boolean) => { setOpen(v); onOpenChange?.(v); };

  if (simple) {
    const child = cloneElement(children, {
      onClick: (e: React.MouseEvent) => {
        children.props.onClick?.(e);
        setOpenBoth(!open);
      },
      className: `${children.props.className || ''}${open ? ' ant-dropdown-open' : ''}`,
      'aria-expanded': open,
      // 定位时按这个属性现查触发元素：ref 在模式切换的那一次渲染里可能还指着旧节点
      'data-sd-anchor': probeCls,
    });
    return (
      <>
        {child}
        {open && (
          <SimpleMenu items={menu.items || []} anchorId={probeCls} placement={placement}
            className={overlayClassName} onClose={() => setOpenBoth(false)} />
        )}
      </>
    );
  }

  // antd 路径 + 可见性自检
  const check = () => {
    if (antdOpen.current) return;       // 这次点击是在关闭已打开的菜单
    window.setTimeout(() => {
      // 两种"点了没反应"都算：打开了却看不见；或者根本没打开（点击被吞掉）
      if (antdOpen.current && popupVisible(probeCls)) return;
      antdOpen.current = false;
      markFallback('下拉菜单');
      setSimple(true);
      setOpenBoth(true);
    }, 450);
  };
  // 不额外包一层元素（会破坏侧栏整宽按钮的布局）：检查挂在触发元素自己的捕获阶段
  const trigger = cloneElement(children, {
    onClickCapture: (e: React.MouseEvent) => { children.props.onClickCapture?.(e); check(); },
  });
  return (
    <Dropdown menu={menu} trigger={['click']} placement={placement}
      overlayClassName={`${overlayClassName || ''} ${probeCls}`}
      onOpenChange={(o) => { antdOpen.current = o; onOpenChange?.(o); }}>
      {trigger}
    </Dropdown>
  );
}

// ═══════════════════════════════════════════════════════════
// 自绘菜单
// ═══════════════════════════════════════════════════════════

type Item = NonNullable<MenuProps['items']>[number] & Record<string, any>;

function SimpleMenu({ items, anchorId, placement, className, onClose }: {
  items: MenuProps['items']; anchorId: string; placement: Placement;
  className?: string; onClose: () => void;
}) {
  const anchor = document.querySelector<HTMLElement>(`[data-sd-anchor="${anchorId}"]`);
  const box = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<React.CSSProperties>({ visibility: 'hidden' });
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [tick, setTick] = useState(0);          // 窗口尺寸变了就重新定位（而不是关掉）

  useLayoutEffect(() => {
    const el = document.querySelector<HTMLElement>(`[data-sd-anchor="${anchorId}"]`);
    const r = el?.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    if (!r || (!r.width && !r.height)) { setPos({ left: 12, bottom: 12, maxHeight: vh - 24 }); return; }
    const up = placement.startsWith('top');
    const right = placement.endsWith('Right');
    const st: React.CSSProperties = {};
    if (up) { st.bottom = Math.max(8, vh - r.top + 6); st.maxHeight = Math.max(160, r.top - 14); }
    else { st.top = r.bottom + 6; st.maxHeight = Math.max(160, vh - r.bottom - 14); }
    // 按菜单实际宽度夹进视口：右对齐时触发点太靠左（如侧栏会话的「⋯」）会伸出屏幕左侧
    const w = box.current?.offsetWidth || 300;
    if (right && r.right - w >= 8) st.right = Math.max(8, vw - r.right);
    else st.left = Math.max(8, Math.min(right ? r.right - w : r.left, vw - w - 8));
    setPos(st);
  }, [anchorId, placement, tick]);

  useEffect(() => {
    const down = (e: MouseEvent | TouchEvent) => {
      const t = e.target as Node;
      if (box.current?.contains(t) || anchor?.contains(t)) return;
      onClose();
    };
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    const resize = () => setTick((t) => t + 1);
    document.addEventListener('mousedown', down, true);
    document.addEventListener('touchstart', down, true);
    document.addEventListener('keydown', key, true);
    window.addEventListener('resize', resize);
    return () => {
      document.removeEventListener('mousedown', down, true);
      document.removeEventListener('touchstart', down, true);
      document.removeEventListener('keydown', key, true);
      window.removeEventListener('resize', resize);
    };
  }, [anchor, onClose]);

  const fire = (it: Item, e: React.MouseEvent) => {
    if (it.disabled) return;
    it.onClick?.({ key: String(it.key), keyPath: [String(it.key)], domEvent: e, item: null as any });
    onClose();
  };

  const render = (list: MenuProps['items'], depth: number): React.ReactNode[] => (list || []).map((raw, i) => {
    const it = raw as Item;
    if (!it) return null;
    const key = String(it.key ?? `${depth}-${i}`);
    if (it.type === 'divider') return <div key={key} className="sm-divider" />;
    if (it.type === 'group') {
      return (
        <div key={key} className="sm-group">
          {it.label && <div className="sm-group-title">{it.label}</div>}
          {render(it.children, depth)}
        </div>
      );
    }
    if (it.children) {
      const on = !!expanded[key];
      return (
        <div key={key} className={'sm-sub' + (on ? ' open' : '')}>
          <button type="button" className={'sm-item' + (it.disabled ? ' disabled' : '')}
            style={{ paddingLeft: 10 + depth * 14 }} aria-expanded={on}
            onClick={(e) => {
              if (it.disabled) return;
              it.onTitleClick?.({ key, domEvent: e });
              setExpanded((x) => ({ ...x, [key]: !on }));
            }}>
            <span className="sm-label">{it.label}</span>
            <span className="sm-caret">{on ? '▾' : '▸'}</span>
          </button>
          {on && <div className="sm-children">{render(it.children, depth + 1)}</div>}
        </div>
      );
    }
    return (
      <button key={key} type="button" disabled={!!it.disabled}
        className={'sm-item' + (it.danger ? ' danger' : '') + (it.disabled ? ' disabled' : '')}
        style={{ paddingLeft: 10 + depth * 14 }} onClick={(e) => fire(it, e)}>
        <span className="sm-label">{it.label}</span>
        {it.extra && <span className="sm-extra">{it.extra}</span>}
      </button>
    );
  });

  return createPortal(
    // 菜单经 portal 渲染到 body，但 React 合成事件仍会冒泡回触发元素的祖先（例如会话行的
    // onClick = 切换会话）：在菜单根上截住，点菜单项不会顺带触发外层的点击
    <div ref={box} role="menu" className={'sm-pop ' + (className || '')} style={pos}
      onClick={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>
      {render(items, 0)}
    </div>,
    document.body,
  );
}
