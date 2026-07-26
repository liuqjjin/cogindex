# cogindex

[![持续集成](https://github.com/liuqjjin/cogindex/actions/workflows/ci.yml/badge.svg)](https://github.com/liuqjjin/cogindex/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![许可证](https://img.shields.io/badge/许可证-Apache--2.0-blue)](LICENSE)

英文说明：[README.en.md](README.en.md)

cogindex 用来把一组持续变化的文档同步到 Cognee 知识图谱。它作为
CocoIndex 的自定义目标，负责稳定文档标识、增量更新、删除、失败重试和配置变更后的
重新处理。

这个项目解决的是同步问题，不负责知识抽取模型本身。CocoIndex 提供目标状态和变更
跟踪，Cognee 负责文档摄入、知识抽取和图谱存储，cogindex 负责让两边在文档发生增删改
时保持一致。

> 当前版本为 `0.1.x`，仍在进行发布前加固。接口、测试和已知限制都保留在公开仓库中；
> 在 `0.2.0` 完成前，不建议用于无法接受重建的数据集。

## 为什么需要单独的同步层

直接调用 Cognee 可以完成首次导入：

```python
for text in documents:
    await cognee.add(text, dataset_name="docs")
await cognee.cognify(datasets=[dataset_id])
```

但这段代码没有保存“源文档 key 对应哪个 Cognee 文档”的关系。内容发生变化时，
Cognee 默认会根据新内容生成新的标识；源文件被删除时，也没有足够的信息找到需要删除
的记录。

cogindex 使用源系统中的稳定 key，例如仓库内相对路径、数据库主键或对象存储 key，
生成固定的 UUID5：

```text
data_id = uuid5(固定命名空间, runtime_key + tenant + dataset + document_key)
```

内容不参与标识计算。因此：

- 同一个 key 的内容变化会更新原文档，而不是新增一个版本；
- key 不再声明时，可以定位并删除对应文档；
- 一次写入中途失败后，下一次运行仍能根据跟踪记录继续处理；
- 处理配置变化时，可以让已有文档重新抽取。

标识规则和兼容约束见
[ADR-0002](docs/adr/0002-stable-document-identity.md)。

## 快速开始

克隆仓库并安装依赖：

```bash
git clone https://github.com/liuqjjin/cogindex.git
cd cogindex
uv sync --all-extras
```

准备一个包含 Markdown 或文本文件的目录，然后运行不需要 API key 的确定性示例：

```bash
uv run python examples/quickstart_live.py ./my-docs --deterministic
```

修改、新增或删除文件后再次运行，示例会重新同步并打印校验结果。持续监听目录：

```bash
uv run python examples/quickstart_live.py ./my-docs --deterministic --live
```

确定性模式只用于验证同步过程，不代表真实模型的抽取质量。

## 在 CocoIndex 流程中使用

```python
import cocoindex as coco
import cogindex

COGNEE = coco.ContextKey[cogindex.CogneeRuntime]("cognee")


@coco.fn
async def app_main(documents: dict[str, str]) -> None:
    target = await coco.use_mount(
        cogindex.declare_dataset_target,
        COGNEE,
        "docs",
    )
    for document_key, content in documents.items():
        target.declare_document(document_key, content)
```

`ContextKey` 的字符串会参与文档标识并写入 CocoIndex 跟踪库，只能使用 `cognee`
这类逻辑名称，不能放 DSN、URL、API key 或其他凭据。公开入口不接受普通字符串代替
`ContextKey`；名称只能包含 1–128 个 ASCII 字母、数字、点、下划线或连字符，且必须
以字母或数字开头。

运行应用前，需要把本地运行时放入同一个 `ContextProvider`：

```python
from pathlib import Path

runtime = cogindex.LocalCogneeRuntime(
    data_root=Path("./data/cognee"),
    system_root=Path("./data/cognee-system"),
)

environment = coco.Environment(
    coco.Settings.from_env(db_path="./data/cocoindex-tracking"),
)
environment.context_provider.provide(COGNEE, runtime)
```

完整可运行代码见 [examples/quickstart_live.py](examples/quickstart_live.py)。

## 一次同步做什么

`reconcile()` 只比较目标状态和跟踪记录，不执行外部 I/O。真正的写入在 sink 中按数据集
批量执行，并使用同一把数据集锁：

```text
删除不再需要的文档
    ↓
清理被替换文档的图和向量数据
    ↓
批量写入新增或更新的文档
    ↓
对本批次执行一次 cognify
    ↓
提交 CocoIndex 跟踪记录
```

其中几条规则不能省略：

- 文档标识只由逻辑位置决定，不能由内容决定；
- 替换内容前先清理旧派生数据；
- `add()` 必须关闭上游默认的逐项跳过和数据缓存，否则替换内容可能没有真正写入；
- 删除不存在的文档按成功处理，但权限错误和其他写入错误必须向上抛出；
- 只有实际尝试并成功完成的写入才能提交跟踪记录。

详细设计见：

- [ADR-0003：一致性模型](docs/adr/0003-consistency-model.md)
- [ADR-0004：替换与删除顺序](docs/adr/0004-replace-delete-protocol.md)
- [ADR-0005：配置变更后的重新处理](docs/adr/0005-configuration-invalidation.md)
- [ADR-0006：数据集锁](docs/adr/0006-concurrency-and-locking.md)

## 正确性边界

cogindex 不提供跨系统事务。CocoIndex 跟踪库、Cognee 关系库、图数据库和向量数据库不能
放进同一个事务，因此进程异常退出后，外部状态可能暂时只完成了一部分。

项目采用的恢复方式是：

1. 外部操作保持幂等；
2. CocoIndex 在写入前保存待提交记录；
3. 失败后把所有可能的旧记录交给 handler；
4. handler 只在这些可能状态都与目标一致时停止处理。

当前自动恢复范围针对由同步流程自身产生的未确认状态。若用户绕过 cogindex 直接修改
Cognee，`verify_dataset()` 可以发现部分漂移，但普通重跑不一定能够修复。外部漂移需要
显式重建或后续提供的修复接口。

## 校验和环境检查

```python
report = await cogindex.verify_dataset(
    runtime,
    COGNEE,
    "docs",
    expected_documents,
)
print(report.render())

print(cogindex.doctor().render())
```

`verify_dataset()` 当前检查：

- 缺失文档；
- 未声明的额外文档；
- cognify 未完成；
- 标签不一致。

它不能证明图和向量一定来自当前内容，也不直接修改数据。

## 测试

```bash
make test              # 单元测试
make test-property     # Hypothesis 状态机
make test-integration  # 真实本地 Cognee，使用确定性模型替身
make test-postgres     # PostgreSQL 咨询锁
make test-llm          # 可选，调用真实模型供应商
make ci                # 静态检查、类型检查、审计门禁、单元和属性测试
```

当前持续集成覆盖：

- Linux、macOS；
- Python 3.11、3.12、3.13；
- Ruff 和格式检查；
- strict mypy；
- 286 个单元测试；
- 60 组、每组 40 步的 Hypothesis 状态机；
- 真实 SQLite、LanceDB 和内嵌图数据库集成测试；
- PostgreSQL 咨询锁测试；
- wheel 构建和干净环境导入。

最近一次覆盖率任务在启用分支统计后为 91%（语句 93%，分支 85%）。覆盖率只说明
哪些代码路径被执行过，不能替代真实上游行为测试。

## 基准测试

仓库保留了 benchmark harness，但旧版对比场景正在重做。原因是旧场景的编辑集合定义
有误，而且清理测试数据前没有先绑定隔离存储目录。

在新的隔离测试、原始 JSON 报告和可达提交号全部准备好之前，本 README 不引用旧的
耗时和文档数量结果，也不建议运行 real baseline。测量方法和修订状态见
[docs/benchmarks.md](docs/benchmarks.md)。

## 安装与兼容性

项目尚未发布到 PyPI。可以从 Git 安装：

```bash
python3 -m pip install "git+https://github.com/liuqjjin/cogindex.git"
```

或者在 uv 项目中添加：

```bash
uv add "cogindex @ git+https://github.com/liuqjjin/cogindex.git"
```

支持范围：

- Python `>=3.11,<3.14`
- CocoIndex `>=1.0.18,<2`
- Cognee `>=1.4.0,<1.5`

本地运行时还有三条约束：

- `data_root` 和 `system_root` 必须同时显式传入；
- 同一进程中同时存在的 `LocalCogneeRuntime` 必须使用相同的存储目录；
- `LocalCogneeRuntime` 只接受 `tenant="default"`，实际 Cognee 租户由传入的
  `user` 决定；按名称查找时只会绑定该用户自己拥有的数据集，不会误用同名共享数据集。

Cognee 版本相关导入统一放在
[`src/cogindex/_compat.py`](src/cogindex/_compat.py)；项目不修改上游运行时代码。

## 项目结构

```text
src/cogindex/          包实现和公开接口
tests/unit/            协调逻辑、身份、锁和运行时单元测试
tests/property/        随机失败序列和收敛检查
tests/integration/     真实本地 Cognee、PostgreSQL 和可选真实模型测试
examples/              可直接运行的文件夹同步与共享实体示例
docs/adr/              设计决策及其修改记录
docs/upstream-audit/   固定版本的上游源码审查记录
benchmarks/            benchmark harness 和场景
```

两个上游的固定提交记录在
[`UPSTREAM_LOCK.json`](UPSTREAM_LOCK.json)。审查采用分级方式：与连接器直接相关的
代码和测试做详细检查，邻近模块确认接口，其余文件只做分类记录。台账用于保存检查范围
和判断依据，不代表逐行读完两个仓库。

## 上游项目

- [CocoIndex](https://github.com/cocoindex-io/cocoindex)：提供目标状态、变更检测和跟踪记录。
- [Cognee](https://github.com/topoteretes/cognee)：负责文档摄入、知识抽取、图谱和检索。

依赖关系与许可证说明见 [ATTRIBUTION.md](ATTRIBUTION.md)。

## 许可证

Apache-2.0。cogindex 与 CocoIndex、Cognee 均无隶属关系。
