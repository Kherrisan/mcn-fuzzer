//! In-process artifact acceptance. Never imports generators or launches Python.

use std::{fs, path::Path};

use rustpython_ast::{self as ast, Visitor};
use rustpython_compiler::{compile, CompileOpts, Mode, Parse};
use serde_json::Value;

use crate::agents::artifacts::{check_identity, read_json};
use crate::engine::input::InstanceParams;

pub(super) fn check(
    output: &Path,
    request: &str,
) -> Result<(Value, InstanceParams), String> {
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
    let schema = read_json(&output.join("params.schema.json"))?;
    let params = read_json(&output.join("params.json"))?;
    check_parameters(&schema, &params)?;
    let params = serde_json::from_value(params)
        .map_err(|error| format!("params.json is not representable: {error}"))?;
    check_generator(&output.join("generator.py"))?;
    Ok((result, params))
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
                return Err(format!("params.json instance {instance} is not in the schema"));
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
        let fields = value.as_object().ok_or_else(|| {
            format!("params.json.{instance} must be an object of mutable fields")
        })?;
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
                _ => true,
            };
            if !matches {
                let article = if matches!(kind, "integer" | "array") {
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
        _ => Err(format!(
            "{path}: objects and null are not representable as ParamValue"
        )),
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
            &json!({"hello":{"value":[i64::MIN, i64::MAX, true, "x", []]}}),
        )
        .unwrap();
        for (params, expected) in [
            (json!({}), "missing required"),
            (json!({"hello":{"value":true}}), "must be an array"),
            (json!({"hello":{"value":[1.0]}}), "integer"),
            (json!({"hello":{"value":[null]}}), "not representable"),
            (json!({"hello":{"value":[{}]}}), "not representable"),
            (json!({"hello":{}}), "is empty"),
        ] {
            let error = check_parameters(&schema, &params).unwrap_err();
            assert!(error.contains(expected), "{params}: {error}");
        }
    }
}
