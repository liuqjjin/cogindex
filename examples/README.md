# 示例

三个示例都可在本地运行。`quickstart_live.py` 通过 `--deterministic` 使用确定性的
LLM 和嵌入替身；另外两个示例默认使用相同的替身。该模式不需要模型凭据，适合
检查同步与物化过程，但输出不能代表真实模型的抽取质量。要使用已配置的模型，
请先参考 [`.env.example`](../.env.example)；运行
`quickstart_live.py` 时省略 `--deterministic`，运行
`shared_entity_demo.py` 时添加 `--real`。

`quickstart_live.py` 会给确定性替身单独记录处理版本。以后在同一 storage 上切换到真实
模型时，处理指纹会变化并重新生成图和向量，不会把替身输出误认为真实模型的当前结果。

以下命令均从仓库根目录运行。

## quickstart_live.py：文件夹 → 知识图谱

```bash
uv run python examples/quickstart_live.py ./my-docs --deterministic
# 修改、新增或删除文件后再次运行
uv run python examples/quickstart_live.py ./my-docs --deterministic --search "你的问题"
# 持续监听文件变化
uv run python examples/quickstart_live.py ./my-docs --deterministic --live
```

该示例把文件相对路径作为稳定身份：同一路径在多次运行中对应同一份 Cognee
文档。编辑文件会触发替换，删除文件会清理对应数据；同步结束后，
`verify_dataset` 检查物化状态是否与文件夹中的声明一致。

这个示例采用 `mount_each` 的逐文件组件模式，便于引擎分别记忆和实时更新每个
文件，代价是每个发生变化的文件形成一个小同步批次。如果需要一次导入大量文档，
可以改为从单个组件声明全部文档，使一次同步只执行一批 `add` 和一次 `cognify`。
实现方式可参考 `tests/unit/test_engine_lifecycle.py`。

## shared_entity_demo.py：共享实体与溯源

```bash
uv run python examples/shared_entity_demo.py
```

示例依次执行三次同步。一个同时被两份文档引用的实体，在其中一份文档被替换后
仍会保留；最后一份引用它的文档被删除后，该实体才会消失。删除操作由 Cognee
的溯源删除规划器完成，cogindex 提供稳定身份和替换协议。

确定性模式下的关键输出如下，省略了文档内容行：

```
== step 1: both documents synced
   graph entities: ['AlphaCorp', 'Bob', 'Carol', 'SharedOrg']
== step 2: bob.md edited (AlphaCorp -> BetaCorp); SharedOrg must survive
   graph entities: ['BetaCorp', 'Bob', 'Carol', 'SharedOrg']
== step 3: carol.md removed; SharedOrg loses its last reference
   graph entities: ['BetaCorp', 'Bob']
```

## agent_memory_demo.py：Agent 读取更新后的图记忆

```bash
uv run python examples/agent_memory_demo.py
```

示例先把 `ProjectAtlas routes alerts to BlueQueue` 写入知识图谱，让一个只有图查询
工具的最小 Agent 回答当前路由；随后编辑同一个 `routing.md`，把队列改为
`GreenQueue`，再做一次增量同步并重复提问。

这里没有引入 Agent 框架，也没有把固定回答冒充检索结果。Agent 的答案来自图中的
`routes_alerts_to` 关系；脚本最后还会直接检查 `GreenQueue` 已经出现且
`BlueQueue` 已经消失。确定性模式下的关键输出是：

```
1. Initial sync: ProjectAtlas routes alerts to BlueQueue.
   Agent answer: ProjectAtlas routes alerts to BlueQueue.
2. Edit routing.md: BlueQueue -> GreenQueue; run incremental sync.
   Agent answer: ProjectAtlas routes alerts to GreenQueue.
   Graph check: GreenQueue present=True
   Graph check: BlueQueue absent=True
3. Passed: the agent read the new fact; the old graph memory is gone.
```
