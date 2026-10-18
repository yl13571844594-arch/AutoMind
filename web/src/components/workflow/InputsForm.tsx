// 按工作流的 inputs 声明自动生成入参表单（v1.8.0，R-108）。
//
// 值在这里就按声明的类型转换好（数字就是数字、数组就是数组）：服务端的
// check_inputs 只做"给全了没"，类型不对的值传进去会让模板渲染出意料之外的东西。
import { Input, InputNumber, Switch } from 'antd';

export interface InputSpec {
  name: string; type: string; required: boolean; default: any; description?: string;
}

export type InputDraft = Record<string, string | number | boolean | null>;

/** 声明 → 初始草稿（有默认值就填默认值）。 */
export function initialDraft(specs: Record<string, InputSpec>): InputDraft {
  const out: InputDraft = {};
  for (const [k, s] of Object.entries(specs || {})) {
    const d = s.default;
    if (s.type === 'boolean') out[k] = d === null || d === undefined ? false : !!d;
    else if (s.type === 'integer' || s.type === 'number') out[k] = typeof d === 'number' ? d : null;
    else if (s.type === 'object' || s.type === 'array') out[k] = d === null || d === undefined ? '' : JSON.stringify(d, null, 2);
    else out[k] = d === null || d === undefined ? '' : String(d);
  }
  return out;
}

/** 草稿 → 提交值；返回 [值, 错误列表]。空着的可选项不提交（交给服务端补默认值）。 */
export function collectInputs(specs: Record<string, InputSpec>, draft: InputDraft)
  : [Record<string, any>, string[]] {
  const out: Record<string, any> = {};
  const errors: string[] = [];
  for (const [k, s] of Object.entries(specs || {})) {
    const v = draft[k];
    const empty = v === '' || v === null || v === undefined;
    if (empty) {
      if (s.required && (s.default === null || s.default === undefined)) errors.push(`缺少必填入参「${k}」`);
      continue;
    }
    if (s.type === 'object' || s.type === 'array') {
      try {
        const parsed = JSON.parse(String(v));
        if (s.type === 'array' && !Array.isArray(parsed)) errors.push(`「${k}」应当是 JSON 数组`);
        else if (s.type === 'object' && (typeof parsed !== 'object' || Array.isArray(parsed))) errors.push(`「${k}」应当是 JSON 对象`);
        else out[k] = parsed;
      } catch { errors.push(`「${k}」不是合法的 JSON`); }
    } else {
      out[k] = v;
    }
  }
  return [out, errors];
}

export default function InputsForm({ specs, draft, onChange }: {
  specs: Record<string, InputSpec>; draft: InputDraft; onChange: (d: InputDraft) => void;
}) {
  const entries = Object.entries(specs || {});
  if (!entries.length) return <div className="hint-text">这个工作流没有声明入参，直接运行即可。</div>;
  const set = (k: string, v: any) => onChange({ ...draft, [k]: v });
  return (
    <div className="wf-inputs">
      {entries.map(([k, s]) => (
        <div key={k} className="wf-input-row">
          <div className="wf-input-label">
            <code>{k}</code>
            <span className="hint-text"> {s.type}{s.required ? ' · 必填' : ''}</span>
            {s.description && <div className="hint-text">{s.description}</div>}
          </div>
          {s.type === 'boolean' ? (
            <Switch checked={!!draft[k]} onChange={(v) => set(k, v)} />
          ) : s.type === 'integer' || s.type === 'number' ? (
            <InputNumber style={{ width: '100%' }} value={draft[k] as number | null}
              precision={s.type === 'integer' ? 0 : undefined}
              onChange={(v) => set(k, v)} />
          ) : s.type === 'object' || s.type === 'array' ? (
            <Input.TextArea className="mono" autoSize={{ minRows: 2, maxRows: 8 }}
              placeholder={s.type === 'array' ? '["a", "b"]' : '{"key": "value"}'}
              value={String(draft[k] ?? '')} onChange={(e) => set(k, e.target.value)} />
          ) : (
            <Input value={String(draft[k] ?? '')} placeholder={s.required ? '必填' : '可选'}
              onChange={(e) => set(k, e.target.value)} />
          )}
        </div>
      ))}
    </div>
  );
}
