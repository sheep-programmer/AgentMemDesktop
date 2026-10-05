# 宣传片录制

实机录制、一镜到底：驱动正在运行的 AgentMem（后端 8765 + 前端 5173）完整走一遍核心流程，
CDP screencast 抓 2x 画面，后期按 60fps 合成——镜头推拉、光标缓动、静止段自动压缩、配乐与音效都在合成阶段完成。

流程：导入资料（实时处理阶段）→ 自动抽取的知识卡片 → 问答（检索、引用、来源卡片）→ 跳到原文高亮依据句
→ 上下文策略 / 对话模型 → 纠错 → 一键进化（确认、四个阶段、结果）→ 知识图谱回放 → 专家度。

```bash
# 准备：一个已有若干资料、跑过一次进化（有测验题）的 Space；chat 角色绑到要出镜的模型
cd apps/web
SPACE=<space_id> node scripts/promo/record.mjs     # 录制（会先删掉上一遍留下的演示资料和对话；约 5 分钟）
node scripts/promo/assets.mjs                        # 字幕、片头片尾、背景、光标
uv run --no-project --with pillow --with numpy python scripts/promo/compose.py   # 合成 → $PROMO_DIR/AgentMem-宣传片.mp4
```

- 中间产物与成片在 `PROMO_DIR`（默认 `/tmp/agentmem-promo`）。
- 字幕文案：`assets.mjs` 的 `CAPTIONS`；加速倍率：`record.mjs` 里的 `speed(...)`。
- 合成时可只渲染几张静帧检查镜头：`python scripts/promo/compose.py 12.5 40 61.2`（参数是成片时间，秒）。
- 配乐 `music.py` 用 numpy 现场合成（118 BPM），没有外部音频素材、没有版权问题。
