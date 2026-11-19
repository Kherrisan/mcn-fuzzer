//! In-process artifact acceptance. Never imports generators or launches Python.

use std::{fs, path::Path};

use rustpython_ast::{self as ast, Visitor};
use rustpython_compiler::{compile, CompileOpts, Mode, Parse};
use serde_json::Value;

use crate::agents::artifacts::{check_identity, read_json};
use crate::engine::input::InstanceParams;

pub(super) fn check_result(output: &Path, request: &str) -> Result<Value, String> {
    let result = read_json(&output.join("result.json"))?;
    check_identity(&result, &[("request_id", request)])?;
    if result.get("status").and_then(Value::as_str) != Some("ok") {
        return Err(result
            .get("error")
            .and_then(Value::as_str)
            .filter(|error| !error.is_empty())
            .unwrap_or("generator synthesis failed")
            .to_string());
    }
    if !result
        .get("added_steps")
        .and_then(Value::as_array)
        .is_some_and(|steps| steps.iter().all(Value::is_string))
    {
        return Err("result.json added_steps must be an array of strings".to_string());
    }
    Ok(result)
}

pub(super) fn check_bundle(output: &Path) -> Result<(InstanceParams, Option<FieldManifest>), String> {
    let schema = read_json(&output.join("params.schema.json"))?;
    let params = read_json(&output.join("params.json"))?;
    check_parameters(&schema, &params)?;
    let params: InstanceParams = serde_json::from_value(params)
        .map_err(|error| format!("params.json is not representable: {error}"))?;
    check_generator(&output.join("generator.py"))?;
    let manifest = FieldManifest::read(output)?;
    Ok((params, manifest))
}

fn reject_remote_refs(value: &Value) -> Result<(), String> {
    match value {
        Value::Object(fields) => {
            for (key, value) in fields {
                if key == "$ref"
                    && value
                        .as_str()
                        .is_some_and(|s| s.contains("://") || s.starts_with("http"))
                {
                    return Err("parameter schema remote $ref is not allowed".to_string());
                }
                reject_remote_refs(value)?;
            }
        }
        Value::Array(items) => {
            for item in items {
                reject_remote_refs(item)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn check_parameters(schema: &Value, params: &Value) -> Result<(), String> {
    reject_remote_refs(schema)?;
    if schema.get("type").and_then(Value::as_str) != Some("object") {
        return Err("parameter schema must describe an object".to_string());
    }
    let params = params.as_object().ok_or("params.json must be an object")?;
    let properties = schema.get("properties").and_then(Value::as_object);
    if schema.get("additionalProperties") == Some(&Value::Bool(false)) {
        for instance in params.keys() {
            let known = properties.is_some_and(|properties| properties.contains_key(instance));
            if !known {
                return Err(format!(
                    "params.json instance {instance} is not in the schema"
                ));
            }
        }
    }
    if let Some(required) = schema.get("required").and_then(Value::as_array) {
        for name in required.iter().filter_map(Value::as_str) {
            if !params.contains_key(name) {
                return Err(format!("params.json is missing required instance {name}"));
            }
        }
    }
    for (instance, value) in params {
        let fields = value
            .as_object()
            .ok_or_else(|| format!("params.json.{instance} must be an object of mutable fields"))?;
        if fields.is_empty() {
            return Err(format!("params.json.{instance} is empty"));
        }
        let path = format!("params.json.{instance}");
        if let Some(spec) = properties.and_then(|properties| properties.get(instance)) {
            check_instance_schema(spec, fields, &path)?;
        }
        for (field, value) in fields {
            check_value(value, &format!("{path}.{field}"))?;
        }
    }
    Ok(())
}

fn check_instance_schema(
    spec: &Value,
    fields: &serde_json::Map<String, Value>,
    path: &str,
) -> Result<(), String> {
    if spec.get("type").and_then(Value::as_str) != Some("object") {
        return Err(format!("{path} schema must describe an object"));
    }
    let properties = spec.get("properties").and_then(Value::as_object);
    if spec.get("additionalProperties") == Some(&Value::Bool(false)) {
        for field in fields.keys() {
            let known = properties.is_some_and(|properties| properties.contains_key(field));
            if !known {
                return Err(format!("{path} field {field} is not in the schema"));
            }
        }
    }
    if let Some(required) = spec.get("required").and_then(Value::as_array) {
        for name in required.iter().filter_map(Value::as_str) {
            if !fields.contains_key(name) {
                return Err(format!("{path} is missing required field {name}"));
            }
        }
    }
    for (field, value) in fields {
        if let Some(kind) = properties
            .and_then(|properties| properties.get(field))
            .and_then(|spec| spec.get("type"))
            .and_then(Value::as_str)
        {
            let matches = match kind {
                "string" => value.is_string(),
                "integer" => value.as_number().is_some_and(|n| n.is_i64() || n.is_u64()),
                "boolean" => value.is_boolean(),
                "array" => value.is_array(),
                "object" => value.is_object(),
                _ => true,
            };
            if !matches {
                let article = if matches!(kind, "integer" | "array" | "object") {
                    "an"
                } else {
                    "a"
                };
                return Err(format!("{path}.{field} must be {article} {kind}"));
            }
        }
    }
    Ok(())
}

fn check_value(value: &Value, path: &str) -> Result<(), String> {
    match value {
        Value::String(_) | Value::Bool(_) => Ok(()),
        Value::Number(number) if number.is_i64() => Ok(()),
        Value::Number(_) => Err(format!(
            "{path}: only signed 64-bit integer parameters are representable"
        )),
        Value::Array(items) => {
            for (index, item) in items.iter().enumerate() {
                check_value(item, &format!("{path}[{index}]"))?;
            }
            Ok(())
        }
        Value::Object(fields) => {
            for (name, item) in fields {
                if name.is_empty() {
                    return Err(format!("{path}: object member names must be nonempty"));
                }
                check_value(item, &format!("{path}.{name}"))?;
            }
            Ok(())
        }
        _ => Err(format!("{path}: null is not representable as ParamValue")),
    }
}

fn check_generator(path: &Path) -> Result<(), String> {
    let source = fs::read_to_string(path)
        .map_err(|error| format!("generator syntax: {}: {error}", path.display()))?;
    let name = path.to_string_lossy();
    check_generator_source(&source, &name)
}

fn check_generator_source(source: &str, name: &str) -> Result<(), String> {
    // Compile to RustPython bytecode for scope/syntax checks; never execute it.
    compile(source, Mode::Exec, name.to_string(), CompileOpts::default())
        .map_err(|error| format!("generator syntax: {error}"))?;
    let suite =
        ast::Suite::parse(source, name).map_err(|error| format!("generator syntax: {error}"))?;
    let mut has_step_handler = false;
    for stmt in &suite {
        let (decorators, function) = match stmt {
            ast::Stmt::FunctionDef(f) => (&f.decorator_list, Some(f)),
            ast::Stmt::AsyncFunctionDef(f) => (&f.decorator_list, None),
            _ => continue,
        };
        let is_step = decorators.iter().any(|decorator| match decorator {
            ast::Expr::Call(call) => matches!(call.func.as_ref(), ast::Expr::Name(name) if name.id.as_str() == "step_handler"),
            _ => false,
        });
        let is_init = decorators.iter().any(|decorator| matches!(decorator, ast::Expr::Name(name) if name.id.as_str() == "global_init"));
        if !is_step && !is_init {
            continue;
        }
        let Some(function) = function else {
            return Err("step handlers and global initializer must be synchronous".into());
        };
        let args = &function.args;
        let positional = args.posonlyargs.iter().chain(&args.args);
        if positional
            .clone()
            .filter(|arg| arg.default.is_none())
            .count()
            > 2
            || (positional.count() < 2 && args.vararg.is_none())
            || args.kwonlyargs.iter().any(|arg| arg.default.is_none())
        {
            return Err("step handlers and global initializer must accept (ctx, params)".into());
        }
        let mut visitor = YieldCheck::default();
        for stmt in function.body.iter().cloned() {
            visitor.visit_stmt(stmt);
        }
        if visitor.found {
            return Err("step handlers and global initializer must not yield".into());
        }
        has_step_handler |= is_step;
    }
    if has_step_handler {
        return Ok(());
    }
    Err("generator must define at least one top-level @step_handler".into())
}

#[derive(Default)]
struct YieldCheck {
    found: bool,
}

impl Visitor for YieldCheck {
    fn visit_expr_yield(&mut self, _: ast::ExprYield) {
        self.found = true;
    }
    fn visit_expr_yield_from(&mut self, _: ast::ExprYieldFrom) {
        self.found = true;
    }
    fn visit_stmt_function_def(&mut self, _: ast::StmtFunctionDef) {}
    fn visit_stmt_async_function_def(&mut self, _: ast::StmtAsyncFunctionDef) {}
    fn visit_stmt_class_def(&mut self, _: ast::StmtClassDef) {}
    fn visit_expr_lambda(&mut self, _: ast::ExprLambda) {}
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn generator_checks_compile_without_importing_or_executing() {
        check_generator_source(
            "from generator_server import step_handler\n@step_handler('hello')\ndef hello(ctx, params):\n    pass\n",
            "generator.py",
        )
        .unwrap();
        assert!(check_generator_source(
            "@step_handler('hello')\nasync def hello(ctx, params):\n    pass\n",
            "generator.py",
        )
        .is_err());
        check_generator_source(
            "import module_that_does_not_exist\nraise RuntimeError()\n@step_handler('hello')\ndef hello(ctx, params):\n    pass\n",
            "generator.py",
        ).unwrap();
        for (source, expected) in [
            ("def generate(ctx, params):\n    pass\n", "step_handler"),
            ("@step_handler('x')\ndef x(ctx):\n    pass\n", "accept"),
            (
                "@step_handler('x')\ndef x(ctx, params):\n    yield 1\n",
                "yield",
            ),
            ("return 1\n", "syntax"),
        ] {
            let error = check_generator_source(source, "generator.py").unwrap_err();
            assert!(error.contains(expected), "{error}");
        }
    }

    #[test]
    fn parameter_checks_match_supported_schema_and_value_subset() {
        let schema = json!({"type":"object", "required":["hello"], "additionalProperties":false,
            "properties":{"hello":{"type":"object", "required":["value"], "additionalProperties":false,
                "properties":{"value":{"type":"array"}}}}});
        check_parameters(
            &schema,
            &json!({"hello":{"value":[i64::MIN, i64::MAX, true, "x", [], {}, {"group":29}]}}),
        )
        .unwrap();
        for (params, expected) in [
            (json!({}), "missing required"),
            (json!({"hello":{"value":true}}), "must be an array"),
            (json!({"hello":{"value":[1.0]}}), "integer"),
            (json!({"hello":{"value":[null]}}), "not representable"),
            (json!({"hello":{}}), "is empty"),
        ] {
            let error = check_parameters(&schema, &params).unwrap_err();
            assert!(error.contains(expected), "{params}: {error}");
        }
    }
}

/// One declared field capability from `mutable_fields.json`.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeclaredField {
    pub field: String,
    pub origin: crate::engine::input::FieldOrigin,
    #[serde(default)]
    pub kind: String,
    #[serde(default)]
    pub encoding: Option<String>,
    /// How the baseline value is produced: `baseline`, `fixed`, `derived`, `prior_state`.
    #[serde(rename = "default")]
    pub default_rule: String,
    /// How a coverage value reaches the message: `encoded`, `fixed`, `derived`, `rejected`.
    pub override_rule: String,
    #[serde(default)]
    pub omit: bool,
    #[serde(default)]
    pub sample: Option<Value>,
    /// The generator's own check that override and omission change the bytes.
    #[serde(default)]
    pub verified: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub verification: Option<String>,
}

/// Field capability list for one model step.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DeclaredStep {
    pub step: String,
    /// What the agent audited for this step, including rejected candidates.
    pub reason: String,
    pub fields: Vec<DeclaredField>,
}

/// `mutable_fields.json`.
///
/// Steps are keyed by model step name because one handler serves every instance
/// of that step and Rust creates, renames, duplicates, and drops instances after
/// the generator is published.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FieldManifest {
    #[serde(default)]
    pub session: String,
    #[serde(default)]
    pub request_id: String,
    pub steps: Vec<DeclaredStep>,
}

const DEFAULT_RULES: [&str; 4] = ["baseline", "fixed", "derived", "prior_state"];
const OVERRIDE_RULES: [&str; 4] = ["encoded", "fixed", "derived", "rejected"];
const MAX_REASON_BYTES: usize = 600;
const FIELD_KINDS: [&str; 6] = ["string", "bytes", "integer", "boolean", "list", "map"];

impl FieldManifest {
    /// Read the manifest, or `None` for a legacy generator that has none.
    pub(crate) fn read(output: &Path) -> Result<Option<Self>, String> {
        let path = output.join("mutable_fields.json");
        if !path.is_file() {
            return Ok(None);
        }
        let manifest: Self = serde_json::from_value(read_json(&path)?)
            .map_err(|error| format!("mutable_fields.json: {error}"))?;
        Ok(Some(manifest))
    }

    /// Validate the manifest against the proposal and the published params.
    ///
    /// The analyzed set must equal the proposal's declared fields so synthesis
    /// cannot drop or reclassify them; adhoc fields must be new. Every field must
    /// exist in params.json with a representable value, and the declared rules
    /// must be known. Adhoc fields must carry a successful capability check.
    pub(crate) fn check(
        &self,
        proposal: &crate::coverage::proposal::Proposal,
        params: &InstanceParams,
        request: &str,
    ) -> Result<(), String> {
        if !self.request_id.is_empty() && self.request_id != request {
            return Err(format!(
                "mutable_fields.json request_id {} does not match {request}",
                self.request_id
            ));
        }
        // Model step -> the analyzed fields its proposal instances declare.
        // Instances of one step must agree, because the manifest is per step.
        let mut expected: std::collections::BTreeMap<&str, std::collections::BTreeSet<&str>> =
            std::collections::BTreeMap::new();
        let mut step_instances: std::collections::BTreeMap<&str, Vec<&str>> =
            std::collections::BTreeMap::new();
        for instance in &proposal.trace {
            let fields = instance
                .mutable_fields
                .iter()
                .map(|field| field.field.as_str())
                .collect::<std::collections::BTreeSet<_>>();
            match expected.get(instance.step.as_str()) {
                Some(previous) if *previous != fields => {
                    return Err(format!(
                        "model step {} has instances declaring different mutable fields;                          declare the same set on every instance",
                        instance.step
                    ));
                }
                Some(_) => {}
                None => {
                    expected.insert(instance.step.as_str(), fields);
                }
            }
            step_instances
                .entry(instance.step.as_str())
                .or_default()
                .push(instance.name.as_str());
        }

        let mut seen = std::collections::BTreeSet::new();
        for declared in &self.steps {
            if !seen.insert(declared.step.as_str()) {
                return Err(format!(
                    "mutable_fields.json repeats model step {}",
                    declared.step
                ));
            }
            let Some(analyzed_expected) = expected.get(declared.step.as_str()) else {
                return Err(format!(
                    "mutable_fields.json step {} is not in the proposal trace",
                    declared.step
                ));
            };
            if declared.reason.trim().is_empty() {
                return Err(format!(
                    "mutable_fields.json step {} needs a non-empty reason",
                    declared.step
                ));
            }
            if declared.reason.len() > MAX_REASON_BYTES {
                return Err(format!(
                    "mutable_fields.json step {} reason exceeds {MAX_REASON_BYTES} bytes",
                    declared.step
                ));
            }
            let instances = step_instances
                .get(declared.step.as_str())
                .map(Vec::as_slice)
                .unwrap_or(&[]);
            let mut analyzed = std::collections::BTreeSet::new();
            let mut adhoc = std::collections::BTreeSet::new();
            for field in &declared.fields {
                let label = format!("{}.{}", declared.step, field.field);
                match field.origin {
                    crate::engine::input::FieldOrigin::Analyzed => {
                        if !analyzed_expected.contains(field.field.as_str()) {
                            return Err(format!(
                                "mutable_fields.json marks {label} analyzed but the proposal does not declare it"
                            ));
                        }
                        if !analyzed.insert(field.field.as_str()) {
                            return Err(format!(
                                "mutable_fields.json repeats analyzed field {label}"
                            ));
                        }
                    }
                    crate::engine::input::FieldOrigin::Adhoc => {
                        if analyzed_expected.contains(field.field.as_str()) {
                            return Err(format!(
                                "mutable_fields.json marks {label} adhoc but the proposal declares it"
                            ));
                        }
                        adhoc.insert(field.field.as_str());
                        if !field.verified {
                            return Err(format!(
                                "adhoc field {label} needs a capability check before publishing"
                            ));
                        }
                    }
                }
                if !DEFAULT_RULES.contains(&field.default_rule.as_str()) {
                    return Err(format!(
                        "field {label} has unknown default rule {}",
                        field.default_rule
                    ));
                }
                if !OVERRIDE_RULES.contains(&field.override_rule.as_str()) {
                    return Err(format!(
                        "field {label} has unknown override rule {}",
                        field.override_rule
                    ));
                }
                if !field.kind.is_empty() && !FIELD_KINDS.contains(&field.kind.as_str()) {
                    return Err(format!("field {label} has unknown kind {}", field.kind));
                }
                if field
                    .encoding
                    .as_deref()
                    .is_some_and(|encoding| !matches!(encoding, "hex" | "base64"))
                {
                    return Err(format!("field {label} has unsupported encoding"));
                }
                if let Some(sample) = &field.sample {
                    check_declared_value(sample, &format!("{label}.sample"))?;
                }
            }
            for field in analyzed_expected {
                if !analyzed.contains(field) {
                    return Err(format!(
                        "mutable_fields.json drops analyzed field {}.{field}",
                        declared.step
                    ));
                }
            }
            // Every instance of the step must carry exactly the declared fields.
            for name in instances {
                let values = params.get(*name);
                let present = values.map(|fields| fields.keys()).into_iter().flatten();
                let present = present
                    .map(|field| field.as_str())
                    .collect::<std::collections::BTreeSet<_>>();
                let declared_set = analyzed
                    .iter()
                    .chain(adhoc.iter())
                    .copied()
                    .collect::<std::collections::BTreeSet<_>>();
                if present != declared_set {
                    return Err(format!(
                        "params.json.{name} fields {:?} do not match the declared fields {:?} for step {}",
                        present, declared_set, declared.step
                    ));
                }
                for field in &declared.fields {
                    let Some(value) = values.and_then(|fields| fields.get(&field.field)) else {
                        return Err(format!(
                            "mutable_fields.json declares {}.{} but params.json.{name} has no such field",
                            declared.step, field.field
                        ));
                    };
                    check_kind(
                        value,
                        &field.kind,
                        &format!("{}.{}", declared.step, field.field),
                    )?;
                }
            }
        }
        for (step, instances) in &step_instances {
            let declared = self.steps.iter().any(|row| row.step == *step);
            if !declared && instances.iter().any(|name| params.contains_key(*name)) {
                return Err(format!(
                    "mutable_fields.json has no entry for model step {step}, which has params"
                ));
            }
        }
        Ok(())
    }

    /// Field provenance map attached to the execution input.
    pub(crate) fn origins(
        &self,
    ) -> std::collections::BTreeMap<String, std::collections::BTreeMap<String, crate::engine::input::FieldOrigin>> {
        let mut map = std::collections::BTreeMap::new();
        for declared in &self.steps {
            let fields = declared
                .fields
                .iter()
                .map(|field| (field.field.clone(), field.origin))
                .collect::<std::collections::BTreeMap<_, _>>();
            if !fields.is_empty() {
                map.insert(declared.step.clone(), fields);
            }
        }
        map
    }
}

fn check_declared_value(value: &Value, path: &str) -> Result<(), String> {
    check_value(value, path)
}

/// Reject a declared kind that the baseline value cannot represent.
fn check_kind(value: &crate::engine::input::ParamValue, kind: &str, path: &str) -> Result<(), String> {
    use crate::engine::input::ParamValue;
    let actual = match value {
        ParamValue::Null => "null",
        ParamValue::Int(_) => "integer",
        ParamValue::Bool(_) => "boolean",
        ParamValue::Str(_) => "string",
        ParamValue::List(_) => "list",
        ParamValue::Bytes(_) => "bytes",
        ParamValue::Map(_) => "map",
    };
    if kind.is_empty() {
        return Ok(());
    }
    // Bytes travel as an encoded string in JSON, so both spellings match.
    let compatible = actual == kind || (kind == "bytes" && actual == "string");
    if compatible {
        Ok(())
    } else {
        Err(format!("{path} declares kind {kind} but holds {actual}"))
    }
}

#[cfg(test)]
mod manifest_tests {
    use super::*;
    use crate::engine::input::{FieldOrigin, ParamValue};

    fn proposal() -> crate::coverage::proposal::Proposal {
        let step = crate::coverage::proposal::ProposalStep {
            name: "login".into(),
            step: "Send USER".into(),
            action: "send".into(),
            condition: String::new(),
            message: None,
            channel: "control".into(),
            sender: Some("User-PI".into()),
            receiver: Some("Server-PI".into()),
            party: None,
            reason: "r".into(),
            constraints: Vec::new(),
            mutable_fields: vec![crate::coverage::proposal::MutableField {
                field: "username".into(),
                kind: crate::engine::input::FieldKind::String,
                encoding: None,
                default: None,
                suggestion: None,
                evidence: None,
                source: String::new(),
            }],
            processing: None,
        };
        crate::coverage::proposal::Proposal {
            origin: "coverage".into(),
            session: "s".into(),
            build: "b".into(),
            snapshot: "000001".into(),
            goal: crate::coverage::proposal::ProposalGoal {
                handler_ids: vec!["ftpUSER".into()],
                reason: "r".into(),
            },
            parties: Default::default(),
            party_aliases: Default::default(),
            channels: Default::default(),
            trace: vec![step],
            dependencies: Vec::new(),
        }
    }

    fn params() -> InstanceParams {
        let mut fields = std::collections::BTreeMap::new();
        fields.insert("username".to_string(), ParamValue::Str("ubuntu".into()));
        fields.insert("greeting".to_string(), ParamValue::Str("HELO".into()));
        std::collections::BTreeMap::from([("login".to_string(), fields)])
    }

    fn field(name: &str, origin: FieldOrigin, verified: bool) -> DeclaredField {
        DeclaredField {
            field: name.into(),
            origin,
            kind: "string".into(),
            encoding: None,
            default_rule: "baseline".into(),
            override_rule: "encoded".into(),
            omit: true,
            sample: Some(Value::String("sample".into())),
            verified,
            verification: None,
        }
    }

    #[test]
    fn manifest_accepts_analyzed_plus_verified_adhoc() {
        let manifest = FieldManifest {
            session: "s".into(),
            request_id: "r1".into(),
            steps: vec![DeclaredStep {
                step: "Send USER".into(),
                reason: "helper reads username from params; greeting added and verified".into(),
                fields: vec![
                    field("username", FieldOrigin::Analyzed, false),
                    field("greeting", FieldOrigin::Adhoc, true),
                ],
            }],
        };
        manifest.check(&proposal(), &params(), "r1").expect("valid");
        let origins = manifest.origins();
        assert_eq!(origins["Send USER"]["greeting"], FieldOrigin::Adhoc);
    }

    #[test]
    fn manifest_rejects_unverified_adhoc_and_dropped_analyzed() {
        let mut manifest = FieldManifest {
            session: "s".into(),
            request_id: "r1".into(),
            steps: vec![DeclaredStep {
                step: "Send USER".into(),
                reason: "audit".into(),
                fields: vec![
                    field("username", FieldOrigin::Analyzed, false),
                    field("greeting", FieldOrigin::Adhoc, false),
                ],
            }],
        };
        assert!(manifest.check(&proposal(), &params(), "r1").is_err());

        manifest.steps[0].fields[1].verified = true;
        manifest.steps[0].fields.remove(0);
        let error = manifest.check(&proposal(), &params(), "r1").unwrap_err();
        assert!(error.contains("drops analyzed"), "{error}");
    }

    #[test]
    fn manifest_rejects_missing_params_and_wrong_kind() {
        let mut manifest = FieldManifest {
            session: "s".into(),
            request_id: "r1".into(),
            steps: vec![DeclaredStep {
                step: "Send USER".into(),
                reason: "audit".into(),
                fields: vec![
                    field("username", FieldOrigin::Analyzed, false),
                    field("greeting", FieldOrigin::Adhoc, true),
                ],
            }],
        };
        manifest.check(&proposal(), &params(), "r1").expect("valid");
        manifest.steps[0].fields[0].kind = "integer".into();
        let error = manifest.check(&proposal(), &params(), "r1").unwrap_err();
        assert!(error.contains("declares kind integer"), "{error}");

        manifest.steps[0].fields[0].kind = "string".into();
        manifest.steps[0].fields.remove(1);
        let error = manifest.check(&proposal(), &params(), "r1").unwrap_err();
        assert!(error.contains("do not match the declared fields"), "{error}");
    }

    #[test]
    fn manifest_requires_a_reason_and_a_steps_entry() {
        let mut manifest = FieldManifest {
            session: "s".into(),
            request_id: "r1".into(),
            steps: vec![DeclaredStep {
                step: "Send USER".into(),
                reason: "   ".into(),
                fields: vec![
                    field("username", FieldOrigin::Analyzed, false),
                    field("greeting", FieldOrigin::Adhoc, true),
                ],
            }],
        };
        let error = manifest.check(&proposal(), &params(), "r1").unwrap_err();
        assert!(error.contains("needs a non-empty reason"), "{error}");

        manifest.steps[0].reason = "audit".into();
        manifest.steps[0].step = "Send PASS".into();
        let error = manifest.check(&proposal(), &params(), "r1").unwrap_err();
        assert!(error.contains("not in the proposal"), "{error}");
    }

    #[test]
    fn legacy_generator_without_manifest_still_validates_params() {
        let dir = std::env::temp_dir().join(format!("mcn-legacy-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        assert!(FieldManifest::read(&dir).unwrap().is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
