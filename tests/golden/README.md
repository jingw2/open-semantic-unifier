# Golden Test Set

本目录保存公开的合成开发回归集。所有名称、身份引用、电话和商品标识均为虚构测试值。`TEST-*` 不是合法税号或 GTIN；本集验证实体决策流程，不验证法定标识格式。

## 目录与用途

`v1/manifest.json` 固定数据版本、文件列表、SHA-256 和数量。三个场景各包含 20 条输入记录及 10 组预期配对，共 60 条记录和 30 组配对。`v1/lifecycle.json` 定义 22 项状态变化契约。

- `records.json` 是唯一的匹配输入，包含来源键和原始属性，不含真值。
- `scheme.json` 是该场景的配置契约。实现需要显式解析并映射到生产方案类型，禁止根据场景名称选择写死答案。
- `expected.json` 是评估真值和动作，禁止发送给匹配器、模型或生产规则。
- `lifecycle.json` 是未来集成测试的输入规范和断言目录，当前不是已实现的产品测试驱动器。

方案中的强标识仅在这个合成项目内被明确授信。真实用户必须确认自己的标识权威性；共享邮箱、电话、供应商 SKU 不能默认作为全局身份。商品中的规格冲突优先于相同标识。

每个配对家族独立构造。初始运行只允许 expected 为 AUTO_MATCH 的两个来源记录进入同一实体；其他共簇关系均无自动确认授权。自然真值 UNRESOLVED 的记录不能因缺少标签而被自动合并。合并后的人工生命周期在 L04 及其他契约中另行验证。

## 验证数据集与评分器

在仓库根目录执行。只用 Python 标准库，不修改现有开发数据库。

```bash
python3 scripts/golden.py validate
python3 -m unittest discover -s tests -p 'test_golden_contract.py' -v
```

第一条应报告 `fixture_integrity=PASS`、60 records、30 pairs、22 lifecycle，以及 `product_status=NOT_EVALUATED`。

第二条只证明评分器能够接受完整预期、拒绝全送审、遗漏候选、危险聚类和不完整结果。自测将 oracle 转成预测是评分器单元测试，不能当作产品评估证据。

## 真实产品预测格式

`scripts/run_golden.py` 将 records 和 scheme 提交给真实应用，保留 API 返回的 execution ID、scheme revision、实际候选、动作和当前实体成员关系。三个 domain 必须全部提供；每个 domain 包含全部来源记录及全部十个配对结果。预期文件只用于关联 case_id 与左右来源引用，脚本不读取 truth 或 expected_action。

```json
{
  "golden_version": "1.0.0",
  "run_id": "actual-application-run-or-report-id",
  "scheme_revision": "actual-scheme-revision-or-report-reference",
  "domains": {
    "supplier": {
      "assignments": {
        "supplier:erp:01": "opaque-entity-id",
        "supplier:secondary:01": "opaque-entity-id"
      },
      "pairs": [
        {
          "case_id": "supplier-01-verified_id",
          "candidate": true,
          "action": "AUTO_MATCH"
        }
      ]
    }
  }
}
```

上例只展示结构，故意不完整，评分器会拒绝它。适配器可以读 expected 中的 case_id 和来源引用以关联输出，但不能使用 truth、expected_action 或 required_candidate 生成预测。需要为每个 domain 保存真实任务与方案版本，顶层字段可指向汇总运行报告。

```bash
OPEN_SEMANTIC_UNIFIER_TOKEN=... python3 scripts/run_golden.py \
  --base-url http://localhost:8000 \
  --project-id PROJECT_ID \
  --output /absolute/path/to/predictions.json
python3 scripts/golden.py evaluate /absolute/path/to/predictions.json
```

匹配服务不直接执行实体合并。当前实体成员关系来自应用实际导入记录；在 T4 持久裁决与合并能力完成前，AUTO_MATCH 配对仍可能分属不同实体，评分器会如实报告未合并，不得改写 assignments 使其通过。

退出码 0 表示配对闸门通过；1 表示行为未满足；2 表示数据或预测结构无效。报告明确保留 `lifecycle_status=NOT_EVALUATED` 和 `product_release_status=NOT_EVALUATED`。

实体 ID 是应用生成的不透明值。比较来源成员关系，不比较实体 ID 字符串是否与测试写死值相同。候选召回必须来自实际召回阶段，不能通过最终动作反推。

## 生命周期执行约定

L01–L22 的业务前置和执行操作见 [生命周期步骤](../../docs/reference/golden-lifecycle.md)。每个 case 未来对应 `backend/tests/test_unification_lifecycle.py` 中一个真实 API/数据库测试。Celery、Phoenix、实际模型和 Compose 相关断言另在部署测试中执行。不能把 assertions 字符串解析成固定成功输出。

## 数据变更

先修改预期与解释，审查真值和动作，再递增版本及更新 manifest 哈希。记录新增场景及行为差异。当前没有隐藏测试集，不能报告留出集效果，也不能通过反复调参后宣称生产泛化能力。

基线固定后不得在评估过程中自动重写 manifest。数据修订需显式重新计算受影响文件的 SHA-256 并保留 diff。记录数量变化时同步更新清单和自检测试。
