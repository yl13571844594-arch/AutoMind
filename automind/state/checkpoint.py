"""检查点管理 — 保存和恢复 Agent 状态。"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from automind.core.types import AgentState


class CheckpointManager:
    """检查点管理器 — 序列化/反序列化 AgentState。

    使用示例::

        mgr = CheckpointManager(".automind/checkpoints")
        checkpoint_id = await mgr.save(state)
        state = await mgr.load(checkpoint_id)
    """

    def __init__(self, checkpoint_dir: str | Path = ".automind/checkpoints") -> None:
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    async def save(self, state: AgentState) -> str:
        """保存当前状态为检查点。

        Returns:
            检查点文件路径。
        """
        state.last_updated = datetime.now(timezone.utc)
        checkpoint_id = f"ckpt_{state.session_id}_{int(time.time())}"
        file_path = self.checkpoint_dir / f"{checkpoint_id}.json"

        data = state.model_dump(mode="json")
        with open(file_path, "w", encoding="utf-8") as f:
            # B-04 修复：data 已由 model_dump(mode="json") 转为 JSON 兼容结构，
            # 不再使用 default=str 兜底——避免把不可序列化对象静默转成不可逆字符串。
            json.dump(data, f, indent=2, ensure_ascii=False)

        return checkpoint_id

    async def load(self, checkpoint_id: str) -> AgentState:
        """从检查点恢复状态。"""
        file_path = self.checkpoint_dir / f"{checkpoint_id}.json"
        if not file_path.exists():
            from automind.core.exceptions import CheckpointNotFoundError
            raise CheckpointNotFoundError(f"Checkpoint not found: {checkpoint_id}")

        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)

        return AgentState.model_validate(data)

    def list_checkpoints(self) -> list[dict[str, Any]]:
        """列出所有检查点。"""
        checkpoints = []
        for f in sorted(self.checkpoint_dir.glob("ckpt_*.json"), reverse=True):
            stat = f.stat()
            checkpoints.append({
                "id": f.stem,
                "path": str(f),
                "size": stat.st_size,
                "created": stat.st_mtime,
            })
        return checkpoints

    def delete_checkpoint(self, checkpoint_id: str) -> bool:
        """删除检查点。"""
        file_path = self.checkpoint_dir / f"{checkpoint_id}.json"
        if file_path.exists():
            file_path.unlink()
            return True
        return False

    async def prune(self, max_checkpoints: int = 20) -> int:
        """删除旧的检查点，只保留最新的 N 个。"""
        checkpoints = sorted(
            self.checkpoint_dir.glob("ckpt_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        removed = 0
        for cp in checkpoints[max_checkpoints:]:
            cp.unlink()
            removed += 1
        return removed
