//! Shell script AST extraction via tree-sitter-bash.
//!
//! Extracts functions, variables, constants, and source/dot imports from
//! shell scripts (`.sh`, `.bash`). Shell has no class or type system,
//! so this parser only emits File, Function, Variable, Constant, and
//! Import entities.

use tree_sitter::{Node, Parser};

use scs_core::node_types::NodeType;

use super::{
    count_complexity, truncate_str, LanguageParser, ParsedEdge, ParsedEntity, RAW_TEXT_LIMIT,
    RAW_TEXT_SMALL_LIMIT,
};

/// UPPER_SNAKE_CASE detection for shell constants.
fn is_constant_name(name: &str) -> bool {
    !name.is_empty()
        && name
            .chars()
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
        && name.chars().next().unwrap().is_ascii_uppercase()
}

fn get_text<'a>(node: &Node, source: &'a [u8]) -> &'a str {
    std::str::from_utf8(&source[node.start_byte()..node.end_byte()]).unwrap_or("")
}

fn find_child_by_field<'a>(node: &Node<'a>, field: &str) -> Option<Node<'a>> {
    node.child_by_field_name(field)
}

fn find_child_by_kind<'a>(node: &Node<'a>, kind: &str) -> Option<Node<'a>> {
    let mut cursor = node.walk();
    let result = node.children(&mut cursor).find(|c| c.kind() == kind);
    result
}

/// Extract `#` comments preceding a node as a doc comment.
///
/// Walks backward through named siblings, collecting contiguous `comment`
/// nodes. Strips the leading `#` (and optional space) from each line.
fn get_doc_comment(node: &Node, source: &[u8]) -> String {
    let mut lines: Vec<String> = Vec::new();
    let mut sibling = node.prev_named_sibling();
    while let Some(s) = sibling {
        if s.kind() != "comment" {
            break;
        }
        let text = get_text(&s, source).trim();
        // Strip leading `#` and optional space.
        let stripped = text
            .strip_prefix('#')
            .unwrap_or(text)
            .strip_prefix(' ')
            .unwrap_or(text.strip_prefix('#').unwrap_or(text));
        lines.push(stripped.to_string());
        sibling = s.prev_named_sibling();
    }
    if lines.is_empty() {
        return String::new();
    }
    lines.reverse();
    lines.join("\n")
}

/// Extract function parameters from the body compound_statement.
///
/// Shell functions don't declare parameters — they use `$1`, `$2`, etc.
/// We scan the body for positional parameter references and return a
/// summary like `($1, $2)`.
fn get_function_params(node: &Node, source: &[u8]) -> String {
    let body = match find_child_by_field(node, "body") {
        Some(b) => b,
        None => return "()".to_string(),
    };

    let body_text = get_text(&body, source);
    let mut max_param: u32 = 0;

    // Scan for $1..$9 and ${N} patterns.
    let bytes = body_text.as_bytes();
    let len = bytes.len();
    let mut i = 0;
    while i < len {
        if bytes[i] == b'$' && i + 1 < len {
            if bytes[i + 1].is_ascii_digit() && bytes[i + 1] != b'0' {
                let digit = (bytes[i + 1] - b'0') as u32;
                if digit > max_param {
                    max_param = digit;
                }
            } else if bytes[i + 1] == b'{' {
                // Parse ${N}
                let start = i + 2;
                let mut end = start;
                while end < len && bytes[end].is_ascii_digit() {
                    end += 1;
                }
                if end > start && end < len && bytes[end] == b'}' {
                    if let Ok(n) = body_text[start..end].parse::<u32>() {
                        if n > 0 && n > max_param {
                            max_param = n;
                        }
                    }
                }
            }
        }
        i += 1;
    }

    if max_param == 0 {
        return "()".to_string();
    }

    let params: Vec<String> = (1..=max_param).map(|n| format!("${n}")).collect();
    format!("({})", params.join(", "))
}

pub struct BashParser {
    // tree-sitter parser is not Send, so we create per-call.
}

impl Default for BashParser {
    fn default() -> Self {
        Self::new()
    }
}

impl BashParser {
    pub fn new() -> Self {
        Self {}
    }
}

impl LanguageParser for BashParser {
    fn parse(&self, source: &str, file_path: &str) -> (Vec<ParsedEntity>, Vec<ParsedEdge>) {
        let mut parser = Parser::new();
        let language = tree_sitter_bash::LANGUAGE;
        parser
            .set_language(&language.into())
            .expect("failed to load bash grammar");

        let tree = match parser.parse(source, None) {
            Some(t) => t,
            None => return (vec![], vec![]),
        };

        let source_bytes = source.as_bytes();
        let root = tree.root_node();

        // Derive module qualified name from file path:
        // `scripts/deploy.sh` → `scripts.deploy`
        let module_name = {
            let dotted = file_path.replace('/', ".");
            if dotted.ends_with(".sh") {
                dotted[..dotted.len() - 3].to_string()
            } else if dotted.ends_with(".bash") {
                dotted[..dotted.len() - 5].to_string()
            } else {
                dotted
            }
        };

        let mut entities = Vec::new();
        let mut edges = Vec::new();

        // File entity.
        let file_raw = truncate_str(source, RAW_TEXT_LIMIT);
        entities.push(ParsedEntity {
            kind: NodeType::File,
            name: file_path.to_string(),
            qualified_name: module_name.clone(),
            start_line: 0,
            end_line: root.end_position().row,
            signature: String::new(),
            docstring: String::new(),
            raw_text: file_raw.to_string(),
            parent_qualified_name: None,
            bases: vec![],
            imports: vec![],
            cyclomatic_complexity: None,
        });

        // Walk top-level children.
        let mut cursor = root.walk();
        for child in root.children(&mut cursor) {
            match child.kind() {
                "function_definition" => {
                    let name = find_child_by_field(&child, "name")
                        .map(|n| get_text(&n, source_bytes).to_string())
                        .unwrap_or_default();

                    if name.is_empty() {
                        continue;
                    }

                    let qualified = format!("{module_name}.{name}");
                    let docstring = get_doc_comment(&child, source_bytes);
                    let params = get_function_params(&child, source_bytes);
                    let complexity = count_complexity(&child, source_bytes, "bash");

                    let raw = truncate_str(get_text(&child, source_bytes), RAW_TEXT_LIMIT);

                    entities.push(ParsedEntity {
                        kind: NodeType::Function,
                        name: name.clone(),
                        qualified_name: qualified.clone(),
                        start_line: child.start_position().row,
                        end_line: child.end_position().row,
                        signature: params,
                        docstring,
                        raw_text: raw.to_string(),
                        parent_qualified_name: Some(module_name.clone()),
                        bases: vec![],
                        imports: vec![],
                        cyclomatic_complexity: Some(complexity),
                    });

                    edges.push(ParsedEdge::new(module_name.clone(), qualified, "contains"));
                }

                "variable_assignment" => {
                    let name = find_child_by_field(&child, "name")
                        .map(|n| get_text(&n, source_bytes).to_string())
                        .unwrap_or_default();

                    if name.is_empty() {
                        continue;
                    }

                    let kind = if is_constant_name(&name) {
                        NodeType::Constant
                    } else {
                        NodeType::Variable
                    };

                    let qualified = format!("{module_name}.{name}");
                    let value_text = find_child_by_field(&child, "value")
                        .map(|v| get_text(&v, source_bytes).to_string())
                        .unwrap_or_default();

                    let raw = truncate_str(get_text(&child, source_bytes), RAW_TEXT_SMALL_LIMIT);

                    entities.push(ParsedEntity {
                        kind,
                        name: name.clone(),
                        qualified_name: qualified.clone(),
                        start_line: child.start_position().row,
                        end_line: child.end_position().row,
                        signature: value_text,
                        docstring: String::new(),
                        raw_text: raw.to_string(),
                        parent_qualified_name: Some(module_name.clone()),
                        bases: vec![],
                        imports: vec![],
                        cyclomatic_complexity: None,
                    });

                    edges.push(ParsedEdge::new(module_name.clone(), qualified, "contains"));
                }

                "command" => {
                    // Detect `source ./path` or `. ./path` commands.
                    let cmd_name = find_child_by_field(&child, "name")
                        .and_then(|n| find_child_by_kind(&n, "word"))
                        .or_else(|| find_child_by_field(&child, "name"))
                        .map(|n| get_text(&n, source_bytes))
                        .unwrap_or("");

                    let is_source = cmd_name == "source" || cmd_name == ".";
                    if !is_source {
                        continue;
                    }

                    // The path argument is the first `argument` field child or
                    // the second word child after the command name.
                    let target = {
                        let mut found = None;
                        let mut arg_cursor = child.walk();
                        for arg_child in child.children(&mut arg_cursor) {
                            if arg_child.kind() == "word"
                                || arg_child.kind() == "string"
                                || arg_child.kind() == "raw_string"
                                || arg_child.kind() == "concatenation"
                            {
                                let text = get_text(&arg_child, source_bytes);
                                if text != "source" && text != "." {
                                    found = Some(text.to_string());
                                    break;
                                }
                            }
                        }
                        found
                    };

                    let target = match target {
                        Some(t) => t,
                        None => continue,
                    };

                    let import_name = target.trim_matches('"').trim_matches('\'').to_string();
                    let qualified = format!("{module_name}.{import_name}");

                    entities.push(ParsedEntity {
                        kind: NodeType::Import,
                        name: import_name.clone(),
                        qualified_name: qualified.clone(),
                        start_line: child.start_position().row,
                        end_line: child.end_position().row,
                        signature: String::new(),
                        docstring: String::new(),
                        raw_text: get_text(&child, source_bytes).to_string(),
                        parent_qualified_name: Some(module_name.clone()),
                        bases: vec![],
                        imports: vec![import_name.clone()],
                        cyclomatic_complexity: None,
                    });

                    edges.push(ParsedEdge::new(module_name.clone(), import_name, "imports"));
                }

                _ => {}
            }
        }

        (entities, edges)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(source: &str) -> (Vec<ParsedEntity>, Vec<ParsedEdge>) {
        let parser = BashParser::new();
        parser.parse(source, "scripts/deploy.sh")
    }

    #[test]
    fn file_entity_created() {
        let (entities, _) = parse("#!/bin/bash\necho hello\n");
        let file = entities.iter().find(|e| e.kind == NodeType::File).unwrap();
        assert_eq!(file.qualified_name, "scripts.deploy");
        assert_eq!(file.name, "scripts/deploy.sh");
    }

    #[test]
    fn parses_function() {
        let source = r#"
function deploy() {
    echo "deploying"
}
"#;
        let (entities, edges) = parse(source);
        let func = entities
            .iter()
            .find(|e| e.kind == NodeType::Function && e.name == "deploy")
            .expect("should find deploy function");

        assert_eq!(func.qualified_name, "scripts.deploy.deploy");
        assert!(func.cyclomatic_complexity.is_some());
        assert_eq!(func.cyclomatic_complexity.unwrap(), 1); // no branching

        // contains edge
        let edge = edges
            .iter()
            .find(|e| e.relationship == "contains" && e.target_qualified_name.ends_with("deploy"))
            .expect("should have contains edge");
        assert_eq!(edge.source_qualified_name, "scripts.deploy");
    }

    #[test]
    fn parses_function_shorthand() {
        // Shell also supports `name() { ... }` without the `function` keyword.
        let source = r#"
build() {
    make all
}
"#;
        let (entities, _) = parse(source);
        let func = entities
            .iter()
            .find(|e| e.kind == NodeType::Function && e.name == "build")
            .expect("should find build function");
        assert_eq!(func.qualified_name, "scripts.deploy.build");
    }

    #[test]
    fn parses_variable_and_constant() {
        let source = r#"
MAX_RETRIES=5
base_dir="/opt/app"
"#;
        let (entities, _) = parse(source);

        let constant = entities
            .iter()
            .find(|e| e.kind == NodeType::Constant && e.name == "MAX_RETRIES")
            .expect("UPPER_SNAKE_CASE should be Constant");
        assert_eq!(constant.signature, "5");

        let variable = entities
            .iter()
            .find(|e| e.kind == NodeType::Variable && e.name == "base_dir")
            .expect("lowercase should be Variable");
        assert!(variable.signature.contains("/opt/app"));
    }

    #[test]
    fn parses_source_import() {
        let source = r#"
source ./lib/utils.sh
. ./lib/config.sh
"#;
        let (entities, edges) = parse(source);

        let imports: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Import)
            .collect();
        assert_eq!(imports.len(), 2, "should find 2 imports");

        assert!(imports.iter().any(|i| i.name == "./lib/utils.sh"));
        assert!(imports.iter().any(|i| i.name == "./lib/config.sh"));

        // imports edges
        let import_edges: Vec<_> = edges
            .iter()
            .filter(|e| e.relationship == "imports")
            .collect();
        assert_eq!(import_edges.len(), 2);
    }

    #[test]
    fn doc_comments_extracted() {
        let source = r#"
# Deploy the application to production.
# Requires SSH access.
function deploy() {
    echo "deploying"
}
"#;
        let (entities, _) = parse(source);
        let func = entities
            .iter()
            .find(|e| e.kind == NodeType::Function && e.name == "deploy")
            .unwrap();

        assert!(func.docstring.contains("Deploy the application"));
        assert!(func.docstring.contains("Requires SSH access"));
    }

    #[test]
    fn cyclomatic_complexity() {
        let source = r#"
function check() {
    if [ "$1" = "yes" ]; then
        for f in *.txt; do
            echo "$f"
        done
    fi
}
"#;
        let (entities, _) = parse(source);
        let func = entities
            .iter()
            .find(|e| e.kind == NodeType::Function && e.name == "check")
            .unwrap();

        // 1 (baseline) + 1 (if) + 1 (for) = 3
        assert_eq!(func.cyclomatic_complexity, Some(3));
    }

    #[test]
    fn function_params_detected() {
        let source = r#"
function greet() {
    echo "Hello, $1! You are $2 years old."
}
"#;
        let (entities, _) = parse(source);
        let func = entities
            .iter()
            .find(|e| e.kind == NodeType::Function && e.name == "greet")
            .unwrap();

        assert_eq!(func.signature, "($1, $2)");
    }

    #[test]
    fn bash_extension_handled() {
        let parser = BashParser::new();
        let (entities, _) = parser.parse("#!/bin/bash\n", "scripts/run.bash");
        let file = entities.iter().find(|e| e.kind == NodeType::File).unwrap();
        assert_eq!(file.qualified_name, "scripts.run");
    }
}
