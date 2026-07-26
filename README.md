# cogindex

[![持续集成](https://github.com/liuqjjin/cogindex/actions/workflows/ci.yml/badge.svg)](https://github.com/liuqjjin/cogindex/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![许可证](https://img.shields.io/badge/许可证-Apache--2.0-blue)](LICENSE)

[英文说明](README.en.md)

cogindex 把 CocoIndex 检测到的文档变更同步到 Cognee 知识图谱。新增文件要写入，修改过的
文件要替换，源文件删除后，原来抽取出的实体、关系和向量也要一起清掉。

第一次导入并不难，直接调用 `cognee.add()` 和 `cognee.cognify()` 就可以。问题通常出在
第二次同步：Cognee 默认按内容生成文档标识，内容一改就成了另一篇文档；重新 `add` 同一个
`data_id` 又不会自动删除旧的图和向量数据；删除过程如果执行到一半，关系库里还可能保留一条
看似已经处理完成的记录。cogindex 处理的就是这些情况，它不修改 Cognee 的抽取逻辑。

项目当前版本是 `0.1.0`，尚未发布到 PyPI。接口仍可能调整，不应把它当作已经稳定的
生产组件。

## 先跑起来

需要 Python 3.11、3.12 或 3.13，以及 [uv](https://docs.astral.sh/uv/)：

```bash
git clone https://github.com/liuqjjin/cogindex.git
cd cogindex
uv sync --all-extras

mkdir -p my-docs
printf '# CocoIndex\nCocoIndex tracks source changes.\n' > my-docs/cocoindex.md
printf '# Cognee\nCognee builds a knowledge graph.\n' > my-docs/cognee.md
uv run python examples/quickstart_live.py ./my-docs --deterministic
```

这个例子使用测试中的确定性模型替身，不需要 API key，适合检查同步过程。修改或删除
`my-docs` 里的文件后再运行一次，可以看到同一份数据被更新或删除。加入 `--live` 可以持续
监听目录。确定性模式的输出不能用来评价真实模型的抽取效果。

接入已有的 CocoIndex 流程时，先注册 Cognee 运行时，再为数据集声明 target。下面是一个
最小例子；真实模型所需的环境变量见 [.env.example](.env.example)。

```python
import asyncio
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


app = coco.App(
    coco.AppConfig(name="cogindex_readme_example", environment=environment),
    app_main,
)


async def run() -> None:
    await app.update().result()
    report = await cogindex.verify_dataset(
        runtime,
        COGNEE,
        "docs",
        [cogindex.ExpectedDocument("guide.md", label="guide.md")],
    )
    print(report.render())


asyncio.run(run())
```

`"guide.md"` 是文档在源系统里的稳定 key，也可以换成仓库相对路径、数据库主键或对象存储
key。正文更新时 key 不变，因此 `data_id` 也不变。

`ContextKey` 必须使用固定的逻辑名称，不要放 URL、DSN 或密钥。这个名称会写入跟踪状态，
也会参与 `data_id` 的生成。

文件夹同步、递归路径和同步后的检查都可以参考
[examples/quickstart_live.py](examples/quickstart_live.py)。

## 文档变更时会发生什么

每篇文档都有固定的 UUID5。CocoIndex 记录上次已经确认的状态，cogindex 根据本次声明和旧
记录选择操作：

| 变化 | 写入方式 |
| --- | --- |
| 新文档 | 写入原文并执行 `cognify` |
| 正文、外部元数据、`node_set` 或处理配置变化 | 先用 `forget(memory_only=True)` 清除旧派生数据，再以原 `data_id` 写入并重新处理 |
| `importance_weight` 变化 | 删除原始记录后按原 `data_id` 重建；Cognee 不能原地更新这个字段 |
| 只有标签变化且旧状态明确 | 重新写入标签，不重复抽取 |
| 源文档删除 | 删除原文及其不再被其他文档引用的派生数据 |

同一数据集的一批写入共用一把锁，需要执行的步骤按删除、清理旧派生数据、批量 `add` 排列；
需要重新抽取时，整批最多调用一次 `cognify`。多进程写同一数据集时可以使用
`PostgresAdvisoryLockProvider`；它需要安装 `postgres` extra，并要求所有写入进程连接同一个
PostgreSQL。默认的 `InProcessLockProvider` 只负责单进程内的并发。

CocoIndex 和 Cognee 不能共用一个事务。外部写入中途失败时，CocoIndex 会保留未确认的新旧
状态；下一次同步据此重新执行替换或删除。只要源文档和处理配置不再变化、跟踪库还在，并且
之后有一次同步能够完整执行，数据就能恢复一致。

具体取舍记录在：

- [稳定文档标识](docs/adr/0002-stable-document-identity.md)
- [失败后的重试规则](docs/adr/0003-consistency-model.md)
- [替换和删除顺序](docs/adr/0004-replace-delete-protocol.md)
- [处理配置变化](docs/adr/0005-configuration-invalidation.md)
- [数据集锁](docs/adr/0006-concurrency-and-locking.md)

## 检查实际数据

`verify_dataset()` 能发现缺少的文档、多出来的文档、未完成的 `cognify` 和标签不一致。它
不会读取图和向量，也不会自动修复问题。`cogindex.doctor()` 用来检查本地配置和模型凭据。

## 安装和限制

PyPI 上还没有这个包，可以直接从 Git 安装：

```bash
python3 -m pip install "git+https://github.com/liuqjjin/cogindex.git"
```

uv 项目可以使用：

```bash
uv add "cogindex @ git+https://github.com/liuqjjin/cogindex.git"
```

当前支持 Python `>=3.11,<3.14`、CocoIndex `>=1.0.18,<2` 和 Cognee
`>=1.4.0,<1.5`。

使用 `LocalCogneeRuntime` 时，`data_root` 和 `system_root` 都必须显式传入。同一进程里
同时存在的本地运行时必须使用相同目录。这个运行时只接受 `tenant="default"`；如果传入
Cognee `user`，按名称查找 dataset 时只会使用该用户自己拥有的数据集。

还有几项限制需要提前知道：

- Cognee 的 REST `add` 不能传 `data_id`，因此目前只支持进程内运行时；
- 卸载由系统管理的 target 会清空 dataset，但 Cognee 仍会留下空的 dataset 记录；
- `managed_by="user"` 只禁止卸载 target 时清空整个数据集；target 仍在时，停止声明的文档
  仍会按正常规则删除；
- 如果 CocoIndex 跟踪库丢失，需要先对整个 dataset 执行一次
  `forget(memory_only=True)`，再重新同步；
- `verify_dataset()` 只能检查文档记录、标识、处理状态和标签，不能检查派生内容。

## 测试

日常开发使用这些命令：

```bash
make ci                # Ruff、mypy、审计门禁、单元测试、属性测试
make test-integration  # 本地 Cognee，使用确定性模型替身
make test-postgres     # PostgreSQL 咨询锁
make test-llm          # 可选；调用真实模型，会产生费用
make coverage
make smoke             # 构建 wheel，并在干净环境中导入
```

当前有 287 个单元测试、60×40 步的 Hypothesis 状态机、9 个本地 Cognee 集成测试和
4 个 PostgreSQL 锁测试。CI 覆盖 Linux、macOS 和 Python 3.11–3.13，总覆盖率为 91%
（语句 93%，分支 85%）。

Cognee 集成测试使用 SQLite、LanceDB 和内嵌图数据库，模型调用使用固定输出的测试替身，
不代表真实模型效果。

性能基准正在重做；旧结果因场景定义和存储隔离问题已经撤下，进度见
[docs/benchmarks.md](docs/benchmarks.md)。

## 设计与源码

公开接口在 [`src/cogindex/__init__.py`](src/cogindex/__init__.py)，实现模块位于
`src/cogindex/`。`tests/unit/`、`tests/property/` 和 `tests/integration/` 分别对应单元、
属性和集成测试。架构决定及其后续修订放在 [`docs/adr/`](docs/adr/)。

两个上游仓库的固定提交写在 [`UPSTREAM_LOCK.json`](UPSTREAM_LOCK.json)。源码审查记录位于
[`docs/upstream-audit/`](docs/upstream-audit/)：直接相关的代码和测试做详细检查，邻近模块
确认接口，其余文件只记录分类和判断理由。

## 上游与许可证

这个项目建立在 [CocoIndex](https://github.com/cocoindex-io/cocoindex) 和
[Cognee](https://github.com/topoteretes/cognee) 之上，前者提供变更检测与跟踪记录，后者
负责文档摄入、知识抽取和存储。感谢两个项目提供的实现和测试。

依赖与许可证说明见 [ATTRIBUTION.md](ATTRIBUTION.md)。cogindex 使用 Apache-2.0 许可证，
与 CocoIndex、Cognee 均无隶属关系。
