# cogindex

[![持续集成](https://github.com/liuqjjin/cogindex/actions/workflows/ci.yml/badge.svg)](https://github.com/liuqjjin/cogindex/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![许可证](https://img.shields.io/badge/许可证-Apache--2.0-blue)](LICENSE)

[英文说明](README.en.md)

cogindex 是一个 CocoIndex 自定义 target，用来把源文档的新增、修改和删除同步到 Cognee。

`cognee.add()` 和 `cognee.cognify()` 可以完成第一次导入。麻烦通常出在第二次同步：
正文改过以后，旧的图和向量不会自动删除；源文件消失后，也要找到原来的 `data_id`
才能清理；一次删除如果中途退出，还可能留下状态正常、图数据却不完整的文档记录。

cogindex 根据源文档的稳定标识生成固定 UUID。CocoIndex 发现变化并保存同步状态；
cogindex 调用 Cognee 写入、替换或删除文档。失败的操作会在下次同步时重试，知识抽取仍由
Cognee 负责。

## 快速开始

需要 Python 3.11、3.12 或 3.13，以及 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/liuqjjin/cogindex.git
cd cogindex
uv sync --all-extras

mkdir -p my-docs
printf '# CocoIndex\nCocoIndex tracks source changes.\n' > my-docs/cocoindex.md
printf '# Cognee\nCognee builds a knowledge graph.\n' > my-docs/cognee.md
uv run python examples/quickstart_live.py ./my-docs --deterministic
```

最后一行应显示 `0 issues`。修改或删除 `my-docs` 里的文件，再运行一次，就能检查更新和
删除是否生效。持续监听目录：

```bash
uv run python examples/quickstart_live.py ./my-docs --deterministic --live
```

`--deterministic` 会替换模型调用，输出固定且不需要 API key。它只检查同步逻辑，不代表
真实模型的抽取效果。真实模型所需的配置见 [.env.example](.env.example)。

## 在 CocoIndex 中使用

项目尚未发布到 PyPI，可以直接从 Git 安装：

```bash
python3 -m pip install "git+https://github.com/liuqjjin/cogindex.git"
```

uv 项目可以使用：

```bash
uv add "cogindex @ git+https://github.com/liuqjjin/cogindex.git"
```

下面省略 App 启动代码，只展示 runtime 和 target 的声明。完整的目录同步示例见
[examples/quickstart_live.py](examples/quickstart_live.py)。

```python
from pathlib import Path

import cocoindex as coco
import cogindex

COGNEE = coco.ContextKey[cogindex.CogneeRuntime]("cognee")

runtime = cogindex.LocalCogneeRuntime(
    data_root=Path("./data/cognee"),
    system_root=Path("./data/cognee-system"),
)
environment = coco.Environment(
    coco.Settings.from_env(db_path="./data/cocoindex-tracking"),
)
environment.context_provider.provide(COGNEE, runtime)


@coco.fn
async def app_main() -> None:
    target = await coco.use_mount(
        cogindex.declare_dataset_target,
        COGNEE,
        "docs",
    )
    target.declare_document(
        "guide.md",
        "CocoIndex tracks changes. Cognee builds the graph.",
        label="guide.md",
    )
```

`"guide.md"` 是源文档的稳定标识，也可以换成仓库相对路径、数据库主键或对象存储 key。
正文可以变化，这个标识不要跟着变。

`ContextKey` 同样要使用固定名称，例如 `"cognee"`。它会写入 CocoIndex 的跟踪记录，并
参与文档 ID 的生成；不要把 URL、DSN 或密钥放进去。

## 更新和删除

| 文档变化 | cogindex 的处理 |
| --- | --- |
| 新增 | 写入原文，执行 `cognify` |
| 正文、外部元数据、`node_set` 或处理配置变化 | 清理原来的图和向量，沿用原 `data_id` 写入并重新处理 |
| `importance_weight` 变化 | 删除原文记录，沿用原 `data_id` 重新创建 |
| 只有标签变化，且上一次同步状态已确认 | 更新标签，不重复抽取 |
| 删除 | 删除原文，以及不再被其他文档引用的图和向量数据 |

同一数据集的一批操作共用一把锁。默认锁只覆盖当前进程；多个进程会写入同一个数据集时，
可以安装 `postgres` extra 并使用 `PostgresAdvisoryLockProvider`。所有进程必须连接同一个
PostgreSQL 数据库作为锁服务。

CocoIndex 跟踪库与 Cognee 存储的写入不在同一个事务中。同步中断时，CocoIndex 会保留
写入前后的可能状态，下次运行据此重新执行替换或删除。源数据和处理配置不再变化、跟踪库
未丢失且后续一次同步成功完成后，Cognee 中由 cogindex 管理的数据会与当前输入一致。

`cogindex.doctor()` 用于检查本地存储和模型配置。`verify_dataset()` 可以检查文档缺失、
多余、处理未完成和标签不一致，但不会逐项比对图节点或向量内容。

## 兼容性和限制

当前版本为 `0.1.0`，支持：

- Python `>=3.11,<3.14`
- CocoIndex `>=1.0.18,<2`
- Cognee `>=1.4.0,<1.5`

API 仍可能调整，建议先用于可以重新构建的数据集。其他已知限制：

- Cognee 的 REST `add` 不能传入自定义 `data_id`；目前只支持直接调用 Python API 的
  `LocalCogneeRuntime`，不支持 REST 后端；
- `LocalCogneeRuntime` 必须显式设置 `data_root` 和 `system_root`；同一进程中的实例必须
  使用相同目录；
- 本地运行时只接受 `tenant="default"`；传入 Cognee `user` 时，只查询该用户拥有的数据集；
- 卸载系统管理的 target 会删除整个 Cognee 数据集；需要保留数据集时应使用
  `managed_by="user"`；
- `managed_by="user"` 只跳过 target 卸载时的整库清理；target 仍在时，不再声明的文档
  照常删除；
- CocoIndex 跟踪库丢失后，普通重跑无法判断哪些 Cognee 文档已经从源端删除。独占数据集应
  先停止写入，清空原文、图和向量，再全量同步；共享数据集不能自动恢复，建议改用新数据集
  或人工核对。

## 开发

```bash
make ci                # Ruff、mypy、上游审阅清单校验、单元测试、属性测试
make test-integration  # 本地 Cognee，替换模型调用
make test-postgres     # PostgreSQL advisory lock
make test-llm          # 可选；调用真实模型，会产生费用
make coverage
make smoke             # 构建 wheel，并在干净环境中导入
```

CI 的核心测试覆盖 Linux、macOS 和 Python 3.11–3.13。本地 Cognee、PostgreSQL、依赖审计
和 wheel 安装另有独立任务。

更多资料：

- [公开 API](src/cogindex/__init__.py)
- [设计记录](docs/adr/)
- [上游行为记录](docs/upstream-audit/)
- [基准测试说明](docs/benchmarks.md)
- [贡献指南](CONTRIBUTING.md)

## 上游项目与许可证

cogindex 依赖 [CocoIndex](https://github.com/cocoindex-io/cocoindex) 和
[Cognee](https://github.com/topoteretes/cognee)。前者提供变更检测与跟踪记录，后者负责
文档摄入、知识抽取和存储。

项目使用 Apache-2.0 许可证，与 CocoIndex、Cognee 均无隶属关系。第三方依赖和许可证说明
见 [ATTRIBUTION.md](ATTRIBUTION.md)。
