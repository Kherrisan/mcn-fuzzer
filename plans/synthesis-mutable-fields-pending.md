# Synthesis 可变字段功能待实现

状态：已实现。字段清单、校验、两阶段调度与 corpus 重载均已接入；见文末“实现记录”。

## 目标

Synthesis 实现每个 step handler 时，先复用已有 helper 建立基线调用，再分析实参可以如何覆盖或省略。除 coverage proposal 指定的字段外，还可记录 synthesis 在实现过程中发现的字段。

## 待实现内容

- Synthesis 输出独立的 `mutable_fields.json`，按 step 实例列出字段，用 `analyzed` 和 `adhoc` 枚举区分 proposal 字段与 synthesis 补充的字段。记录字段类型、缺省规则、覆盖方式、是否支持省略和可编码的样例值。
- 对字段能力进行验证，检查覆盖或省略是否反映在编码后的消息中。基线缺省、前序状态、固定值和派生值分别记录。
- Rust 以字段清单作为字段变异依据。发布、验收和 replay 读取清单，旧 generator 和 seed 继续兼容已有参数格式。复制 step、保存和重载 corpus 时保留实例与字段能力的关联。
- 新 generator 的首次变异预算分为两段，先选择 `analyzed` 字段，再选择 `adhoc` 字段。两组都可用时预算平分，奇数时前组多一次；单组可用时使用全部预算。普通 corpus 调度允许选择两组。事件记录字段来源和操作。
- 验证清单格式、基线行为、字段能力、发布失败清理、两阶段调度、旧产物兼容及 corpus 重载。

## 当前状态

已移除新 campaign 的 autonomous synthesis 和无 seed 自动 bootstrap，并调整 generator 发布为先校验临时目录、再原子发布。现有运行产物仍为 `generator.py`、`params.schema.json` 和 `params.json`。当前 field mutator 从 case 已有的 `params` 选择字段；没有 `mutable_fields.json` 或 analyzed/adhoc 两阶段调度。

## 实现记录

```text
产物      OUTPUT_DIR/mutable_fields.json，发布时复制到 published/
格式      {session, request_id, steps:[{step, reason, fields:[{field, origin, kind,
          encoding, default, override_rule, omit, sample, verified, verification}]}]}
          以 model step 为键；每一步必带 reason（审计结论，零候选也要写）
校验      analyzed 集合必须等于该 step 在 proposal 中声明的字段并集；同一 step 的
          实例必须声明一致，且每个实例的 params 键集合必须等于声明字段集；
          reason 非空且不超过 600 字节；adhoc 必须是新增字段且
          verified=true；每个字段必须存在于 params.json；kind/encoding/default/
          override_rule 取值受限；sample 必须可表示
Rust      FieldManifest + DeclaredStep/DeclaredField（artifacts.rs）
          GeneratorInput.field_origin（按 model step → 字段，serde 保留，旧文件缺省为空；
          实例通过自己的 step 查表，副本自动继承）
          发布走 staging 校验后原子 rename；失败清理 staging 且不产生 published/
调度      新 generator 首批预算先 analyzed 后 adhoc；两组都有时对半、奇数给
          analyzed；单组独占；池空时回退任意字段；普通 corpus 两组合用
事件      mutation 操作记录 origin=analyzed|adhoc
重放      input.json 携带 field_origin；corpus 重载保留实例与字段关联
兼容      无 mutable_fields.json 的旧 generator、seed、旧 corpus 条目：
          所有参数按 analyzed 处理
测试      191 项 Rust 单测通过；coverage_mcp 集成测试通过；99 项 Python 测试通过
```

### 未覆盖的部分

- 动态能力验证由 agent 在发布前执行（本地 socket 比较 baseline / sample / 省略
  三种编码结果），Rust 只校验 `verified` 标志与结构一致性，不复跑生成器。
- `sample` 只做可表示性检查，不校验其编码结果。
