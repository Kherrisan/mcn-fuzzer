//! In-process artifact acceptance. Never imports generators or launches Python.

use std::{collections::BTreeMap, fs, path::Path};

use rustpython_ast::{self as ast, Visitor};
use rustpython_compiler::{compile, CompileOpts, Mode, Parse};
use serde_json::Value;

use super::ParamValue;
use crate::agents::artifacts::{check_identity, read_json};

pub(super) fn check(
    output: &Path,
    request: &str,
) -> Result<(Value, BTreeMap<String, ParamValue>), String> {
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
    if let Some(required) = schema.get("required").and_then(Value::as_array) {
        for name in required.iter().filter_map(Value::as_str) {
            if !params.contains_key(name) {
                return Err(format!("params.json is missing required field {name}"));
            }
        }
    }
    if let Some(properties) = schema.get("properties").and_then(Value::as_object) {
        if schema.get("additionalProperties") == Some(&Value::Bool(false)) {
            for key in params.keys() {
                if !properties.contains_key(key) {
                    return Err(format!("params.json field {key} is not in the schema"));
                }
            }
        }
        for (key, spec) in properties {
            if let (Some(value), Some(kind)) =
                (params.get(key), spec.get("type").and_then(Value::as_str))
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
                    return Err(format!("{key} must be {article} {kind}"));
                }
            }
        }
    }
    for (key, value) in params {
        check_value(value, &format!("params.json.{key}"))?;
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
    let function = suite.into_iter().rev().find(|stmt| match stmt {
        ast::Stmt::FunctionDef(f) => f.name.as_str() == "generate",
        ast::Stmt::AsyncFunctionDef(f) => f.name.as_str() == "generate",
        _ => false,
    });
    let Some(ast::Stmt::FunctionDef(function)) = function else {
        return Err(
            "generator must define synchronous top-level def generate(ctx, params)".to_string(),
        );
    };
    let args = &function.args;
    let positional = args.posonlyargs.iter().chain(&args.args);
    let count = positional.clone().count();
    let required = positional.filter(|arg| arg.default.is_none()).count();
    if required > 2
        || (count < 2 && args.vararg.is_none())
        || args.kwonlyargs.iter().any(|arg| arg.default.is_none())
    {
        return Err(
            "generate() signature must accept exactly the runtime call generate(ctx, params)"
                .to_string(),
        );
    }
    let mut visitor = YieldCheck::default();
    for stmt in function.body {
        visitor.visit_stmt(stmt);
    }
    if visitor.found {
        return Err(
            "generate() must execute synchronously, not return a yield generator".to_string(),
        );
    }
    Ok(())
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
            "import module_that_does_not_exist\nraise RuntimeError()\ndef generate(ctx, params):\n    pass\n",
            "generator.py",
        )
        .unwrap();
        for (source, expected) in [
            (
                "async def generate(ctx, params):\n    pass\n",
                "synchronous",
            ),
            ("def generate(ctx, params):\n    yield 1\n", "yield"),
            ("def generate(ctx):\n    pass\n", "signature"),
            ("return 1\ndef generate(ctx, params):\n    pass\n", "syntax"),
        ] {
            let error = check_generator_source(source, "generator.py").unwrap_err();
            assert!(error.contains(expected), "{error}");
        }
    }

    #[test]
    fn parameter_checks_match_supported_schema_and_value_subset() {
        let schema = json!({"type":"object", "required":["value"], "additionalProperties":false,
            "properties":{"value":{"type":"array"}}});
        check_parameters(
            &schema,
            &json!({"value":[i64::MIN, i64::MAX, true, "x", []]}),
        )
        .unwrap();
        for (params, expected) in [
            (json!({}), "missing required"),
            (json!({"value":true}), "must be an array"),
            (json!({"value":[1.0]}), "integer"),
            (json!({"value":[null]}), "not representable"),
            (json!({"value":[{}]}), "not representable"),
        ] {
            let error = check_parameters(&schema, &params).unwrap_err();
            assert!(error.contains(expected), "{params}: {error}");
        }
    }
}
