//! Field and duplicate mutations over an ordered proposal trace.
//!
//! `steps` is either an array of instance names or a `[start:end]` range.
//! Duplicate copies reuse the original instances and do not recurse.

use serde_json::Value;

pub const MAX_COUNT: i64 = 32;
pub const MAX_STEPS: usize = 4096;

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
            let body = &text[1..text.len() - 1];
            let (start_name, end_name) = body
                .split_once(':')
                .ok_or_else(|| format!("selector {text:?} must be an array or [start:end]"))?;
            let start_name = start_name.trim();
            let end_name = end_name.trim();
            let start = names
                .iter()
                .position(|name| name == start_name)
                .ok_or_else(|| format!("unknown range endpoint in {text}"))?;
            let end = names
                .iter()
                .position(|name| name == end_name)
                .ok_or_else(|| format!("unknown range endpoint in {text}"))?;
            if start > end {
                return Err(format!("range {text} is reversed"));
            }
            Ok(Selector::Range(start, end))
        }
        _ => Err("mutation steps must be an array or [start:end]".into()),
    }
}

/// Runtime duplicate count. Non-integers, including floats and booleans, are 0.
pub fn runtime_count(value: &Value) -> Result<usize, String> {
    let Some(number) = value.as_number() else {
        return Ok(0);
    };
    if let Some(count) = number.as_i64() {
        if (0..=MAX_COUNT).contains(&count) {
            return Ok(count as usize);
        }
        return Err(format!("duplicate count {count} is outside 0..{MAX_COUNT}"));
    }
    if number.as_u64().is_some() {
        return Err(format!("duplicate count is outside 0..{MAX_COUNT}"));
    }
    Ok(0)
}

pub fn validate_mutations(
    names: &[String],
    send: impl Fn(usize) -> bool,
    mutations: &[Value],
) -> Result<(), String> {
    let mut seen_fields = Vec::new();
    let mut duplicate_spans = Vec::new();
    for mutation in mutations {
        let op = mutation
            .get("op")
            .and_then(Value::as_str)
            .ok_or_else(|| "mutation needs an op".to_string())?;
        let selected = selection(mutation.get("steps").unwrap_or(&Value::Null), names)?;
        let indexes = match &selected {
            Selector::Each(indexes) => indexes.clone(),
            Selector::Range(start, end) => (*start..=*end).collect(),
        };
        match op {
            "field" => {
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
            "duplicate" => {
                if mutation
                    .get("count_param")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .trim()
                    .is_empty()
                {
                    return Err("duplicate mutation needs count_param".into());
                }
                let spans = match selected {
                    Selector::Each(indexes) => {
                        indexes.into_iter().map(|index| (index, index)).collect()
                    }
                    Selector::Range(start, end) => vec![(start, end)],
                };
                for span in spans {
                    if duplicate_spans.iter().any(|previous: &(usize, usize)| {
                        !(span.1 < previous.0 || previous.1 < span.0)
                    }) {
                        return Err("duplicate selections overlap".into());
                    }
                    duplicate_spans.push(span);
                }
            }
            other => return Err(format!("unknown mutation op {other:?}")),
        }
    }
    Ok(())
}

pub fn expand_named<T: Clone>(
    trace: &[T],
    names: &[String],
    mutations: &[Value],
    counts: &dyn Fn(&str) -> Value,
) -> Result<Vec<T>, String> {
    let mut expanded = trace.to_vec();
    let mut jobs = Vec::new();
    for mutation in mutations {
        if mutation.get("op").and_then(Value::as_str) != Some("duplicate") {
            continue;
        }
        let count = runtime_count(&counts(
            mutation
                .get("count_param")
                .and_then(Value::as_str)
                .unwrap_or(""),
        ))?;
        match selection(mutation.get("steps").unwrap_or(&Value::Null), names)? {
            Selector::Each(indexes) => {
                for index in indexes {
                    jobs.push((index, index, count));
                }
            }
            Selector::Range(start, end) => jobs.push((start, end, count)),
        }
    }
    jobs.sort_by_key(|job| std::cmp::Reverse(job.0));
    for (start, end, count) in jobs {
        let block = expanded[start..=end].to_vec();
        let mut extra = Vec::new();
        for _ in 0..count {
            extra.extend(block.iter().cloned());
        }
        expanded.splice(end + 1..end + 1, extra);
        if expanded.len() > MAX_STEPS {
            return Err(format!(
                "expanded trace has {} steps; limit is {MAX_STEPS}",
                expanded.len()
            ));
        }
    }
    Ok(expanded)
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
    fn duplicate_array_repeats_each_and_range_repeats_the_block() {
        let items = trace();
        let listed = names(&items);
        let each = expand_named(
            &items,
            &listed,
            json!([
                {"op":"duplicate","steps":["login_first","quit"],"count_param":"n"}
            ])
            .as_array()
            .unwrap(),
            &|name| if name == "n" { json!(1) } else { json!(0) },
        )
        .unwrap();
        assert_eq!(
            each.iter()
                .map(|item| item.name.as_str())
                .collect::<Vec<_>>(),
            vec![
                "login_first",
                "login_first",
                "login_second",
                "listen_once",
                "quit",
                "quit"
            ]
        );
        let block = expand_named(
            &items,
            &listed,
            json!([
                {"op":"duplicate","steps":"[login_first:login_second]","count_param":"n"}
            ])
            .as_array()
            .unwrap(),
            &|_| json!(2),
        )
        .unwrap();
        assert_eq!(
            block
                .iter()
                .map(|item| item.name.as_str())
                .collect::<Vec<_>>(),
            vec![
                "login_first",
                "login_second",
                "login_first",
                "login_second",
                "login_first",
                "login_second",
                "listen_once",
                "quit"
            ]
        );
        let zero = expand_named(
            &items,
            &listed,
            json!([
                {"op":"duplicate","steps":["quit"],"count_param":"n"}
            ])
            .as_array()
            .unwrap(),
            &|_| json!(0),
        )
        .unwrap();
        assert_eq!(zero.len(), items.len());
        let floated = expand_named(
            &items,
            &listed,
            json!([
                {"op":"duplicate","steps":["quit"],"count_param":"n"}
            ])
            .as_array()
            .unwrap(),
            &|_| json!(1.5),
        )
        .unwrap();
        assert_eq!(floated.len(), items.len());
    }

    #[test]
    fn illegal_overlap_and_expansion_cap_fail_and_field_may_share_a_step() {
        let items = trace();
        let error = check(
            &items,
            json!([
                {"op":"duplicate","steps":"[login_first:login_second]","count_param":"n"},
                {"op":"duplicate","steps":["login_second"],"count_param":"m"}
            ]),
        )
        .unwrap_err();
        assert!(error.contains("overlap"), "{error}");
        let error = check(&items, json!([
            {"op":"field","steps":["login_first"],"field":"username","suggestion":"a","evidence":"s:1"},
            {"op":"field","steps":["login_first"],"field":"username","suggestion":"b","evidence":"s:2"}
        ])).unwrap_err();
        assert!(error.contains("repeated"), "{error}");
        check(&items, json!([
            {"op":"field","steps":["login_first"],"field":"username","suggestion":"a","evidence":"s:1"},
            {"op":"duplicate","steps":["login_first"],"count_param":"n"}
        ])).unwrap();
        let error = selection(&json!("[quit:login_first]"), &names(&items)).unwrap_err();
        assert!(error.contains("reversed"), "{error}");
        let huge: Vec<Item> = (0..4096)
            .map(|index| Item {
                name: format!("s{index}"),
                step: "user".into(),
                send: true,
            })
            .collect();
        let listed = names(&huge);
        let error = expand_named(
            &huge,
            &listed,
            json!([
                {"op":"duplicate","steps":["s0"],"count_param":"n"}
            ])
            .as_array()
            .unwrap(),
            &|_| json!(1),
        )
        .unwrap_err();
        assert!(error.contains("4096"), "{error}");
        assert!(runtime_count(&json!(true)).unwrap() == 0);
        assert!(runtime_count(&json!(33)).is_err());
    }
}
