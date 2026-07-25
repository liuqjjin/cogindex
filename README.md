# cogindex

[![CI](https://github.com/liuqjjin/cogindex/actions/workflows/ci.yml/badge.svg)](https://github.com/liuqjjin/cogindex/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20|%203.12%20|%203.13-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

English: [README.en.md](README.en.md)

让一份会持续变动的文档集合，和它派生出的知识图谱保持一致。

文档被改了，图谱要跟着改；文档被删了，只由它支撑的实体要跟着消失，而被别的文档共同引用的实体必须留下；中途崩溃了，下一次同步要能自己收敛回正确状态。这些事没有一件是调用一次 API 就能解决的。

## 问题出在哪

把文档灌进知识图谱，看上去是四行代码，而且它确实能跑：

```python
for text in documents:
    await cognee.add(text, dataset_name="docs")
await cognee.cognify(datasets=[dataset_id])
```

它一直正确，直到某个文档被修改。

`add()` 用内容的哈希来生成文档 id。内容一改，哈希就变，于是**改过的文档被当成一篇新文档**：新内容进去了，旧的那行还在，两个版本抽取出的实体并排躺在图里，地位完全相同。检索的时候分不出哪个是当前的。删除源文件更是什么都不会发生，因为从来没有任何东西记下过它对应哪一行。

这不是使用姿势不对，官方示例就是这么写的。问题在于**稳定身份必须有人来提供，如果集成层不做，就没有人做**。

下面是实测的代价。同样 6 篇文档、改 2 篇、删 1 篇，跑在同一套真实的本地栈上，左边是上面那四行，右边是 cogindex（[复现方式](docs/benchmarks.md)）：

| 三次同步之后 | 手写四行 | cogindex | 正确值 |
|---|---|---|---|
| 库里的文档数 | 9 | **5** | 5 |
| 图中已失效的实体数 | 4 | **0** | 0 |
| 删除源文件后真的删掉了 | 0 | **1** | 1 |
| 耗时 | 31.9 秒 | 20.4 秒 | |

cogindex 反而更快，因为它不会去重新抽取那些已经被取代的旧内容。

## 用法

```python
import cocoindex as coco
import cogindex

COGNEE = coco.ContextKey[cogindex.CogneeRuntime]("cognee")


@coco.fn
async def app_main(docs: dict[str, str]) -> None:
    target = await coco.use_mount(cogindex.declare_dataset_target, COGNEE, "docs")
    for key, content in docs.items():
        target.declare_document(key, content)
```

你只声明"应该存在什么"。再跑一次，只有内容变了的文档会被清理派生数据、重新写入、重新抽取。去掉一个 key，对应文档连同只由它支撑的图数据一起消失，而仍被其他文档引用的实体会留下。

拿一个文件夹直接试，不需要任何 API key：

```bash
git clone https://github.com/liuqjjin/cogindex && cd cogindex
make setup
python examples/quickstart_live.py ./my-docs --deterministic
```

[`examples/`](examples/) 下有这个快速上手示例，以及一个演示共享实体溯源的例子。

## 工作量正比于变更集，而不是语料规模

24 篇文档，改其中 6 篇，跑在真实栈上：

| | 抽取调用次数 | 耗时 |
|---|---|---|
| 首次同步 | 49 | 9.22 秒 |
| 内容没变，重跑一次 | **0** | **0.02 秒** |
| 改 6 篇 | **12** | 7.92 秒 |

请看调用次数这一列，不要看秒数。基准测试把大模型换成了确定性的替身，所以秒数量的是数据库开销；真实部署里一次抽取是几秒的延迟加账单上的一笔钱，那才是主导成本。改四分之一的语料，就付四分之一的抽取代价；什么都没改就一次都不调用，而没有稳定身份的集成永远做不到最后这一点，因为它无法判断眼前这篇是不是自己已经处理过的。

完整数据、测量所用的机器、以及每个数字的适用边界：[docs/benchmarks.md](docs/benchmarks.md)。

## 设计

六个问题，每个都有对应的决策记录（ADR）：

| 问题 | 做法 | ADR |
|---|---|---|
| **稳定身份** | `data_id = uuid5(命名空间, 运行时 ⧺ 租户 ⧺ 数据集 ⧺ key)`，只由逻辑坐标决定，绝不含内容，且拼接方式是单射的 | [0002](docs/adr/0002-stable-document-identity.md) |
| **幂等写入** | 每个操作都是"确保成立"而非"执行一次"：重复写入会收敛，删除不存在的东西算成功 | [0003](docs/adr/0003-consistency-model.md) |
| **内容替换** | 先清理派生数据，再用同一个 `data_id` 写回，最后抽取。少了第一步，旧内容的图节点和向量会留在原地 | [0004](docs/adr/0004-replace-delete-protocol.md) |
| **配置失效** | 每篇文档一个处理指纹，加上数据集级别的失效传播。上游的增量判断只看"处理完了没有"，从不看配置 | [0005](docs/adr/0005-configuration-invalidation.md) |
| **删除与归属** | 由 `managed_by` 决定卸载时是否清空；删除一律走上游的溯源规划器，而不是自己动手删图 | [0004](docs/adr/0004-replace-delete-protocol.md) |
| **崩溃后收敛** | 基于引擎的预提交/提交记录，对"所有可能的历史状态"做保守协调 | [0003](docs/adr/0003-consistency-model.md) |

最后一条是最值得讲的。这个项目做成目标状态连接器而不是一个带缓存的函数，是因为缓存只知道自己跑没跑过，对跑出来的外部状态一无所知：它没法删除、没法替换，也分不清一次崩溃的写入和一次完成的写入（[ADR-0001](docs/adr/0001-cocoindex-target-not-memoized-function.md)）。

## 保证什么，不保证什么

**保证，并且有测试**：幂等操作的至少一次投递，加上最终收敛。在写入协议的任意阶段崩溃后，下一次成功的同步会让图谱恰好等于声明的状态、派生数据全部是新的，并且协调过程达到不动点。

**不保证**：跨系统原子性。追踪存储和图谱的三个库无法放进同一个事务，所以在崩溃到下一次同步之间，读到的可能是应用了一半的状态。[ADR-0003](docs/adr/0003-consistency-model.md) 把每一个异常窗口都列了出来，而不是假装它们不存在。

已知边界，都是上游限制而非未完成的功能：

- 图谱必须在同一进程内运行。没有基于 HTTP 的实现，因为上游的 REST 接口不接受调用方指定的文档 id（[提案 0002](docs/upstream-proposals/0002-cognee-rest-add-data-id.md)）。
- 卸载时会清空数据集，但那条空的数据集记录会留下，上游没有公开的删除接口。
- `managed_by="user"` 的含义是"绝不销毁这个数据集里的任何东西"，不是"只删自己加的那部分"。
- `verify_dataset` 比对的是存在性、身份、抽取完成状态和标签，不比对原始内容，也看不出派生数据是否与当前内容匹配。

## 正确性是怎么建立的

| 层级 | 跑什么 | 命令 |
|---|---|---|
| 单元 | 协调决策矩阵、身份黄金值、11 个场景的故障注入矩阵、锁串行化、上游兼容面 | `make test` |
| 属性 | Hypothesis 状态机，随机交织声明、删除、改配置、同步、崩溃。用变异测试验证过：把清理派生数据这一步改成空操作，它会失败 | `make test-property` |
| 集成 | **真实的本地图谱栈**（SQLite、LanceDB、内嵌图库），大模型和向量用确定性替身。替换协议和共享实体溯源都在图这一层做断言 | `make test-integration` |
| PostgreSQL | 咨询锁语义，含持有连接死亡后的自动释放 | `make test-postgres` |
| 真实大模型 | 可选，端到端调真实供应商 | `make test-llm` |

不需要外部服务的那几层，覆盖率 89%，这个数字由 CI 的 `coverage` 任务算出。明显低于它的两个模块，`_locks_postgres` 由 PostgreSQL 层覆盖，`_doctor` 只做只读检查。

用内存替身写的测试从不冒充集成测试。那个替身刻意复现了上游的危险行为，包括重复写入后残留的旧派生数据、以及只看完成状态的增量判断，所以一旦协议不再补偿这些问题，测试就会失败。

集成测试层已经两次赚回了它的运行时间。它发现上游 `add()` 的逐项跳过闸门在默认配置下会静默吞掉替换内容，这一点源码审计漏掉了（[修正后的结论](docs/upstream-audit/cognee/findings.md)）；也是它把"逐篇文档拆一次图数据库 worker"这个让增量更新比全量重建还慢的问题，变成了一个具体数字。

## 运维

```python
report = await cogindex.verify_dataset(runtime, COGNEE, "docs", expected)
print(report.render())  # 缺失 / 多余 / 未完成 / 标签漂移

print(cogindex.doctor().render())  # 版本、能力、存储路径、凭据状态
```

`verify_dataset` 只负责发现问题，修复手段就是重跑一次流程，而这件事之所以安全，正是因为 ADR-0003 的收敛性质。

每个数据集的批量写入由 `LockProvider` 串行化：默认进程内，多进程场景用 PostgreSQL 咨询锁（`cogindex[postgres]`）。正确性从不依赖这把锁，属性测试在去掉锁之后仍然通过就是证据；锁的作用是避免重复劳动（[ADR-0006](docs/adr/0006-concurrency-and-locking.md)）。

## 安装与兼容性

尚未发布到 PyPI，从源码安装：

```bash
pip install git+https://github.com/liuqjjin/cogindex
```

Python 3.11 到 3.13，Linux 与 macOS。依赖区间 `cocoindex >=1.0.18,<2`、`cognee >=1.4.0,<1.5`。

两个上游都在固定的提交上做过通读审计，提交号记录在 [`UPSTREAM_LOCK.json`](UPSTREAM_LOCK.json)。上游每一个第一方源码文件都在[审计台账](docs/upstream-audit/)里标了明确的审阅状态，这一点由 CI 机器校验；审计过程中发现的四处缺口写成了[改进提案](docs/upstream-proposals/)。

## 目录

```
src/cogindex/          连接器本体，公开 API 在 __init__ 里再导出
docs/adr/              七份决策记录，建议从 0003 和 0004 读起
docs/upstream-audit/   两个上游的通读审计台账
docs/benchmarks.md     测量结果、所用机器、复现命令
tests/{unit,property,integration}
benchmarks/            七类基准测试
examples/              可直接运行的示例，不需要任何凭据
```

开发：`make setup && make ci`，详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 致谢与引用

本项目建立在两个开源项目之上，它们各自解决了自己领域内的难题：

- [CocoIndex](https://github.com/cocoindex-io/cocoindex)，增量数据处理引擎，提供了目标状态声明、变更检测和崩溃后的追踪语义。
- [Cognee](https://github.com/topoteretes/cognee)，知识图谱记忆层，负责摄入、抽取、溯源和检索。

cogindex 不修改也不复制它们的任何代码，只通过公开接口驱动，补上两者之间那层谁都没有覆盖的一致性协议。详细的依赖关系与版本说明见 [ATTRIBUTION.md](ATTRIBUTION.md)。

## 许可证

Apache-2.0。与上述两个项目均无隶属关系，二者按其各自的 Apache-2.0 许可证使用。
