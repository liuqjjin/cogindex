# 基准测试

## 当前状态

`baseline_comparison` 暂时停用。

旧实现存在两个问题：

1. 直接调用 Cognee 全局清理函数时，尚未为对比组绑定独立的临时存储目录；
2. 第三次同步移动了编辑集合，实际场景与文档写的“编辑 2 篇、删除 1 篇”不一致。

为避免误删已有数据和继续传播错误数字，当前实现只返回明确的 `skipped` 结果。旧版
README 和简历中引用的文档数、失效实体数及耗时暂不作为项目指标。

重新启用前必须满足：

- 直接调用组和 cogindex 组使用不同的临时数据目录、系统目录和跟踪库；
- 测试开始前验证这些目录不等于任何外部配置路径；
- 编辑集合与删除集合固定且互不意外移动；
- 删除结果、文档数量和失效实体均从实际存储读取，不使用常量填充；
- 报告记录完整 Git 提交号、工作树是否干净、依赖版本和机器信息；
- 原始 JSON 报告随用于 README 的结果一起提交；
- 同一个计时场景重复运行，报告中位数和离散程度。

## 现有测试入口

使用内存运行时测量连接器和 CocoIndex 的开销：

```bash
uv run python -m benchmarks.run --profile smoke --mode fake
```

运行指定场景：

```bash
uv run python -m benchmarks.run \
  --profile smoke \
  --mode fake \
  --categories incremental_update deletion verify_read
```

真实本地 Cognee 模式使用 SQLite、LanceDB、内嵌图数据库以及确定性模型替身：

```bash
uv run python -m benchmarks.run \
  --profile smoke \
  --mode real \
  --categories initial_ingest incremental_update deletion verify_read
```

确定性模型替身用于排除供应商延迟和费用。该模式得到的耗时主要反映本地数据库、进程和
批处理开销，不能当作真实模型部署的吞吐量。

## 报告要求

benchmark harness 会在 `benchmarks/reports/` 生成 JSON 和 Markdown。开发阶段的临时报告
默认不提交；任何进入 README、简历或发布说明的数字，必须同时满足：

- 来自最终候选提交；
- 工作树干净；
- 对应命令可以在文档中直接运行；
- 原始 JSON 可以从仓库或 CI artifact 找到；
- 不与其他机器的 wall-clock 直接比较；
- 结论不超过样本本身能够说明的范围。

在新的 baseline 报告通过这些检查前，本项目不对外使用旧的 `9 → 5`、`4 → 0`、
`31.9s → 20.4s` 等数字。
