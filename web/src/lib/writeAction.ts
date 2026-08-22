// 写操作的失败必须说出来 —— 尤其是确认框里的那种。
//
// antd 的 `modal.confirm({ onOk })` 拿到一个 rejected promise 时，只会往控制台
// 打一行 error，然后把确认框**僵在原地**：用户看到的是"点了删除没反应"，
// 既不知道失败了，也不知道为什么。全局的 unhandledrejection 兜底也接不到它
// （antd 已经把 rejection 接走了）。所以这类地方要就地兜住并明确提示。
import { message } from 'antd';
import { errText } from '../api/client';

/**
 * 包一次"确认后执行"的写操作。
 *
 * @param what 面向用户的动作名（如「删除专家」），用于拼失败提示。
 * @param fn   真正的写操作；抛错即视为失败。
 */
export function writeAction(what: string, fn: () => Promise<void>) {
  return async () => {
    try {
      await fn();
    } catch (e) {
      message.error(`${what}失败：${errText(e)}`);
    }
  };
}
