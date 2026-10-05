# 参与贡献

欢迎提交问题、文档改进、测试和代码。请先查阅 [系统架构](docs/01-ARCHITECTURE.md) 与 [API 规范](docs/03-API-SPEC.md)，让修改保持在已有分层内：`core` 负责领域逻辑，`api` 负责 HTTP 适配，`web` 通过接口访问数据。

## 本地准备

```bash
uv sync --frozen
cd apps/web
pnpm install --frozen-lockfile
```

前端使用 `package.json` 中固定的 pnpm 版本。开发服务和模型配置见 [README](README.md)。

## 验证修改

在项目根目录运行后端检查：

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest -q
```

在 `apps/web` 中运行前端检查：

```bash
pnpm typecheck
pnpm lint
pnpm test:run
pnpm build
pnpm exec playwright install chromium
pnpm check:contrast
```

修改接口后运行 `pnpm gen:api`，提交同步更新的接口类型。请使用演示数据或临时空间验收；测试不应依赖开发者的密钥、真实文档和付费模型调用。

## 提交问题与 Pull Request

- 问题报告请提供运行系统、Python / Node.js / pnpm 版本、复现步骤，以及经过脱敏的错误信息或请求 ID。
- Pull Request 请说明具体问题、修改后的行为和验证结果。界面修改请附截图，覆盖相关深浅主题或窗口尺寸。
- 模型相关修改请描述适配器与模型类型，避免绑定某个厂商到领域逻辑中。
- 请勿提交 `.env`、运行时 `data/`、访问密钥、本机模型配置或客户资料。

贡献代码使用项目的 [MIT 许可证](LICENSE)。
