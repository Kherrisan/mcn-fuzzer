//! Field mutations over an ordered proposal trace.
//!
//! `steps` is either an array of instance names or a `[start:end]` range.
//! Only `op: "field"` mutations are supported.

use serde_json::Value;

use crate::engine::input::{BytesEncoding, FieldKind, ParamValue};

#[derive(Clone, Debug)]
pub enum Selector {
    Each(Vec<usize>),
    Range(usize, usize),
}

pub fn instance_names(trace: &[impl Named]) -> Result<Vec<String>, String> {
    let mut names = Vec::new();
    for item in trace {
        let name = item.instance_name();
        if name.is_empty() || name.chars().any(|char| matches!(char, ':' | '[' | ']')) {
            return Err(format!("instance name {name:?} must not contain :[]"));
        }
        if names.iter().any(|seen| seen == name) {
            return Err(format!("duplicate instance name {name}"));
        }
        names.push(name.to_string());
    }
    Ok(names)
}

pub trait Named {
    fn instance_name(&self) -> &str;
    fn model_step(&self) -> &str;
}

pub fn selection(spec: &Value, names: &[String]) -> Result<Selector, String> {
    match spec {
        Value::Array(items) => {
            let mut indexes = Vec::new();
            for item in items {
                let name = item
                    .as_str()
                    .ok_or_else(|| "mutation steps must be instance names".to_string())?;
                let index = names
                    .iter()
                    .position(|candidate| candidate == name)
                    .ok_or_else(|| format!("unknown instance {name}"))?;
                if indexes.contains(&index) {
                    return Err("a mutation repeats an instance".into());
                }
                indexes.push(index);
            }
            Ok(Selector::Each(indexes))
        }
        Value::String(text) => {
            let text = text.trim();
            if !(text.starts_with('[') && text.ends_with(']') && text.contains(':')) {
                return Err(format!("selector {text:?} must be an array or [start:end]"));
            }
            let (start, end) = text[1..text.len() - 1]
                .split_once(':')
                .ok_or_else(|| format!("selector {text:?} must be [start:end]"))?;
            let start = start.trim();
            let end = end.trim();
            let Some(start) = names.iter().position(|item| item == start) else {
                return Err(format!("unknown range endpoint {start:?}"));
            };
            let Some(end) = names.iter().position(|item| item == end) else {
                return Err(format!("unknown range endpoint {end:?}"));
            };
            if start > end {
                return Err(format!("range {text} is reversed"));
            }
            Ok(Selector::Range(start, end))
        }
        _ => Err("mutation steps must be an array or [start:end]".into()),
    }
}

/// Parse the kind, bytes encoding, and optional default of a field mutation.
pub fn field_type(
    mutation: &Value,
) -> Result<(FieldKind, Option<BytesEncoding>, Option<ParamValue>), String> {
    let kind = mutation
        .get("kind")
        .cloned()
        .map(serde_json::from_value::<FieldKind>)
        .transpose()
        .map_err(|error| format!("invalid field kind: {error}"))?
        .unwrap_or_default();
    let encoding = mutation
        .get("encoding")
        .cloned()
        .map(serde_json::from_value::<BytesEncoding>)
        .transpose()
        .map_err(|error| format!("invalid bytes encoding: {error}"))?;
    if kind != FieldKind::Bytes && encoding.is_some() {
        return Err("encoding is only valid for bytes fields".into());
    }
    let encoding = (kind == FieldKind::Bytes).then(|| encoding.unwrap_or_default());
    let default = mutation
        .get("default")
        .cloned()
        .map(serde_json::from_value::<ParamValue>)
        .transpose()
        .map_err(|error| format!("invalid field default: {error}"))?;
    Ok((kind, encoding, default))
}

pub fn validate_mutations(
    names: &[String],
    send: impl Fn(usize) -> bool,
    mutations: &[Value],
) -> Result<(), String> {
    let mut seen_fields = Vec::new();
    for mutation in mutations {
        let op = mutation
            .get("op")
            .and_then(Value::as_str)
            .ok_or_else(|| "mutation needs an op".to_string())?;
        if op != "field" {
            return Err(format!(
                "unknown mutation op {op:?}; only field mutations are supported"
            ));
        }
        let selected = selection(mutation.get("steps").unwrap_or(&Value::Null), names)?;
        let indexes = match selected {
            Selector::Each(indexes) => indexes,
            Selector::Range(start, end) => (start..=end).collect(),
        };
        let field = mutation
            .get("field")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim();
        if field.is_empty() {
            return Err("field mutation needs a field name".into());
        }
        if mutation
            .get("suggestion")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .is_empty()
        {
            return Err(format!("field {field} needs a suggestion"));
        }
        if mutation
            .get("evidence")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .is_empty()
        {
            return Err(format!("field {field} needs source evidence"));
        }
        field_type(mutation)?;
        for index in indexes {
            let instance = names[index].as_str();
            if !send(index) {
                return Err(format!("field mutation on non-send instance {instance}"));
            }
            let key = (instance.to_string(), field.to_string());
            if seen_fields.contains(&key) {
                return Err(format!("field {field} is repeated on {instance}"));
            }
            seen_fields.push(key);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[derive(Clone, Debug, PartialEq)]
    struct Item {
        name: String,
        step: String,
        send: bool,
    }

    impl Named for Item {
        fn instance_name(&self) -> &str {
            &self.name
        }
        fn model_step(&self) -> &str {
            &self.step
        }
    }

    fn trace() -> Vec<Item> {
        vec![
            Item {
                name: "login_first".into(),
                step: "user".into(),
                send: true,
            },
            Item {
                name: "login_second".into(),
                step: "pass".into(),
                send: true,
            },
            Item {
                name: "listen_once".into(),
                step: "listen".into(),
                send: false,
            },
            Item {
                name: "quit".into(),
                step: "quit".into(),
                send: true,
            },
        ]
    }

    fn names(items: &[Item]) -> Vec<String> {
        instance_names(items).unwrap()
    }

    fn check(items: &[Item], mutations: Value) -> Result<(), String> {
        let names = names(items);
        validate_mutations(
            &names,
            |index| items[index].send,
            mutations.as_array().unwrap(),
        )
    }

    #[test]
    fn field_selects_one_or_many_and_allows_unmodeled_fields() {
        let items = trace();
        check(&items, json!([
            {"op":"field","steps":["login_first"],"field":"username","suggestion":"empty","evidence":"src/auth.c:120"}
        ])).unwrap();
        check(&items, json!([
            {"op":"field","steps":"[login_first:login_second]","field":"token","suggestion":"boundary","evidence":"src/auth.c:130"}
        ])).unwrap();
        let error = check(&items, json!([
            {"op":"field","steps":["listen_once"],"field":"port","suggestion":"x","evidence":"src/a.c:1"}
        ])).unwrap_err();
        assert!(error.contains("non-send"), "{error}");
    }

    #[test]
    fn field_kind_and_encoding_are_validated() {
        let items = trace();
        check(&items, json!([
            {"op":"field","steps":["login_first"],"field":"body","suggestion":"x",
             "evidence":"s:1","kind":"bytes","encoding":"hex"}
        ]))
        .unwrap();
        check(&items, json!([
            {"op":"field","steps":["login_first"],"field":"body","suggestion":"x",
             "evidence":"s:1","kind":"bytes","encoding":"base64"}
        ]))
        .unwrap();
        check(&items, json!([
            {"op":"field","steps":["login_first"],"field":"count","suggestion":"x",
             "evidence":"s:1","kind":"integer"}
        ]))
        .unwrap();
        for mutation in [
            json!({"op":"field","steps":["login_first"],"field":"x","suggestion":"x",
                   "evidence":"s:1","kind":"hex"}),
            json!({"op":"field","steps":["login_first"],"field":"x","suggestion":"x",
                   "evidence":"s:1","kind":"string","encoding":"hex"}),
            json!({"op":"field","steps":["login_first"],"field":"x","suggestion":"x",
                   "evidence":"s:1","kind":"bytes","encoding":"utf8"}),
        ] {
            let error = check(&items, json!([mutation])).unwrap_err();
            assert!(
                error.contains("kind") || error.contains("encoding"),
                "{error}"
            );
        }
    }

    #[test]
    fn duplicate_and_repeated_fields_are_rejected() {
        let items = trace();
        let error = check(
            &items,
            json!([{"op":"duplicate","steps":["login_first"],"count_param":"n"}]),
        )
        .unwrap_err();
        assert!(error.contains("only field mutations"), "{error}");
        let error = check(&items, json!([
            {"op":"field","steps":["login_first"],"field":"username","suggestion":"a","evidence":"s:1"},
            {"op":"field","steps":["login_first"],"field":"username","suggestion":"b","evidence":"s:2"}
        ]))
        .unwrap_err();
        assert!(error.contains("repeated"), "{error}");
        let error = selection(&json!("[quit:login_first]"), &names(&items)).unwrap_err();
        assert!(error.contains("reversed"), "{error}");
    }
}
