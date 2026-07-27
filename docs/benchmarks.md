# 基准测试

## 要回答的问题

一批文档已经进入 Cognee 后，如果只修改其中几篇、再删除一篇，是清空数据集重新构建，
还是让 cogindex 只处理变化的文档？

这里比较的是两种都能得到正确结果的做法：

- **全量重建**：硬删除临时数据集，写入当前仍存在的全部文档，再执行 `cognify`；
- **增量同步**：保留数据集，由 CocoIndex 找出变化，cogindex 执行删除、替换和
  `cognify`。

基线没有使用“在旧数据上继续 `add`”这种错误写法。那样会留下旧图和向量，速度再快也
不能作为对照。

## 场景

`real/smoke` 使用真实的本地 Cognee 存储栈：SQLite、LanceDB 和内嵌图数据库。LLM 和
嵌入调用换成固定输出，排除模型供应商的延迟、费用和结果波动。

每次重复都从 6 篇相同文档开始，然后：

1. 修改前 2 篇，实体由 `Entity00000/1` 改为 `Replacement00000/1`；
2. 删除最后 1 篇；
3. 保留其余 3 篇不变。

两个方案使用同一套 Cognee 配置和同一临时存储根目录，但写入不同的数据集；CocoIndex
跟踪库只属于增量方案。初始导入不计时。更新阶段运行 3 次，并交替两种方案的执行顺序。

计时范围：

- 全量重建：获取数据集锁、整库清理、写入当前 5 篇、`cognify`；
- 增量同步：CocoIndex 变更检测、删除 1 篇、清理 2 篇旧图和向量、重新写入这 2 篇、
  `cognify`。

全量组直接调用 `CogneeRuntime`，没有承担 CocoIndex 的调度开销。这是偏向基线的保守
比较。

## 结果

测试环境：

| 项目 | 值 |
| --- | --- |
| 系统 | macOS 26.5.2，arm64 |
| Python | 3.12.13 |
| CocoIndex | 1.0.18 |
| Cognee | 1.4.0 |
| 重复次数 | 3 |

| 指标 | 清空后全量重建 | cogindex 增量同步 |
| --- | ---: | ---: |
| 最终文档数 | 5 | 5 |
| 送入 `add` 的文档数 | 5 | 2 |
| 没有修改却被重新处理 | 3 | 0 |
| 同步耗时中位数 | 7.0640 秒 | 9.6685 秒 |
| 最小值—最大值 | 7.0554—7.0837 秒 | 9.6086—9.7138 秒 |
| 文档身份、数量和完成状态 | 通过 | 通过 |
| 旧实体残留 | 0 | 0 |
| 应有实体缺失 | 0 | 0 |

三次原始耗时：

```text
全量重建：7.0640, 7.0837, 7.0554
增量同步：9.7138, 9.6086, 9.6685
```

这个 6 篇文档的小场景里，增量同步没有更快。逐篇删除和清理图数据的固定开销高于重新
建立一个很小的数据集；结果只能证明它把重新送入处理的文档从 5 篇降到 2 篇，并避免了
3 篇无变化文档的重复工作。

真实模型下，少处理文档通常意味着更少的 token、嵌入和抽取请求，但本测试没有调用真实
模型，因此不把它换算成费用或线上吞吐量。耗时也不能外推到更大的语料。

原始 JSON 会记录 Git commit、工作树状态、依赖版本、机器信息、每次耗时和所有一致性
检查。最终候选提交的报告见
[`docs/benchmark-results/real-smoke.json`](benchmark-results/real-smoke.json)。

## 复现

运行本文的对比：

```bash
uv run python -m benchmarks.run \
  --profile smoke \
  --mode real \
  --categories baseline_comparison
```

报告写入 `benchmarks/reports/`。真实模式会创建独立临时目录，不读取或清理调用方已有的
Cognee 数据。

CI 使用内存 runtime 检查整个 benchmark harness、计数和失败退出码：

```bash
uv run python -m benchmarks.run --profile smoke --mode fake
```

也可以只运行其他场景：

```bash
uv run python -m benchmarks.run \
  --profile smoke \
  --mode fake \
  --categories incremental_update deletion crash_recovery verify_read
```

只要报告出现文档不一致、旧图未清理、故障恢复未收敛或注入的故障没有发生，命令就会以
非零状态退出。

## 如何解释结果

- `fake` 模式只测 CocoIndex 与 cogindex 的调度和记录处理，不代表 Cognee 性能；
- `real` 模式包含本地数据库和图处理，但固定输出模型不代表真实模型质量或延迟；
- 不同机器、提交、依赖版本和 profile 的 wall-clock 结果不能直接比较；
- `verify_dataset()` 本身看不到图内容，所以 real baseline 另行读取数据集图，核对旧实体
  和应有实体；
- 简历和 README 优先引用“处理了多少篇”这种由场景决定的数量；单机秒数只保留在本页和
  原始报告中。
