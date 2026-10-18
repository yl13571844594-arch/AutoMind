// ⭐ 升级 / 许可证（v1.11.1）：社区版 → 专业版 / 企业版，全程在应用里完成。
//
// 此前升级要四步、全在应用外：pip 装 wheel → 设环境变量或手写许可证文件 → 重启 →
// 看徽标变没变；许可证填错没有任何反馈，只是"重启后还是社区版"。现在：
//   ① 上传扩展包（.whl，免 pip，桌面版同样可用；已装则跳过）
//   ② 粘贴许可证 → 当场校验，无效说清原因
//   ③ 校验通过就地激活，🔒 立即消失（新功能的接口由服务端热挂载，不必重启）
import { App, Input, Modal, Steps, Tag, Upload } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { apiGet, apiPost, apiUpload, errText } from '../../api/client';
import { EDITION_LABELS, useApp } from '../../store/app';
import { useUi } from '../../store/ui';

interface UpgradeStatus {
  edition: string; label: string;
  license: null | { tier: string; customer: string; expiry: string | null; seats?: number; issued?: string };
  package: { installed: boolean; version: string; compatible: boolean; api_version: number | null; error?: string };
  step: 'active' | 'need_package' | 'incompatible' | 'need_license';
  reason_text: string;
  license_file: string;
  extensions_dir: string;
  features: Record<string, boolean>;
  feature_labels: Record<string, { tier: string; label: string }>;
}

const fmtExpiry = (e: string | null | undefined) =>
  !e ? '永久' : /^\d{8}$/.test(e) ? `${e.slice(0, 4)}-${e.slice(4, 6)}-${e.slice(6)}` : e;

export default function LicenseModal() {
  const { message } = App.useApp();
  const open = useUi((s) => s.modal) === 'license';
  const close = useUi((s) => s.closeModal);
  const [st, setSt] = useState<UpgradeStatus | null>(null);
  const [loadErr, setLoadErr] = useState('');
  const [lic, setLic] = useState('');
  const [busy, setBusy] = useState<'' | 'pkg' | 'lic'>('');
  const [err, setErr] = useState('');
  const [done, setDone] = useState<null | { label: string; added: string[]; restart: boolean }>(null);

  const load = async () => {
    setLoadErr('');
    try { setSt(await apiGet('/license')); } catch (e) { setLoadErr(errText(e)); }
  };
  useEffect(() => {
    if (!open) return;
    setErr(''); setDone(null); setLic('');
    load();
  }, [open]);

  const afterActivate = async (label: string, added: string[], restart: boolean) => {
    setDone({ label, added, restart });
    await useApp.getState().loadStatus();
    await load();
  };

  const uploadPkg = async (file: File) => {
    setBusy('pkg'); setErr('');
    try {
      const r = await apiUpload('/license/package', file);
      if (r.bundle && r.edition && r.edition !== 'community' && !r.restart_required) {
        // v1.11.7 一键升级包：装包 + 激活一次完成
        await afterActivate(r.label || EDITION_LABELS[r.edition] || r.edition, r.added || [], false);
        return;
      }
      if (r.bundle && r.license_saved) {
        message.warning('升级包已安装，许可证已保存：重启 AutoMind 后生效', 8);
      } else if (r.restart_required) {
        message.warning('扩展包已更新，重启 AutoMind 后生效', 6);
      } else if (r.edition && r.edition !== 'community') {
        // 本机早就配过许可证：装好包当场就激活了
        await afterActivate(EDITION_LABELS[r.edition] || r.edition, r.added || [], false);
        return;
      } else {
        message.success(`扩展包已安装（v${r.package?.version || '?'}），下一步：激活许可证`);
      }
      await load();
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy('');
    }
  };

  const activate = async (raw?: string) => {
    const text = (raw ?? lic).trim();
    if (!text) { setErr('请粘贴许可证内容'); return; }
    setBusy('lic'); setErr('');
    try {
      const r = await apiPost('/license/activate', { license: text });
      await afterActivate(r.label || r.edition, r.added || [], !!r.restart_required);
    } catch (e) {
      setErr(errText(e));
    } finally {
      setBusy('');
    }
  };

  const stepIdx = !st ? 0 : st.step === 'active' ? 2 : st.step === 'need_license' ? 1 : 0;
  const groups = useMemo(() => {
    if (!st) return [];
    const by: Record<string, [string, string, boolean][]> = { pro: [], enterprise: [] };
    for (const [k, v] of Object.entries(st.feature_labels)) {
      (by[v.tier] ||= []).push([k, v.label, !!st.features[k]]);
    }
    return [['pro', '专业版', by.pro], ['enterprise', '企业版（含专业版全部）', by.enterprise]] as const;
  }, [st]);

  return (
    <Modal open={open} onCancel={close} footer={null} width={640} destroyOnClose
      title={<span>⭐ 升级与许可证</span>}>
      {loadErr && (
        <div className="lic-err">⚠️ 读取版本信息失败：{loadErr} <a onClick={load}>重试</a></div>
      )}
      {st && (
        <>
          <div className={'lic-head ' + st.edition}>
            <div>
              <div className="lic-cur">当前版本</div>
              <div className="lic-ed">{st.label}</div>
            </div>
            {st.license ? (
              <div className="lic-meta">
                <div>授权给 <b>{st.license.customer || '—'}</b></div>
                <div>有效期至 <b>{fmtExpiry(st.license.expiry)}</b>
                  {st.license.seats ? <> · {st.license.seats} 席位</> : null}</div>
              </div>
            ) : (
              <div className="lic-meta hint">本地运行 · 社区版功能完整可用<br />商业功能需扩展包 + 许可证</div>
            )}
          </div>

          {done ? (
            <div className="lic-done">
              <div className="lic-done-ico">🎉</div>
              <b>已升级为{done.label}</b>
              {done.added.length > 0 && (
                <p>新解锁 {done.added.length} 项功能：{done.added
                  .map((k) => st.feature_labels[k]?.label || k).slice(0, 6).join('、')}
                  {done.added.length > 6 ? ' 等' : ''}</p>
              )}
              {done.restart
                ? <p className="lic-warn">部分功能需要重启 AutoMind 后才能完全生效。</p>
                : <p className="hint-text">已即时生效，无需重启。</p>}
              <button className="lic-btn primary" onClick={close}>开始使用</button>
            </div>
          ) : st.step !== 'active' ? (
            <>
              <div className="lic-card" style={{ marginTop: 14 }}>
                <b>⚡ 一键升级（推荐）</b>
                <p className="hint-text">把发行方发给你的 <code>.ampkg</code> 升级包拖到这里：自动安装扩展包并激活许可证，一步完成、无需重启。</p>
                <Upload.Dragger accept=".ampkg,.zip" multiple={false} showUploadList={false}
                  disabled={busy === 'pkg'} beforeUpload={(f) => { uploadPkg(f); return false; }}>
                  <div className="lic-drop">{busy === 'pkg' ? '正在升级…' : <>🚀 点击或拖入 <code>.ampkg</code> 升级包</>}</div>
                </Upload.Dragger>
              </div>
              <div className="hint-text" style={{ margin: '12px 0 0' }}>没有升级包？也可以分两步：先装扩展包，再粘贴许可证。</div>
              <Steps size="small" current={stepIdx} style={{ margin: '12px 0 14px' }} items={[
                { title: '安装扩展包' }, { title: '激活许可证' }, { title: '完成' },
              ]} />

              {(st.step === 'need_package' || st.step === 'incompatible') && (
                <div className="lic-card">
                  <b>① 安装商业扩展包 automind-pro</b>
                  <p className="hint-text">
                    {st.step === 'incompatible'
                      ? `已安装的扩展包（v${st.package.version}）与当前版本不配套，请上传配套的新版扩展包。`
                      : st.package.error || '从商业渠道获取的 automind_pro-x.y.z-py3-none-any.whl，直接拖到下面即可，无需命令行。'}
                  </p>
                  <Upload.Dragger accept=".whl,.zip" multiple={false} showUploadList={false}
                    disabled={busy === 'pkg'}
                    beforeUpload={(f) => { uploadPkg(f); return false; }}>
                    <div className="lic-drop">
                      {busy === 'pkg' ? '正在安装…' : <>📦 点击或拖入 <code>.whl</code> 扩展包</>}
                    </div>
                  </Upload.Dragger>
                  <details className="lic-alt">
                    <summary>也可以用命令行安装</summary>
                    <pre>pip install automind_pro-*.whl</pre>
                    安装后刷新本窗口即可继续。
                  </details>
                </div>
              )}

              {st.step === 'need_license' && (
                <div className="lic-card">
                  <b>② 激活许可证</b>
                  <p className="hint-text">
                    扩展包已就绪（v{st.package.version}）。{st.reason_text && `状态：${st.reason_text}。`}
                    粘贴购买后收到的许可证（一整行，以 AMP 开头）：
                  </p>
                  <Input.TextArea rows={3} value={lic} spellCheck={false} className="lic-input"
                    placeholder="AMP2-…" onChange={(e) => { setLic(e.target.value); setErr(''); }}
                    onPressEnter={(e) => { if (!e.shiftKey) { e.preventDefault(); activate(); } }} />
                  <div className="lic-actions">
                    <Upload accept=".txt,.lic,.license,*" showUploadList={false}
                      beforeUpload={async (f) => {
                        const t = (await new Response(f).text()).trim();   // Blob.text() 需 Chromium 76+
                        setLic(t); activate(t);
                        return false;
                      }}>
                      <button className="lic-btn">📄 从文件导入</button>
                    </Upload>
                    <span style={{ flex: 1 }} />
                    <button className="lic-btn primary" disabled={busy === 'lic' || !lic.trim()}
                      onClick={() => activate()}>
                      {busy === 'lic' ? '正在校验…' : '校验并激活'}
                    </button>
                  </div>
                  <p className="hint-text" style={{ marginTop: 8 }}>
                    许可证离线校验，不联网、不上传；保存在 <code>{st.license_file}</code>
                  </p>
                </div>
              )}
            </>
          ) : (
            <div className="lic-card" style={{ marginTop: 14 }}>
              <b>🔁 续期 / 升级到更高版本</b>
              <p className="hint-text">收到新的 <code>.ampkg</code> 升级包（续期、专业版升企业版、扩展包新版本）时，直接拖到这里即可。</p>
              <Upload.Dragger accept=".ampkg,.zip,.whl" multiple={false} showUploadList={false}
                disabled={busy === 'pkg'} beforeUpload={(f) => { uploadPkg(f); return false; }}>
                <div className="lic-drop">{busy === 'pkg' ? '正在升级…' : <>📦 点击或拖入升级包</>}</div>
              </Upload.Dragger>
            </div>
          )}

          {err && <div className="lic-err">⚠️ {err}</div>}

          <div className="lic-feats">
            {groups.map(([tier, title, items]) => (
              <div key={tier} className="lic-feat-col">
                <div className="lic-feat-title">{title}</div>
                {items.map(([k, label, on]) => (
                  <div key={k} className={'lic-feat' + (on ? ' on' : '')}>
                    <span>{on ? '✓' : '🔒'}</span>{label}
                  </div>
                ))}
              </div>
            ))}
          </div>
          {st.step === 'active' && !done && (
            <div className="lic-renew">
              <Tag color={st.edition === 'enterprise' ? 'purple' : 'blue'}>{st.label}已激活</Tag>
              <span className="hint-text">续期 / 升级到企业版：</span>
              <Input size="small" placeholder="粘贴新许可证" value={lic} style={{ flex: 1 }}
                onChange={(e) => { setLic(e.target.value); setErr(''); }} />
              <button className="lic-btn primary" disabled={busy === 'lic' || !lic.trim()}
                onClick={() => activate()}>{busy === 'lic' ? '校验中…' : '激活'}</button>
            </div>
          )}
        </>
      )}
    </Modal>
  );
}
