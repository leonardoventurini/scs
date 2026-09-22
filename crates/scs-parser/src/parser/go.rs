//! Go AST extraction via tree-sitter.
//!
//! Go packages span files, so symbol identities use the repository-relative
//! directory plus the declared package. The parser remains syntax-only: it
//! records embedding but never guesses implicit interface satisfaction.

use std::collections::HashSet;

use tree_sitter::{Node, Parser};

use scs_core::node_types::NodeType;

use super::{
    count_complexity, truncate_str, LanguageParser, ParsedEdge, ParsedEntity, RAW_TEXT_LIMIT,
    RAW_TEXT_SMALL_LIMIT,
};

const GO_BUILTINS: &[&str] = &[
    "append", "cap", "clear", "close", "complex", "copy", "delete", "imag", "len", "make", "max",
    "min", "new", "panic", "print", "println", "real", "recover",
];

#[derive(Default)]
pub struct GoParser;

impl GoParser {
    pub fn new() -> Self {
        Self
    }

    fn walk_top_level(
        &self,
        root: Node,
        source: &[u8],
        file_qualified: &str,
        package_qualified: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
    ) {
        let mut cursor = root.walk();
        for node in root.named_children(&mut cursor) {
            match node.kind() {
                "import_declaration" => self.handle_imports(
                    node,
                    source,
                    file_qualified,
                    package_qualified,
                    entities,
                    edges,
                ),
                "type_declaration" => {
                    for_each_descendant(node, "type_spec", |spec| {
                        self.handle_type(spec, source, package_qualified, entities, edges)
                    });
                    for_each_descendant(node, "type_alias", |alias| {
                        self.handle_alias(alias, source, package_qualified, entities, edges)
                    });
                }
                "function_declaration" => {
                    self.handle_callable(node, source, package_qualified, None, entities, edges)
                }
                "method_declaration" => {
                    let receiver = node
                        .child_by_field_name("receiver")
                        .map(|receiver| normalized_receiver(receiver, source))
                        .unwrap_or_default();
                    self.handle_callable(
                        node,
                        source,
                        package_qualified,
                        Some(receiver),
                        entities,
                        edges,
                    );
                }
                "const_declaration" => for_each_descendant(node, "const_spec", |spec| {
                    self.handle_values(
                        spec,
                        source,
                        package_qualified,
                        NodeType::Constant,
                        entities,
                        edges,
                    )
                }),
                "var_declaration" => for_each_descendant(node, "var_spec", |spec| {
                    self.handle_values(
                        spec,
                        source,
                        package_qualified,
                        NodeType::Variable,
                        entities,
                        edges,
                    )
                }),
                _ => {}
            }
        }
    }

    fn handle_imports(
        &self,
        declaration: Node,
        source: &[u8],
        file_qualified: &str,
        package_qualified: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
    ) {
        for_each_descendant(declaration, "import_spec", |spec| {
            let Some(path_node) = spec.child_by_field_name("path") else {
                return;
            };
            let path = text(path_node, source).trim_matches(['"', '`']).to_string();
            let alias = spec
                .child_by_field_name("name")
                .map(|name| text(name, source).to_string());
            let name = alias
                .clone()
                .unwrap_or_else(|| path.rsplit('/').next().unwrap_or(&path).to_string());
            let qualified = format!("{file_qualified}.import.{name}");

            entities.push(entity(
                NodeType::Import,
                name,
                qualified.clone(),
                spec,
                source,
                Some(package_qualified.to_string()),
                text(spec, source).to_string(),
                String::new(),
                vec![],
                vec![path],
                None,
            ));
            edges.push(ParsedEdge::new(
                package_qualified.to_string(),
                qualified.clone(),
                "contains",
            ));
            edges.push(ParsedEdge::new(
                package_qualified.to_string(),
                qualified,
                "imports",
            ));
        });
    }

    fn handle_type(
        &self,
        spec: Node,
        source: &[u8],
        package_qualified: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
    ) {
        let Some(name_node) = spec.child_by_field_name("name") else {
            return;
        };
        let name = text(name_node, source).to_string();
        let qualified = format!("{package_qualified}.{name}");
        let type_node = spec.child_by_field_name("type");
        let mut bases = Vec::new();

        if let Some(container) = type_node {
            match container.kind() {
                "struct_type" => for_each_descendant(container, "field_declaration", |field| {
                    self.handle_field(field, source, &qualified, entities, edges, &mut bases)
                }),
                "interface_type" => {
                    for_each_descendant(container, "method_elem", |method| {
                        self.handle_interface_method(method, source, &qualified, entities, edges)
                    });
                    for_each_descendant(container, "type_elem", |embedded| {
                        self.record_embedding(embedded, source, &qualified, edges, &mut bases)
                    });
                }
                _ => {}
            }
        }

        entities.push(entity(
            NodeType::Class,
            name,
            qualified.clone(),
            spec,
            source,
            Some(package_qualified.to_string()),
            signature_before_body(spec, source),
            doc_comment(spec, source),
            bases,
            vec![],
            None,
        ));
        edges.push(ParsedEdge::new(
            package_qualified.to_string(),
            qualified,
            "contains",
        ));
    }

    fn handle_field(
        &self,
        field: Node,
        source: &[u8],
        owner: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        bases: &mut Vec<String>,
    ) {
        let names = children_by_field(field, "name");
        if names.is_empty() {
            self.record_embedding(field, source, owner, edges, bases);
            return;
        }

        for name_node in names {
            let name = text(name_node, source).to_string();
            let qualified = format!("{owner}.{name}");
            entities.push(entity(
                NodeType::Variable,
                name,
                qualified.clone(),
                field,
                source,
                Some(owner.to_string()),
                text(field, source).to_string(),
                doc_comment(field, source),
                vec![],
                vec![],
                None,
            ));
            edges.push(ParsedEdge::new(owner.to_string(), qualified, "contains"));
        }
    }

    fn record_embedding(
        &self,
        node: Node,
        source: &[u8],
        owner: &str,
        edges: &mut Vec<ParsedEdge>,
        bases: &mut Vec<String>,
    ) {
        let Some(type_node) = node
            .child_by_field_name("type")
            .or_else(|| node.named_child(0))
        else {
            return;
        };
        let embedded = normalize_type_name(text(type_node, source));
        if embedded.is_empty() {
            return;
        }
        bases.push(embedded.clone());
        edges.push(ParsedEdge::new(owner.to_string(), embedded, "inherits"));
    }

    fn handle_interface_method(
        &self,
        method: Node,
        source: &[u8],
        owner: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
    ) {
        let Some(name_node) = method.child_by_field_name("name") else {
            return;
        };
        let name = text(name_node, source).to_string();
        let qualified = format!("{owner}.{name}");
        entities.push(entity(
            NodeType::Method,
            name,
            qualified.clone(),
            method,
            source,
            Some(owner.to_string()),
            text(method, source).to_string(),
            doc_comment(method, source),
            vec![],
            vec![],
            None,
        ));
        edges.push(ParsedEdge::new(owner.to_string(), qualified, "contains"));
    }

    fn handle_alias(
        &self,
        alias: Node,
        source: &[u8],
        package_qualified: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
    ) {
        let Some(name_node) = alias.child_by_field_name("name") else {
            return;
        };
        let name = text(name_node, source).to_string();
        let qualified = format!("{package_qualified}.{name}");
        entities.push(entity(
            NodeType::TypeAlias,
            name,
            qualified.clone(),
            alias,
            source,
            Some(package_qualified.to_string()),
            text(alias, source).to_string(),
            doc_comment(alias, source),
            vec![],
            vec![],
            None,
        ));
        edges.push(ParsedEdge::new(
            package_qualified.to_string(),
            qualified,
            "contains",
        ));
    }

    fn handle_callable(
        &self,
        callable: Node,
        source: &[u8],
        package_qualified: &str,
        receiver: Option<String>,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
    ) {
        let Some(name_node) = callable.child_by_field_name("name") else {
            return;
        };
        let name = text(name_node, source).to_string();
        let (kind, owner) = match receiver.filter(|value| !value.is_empty()) {
            Some(receiver) => (NodeType::Method, format!("{package_qualified}.{receiver}")),
            None => (NodeType::Function, package_qualified.to_string()),
        };
        let qualified = format!("{owner}.{name}");
        let complexity = callable
            .child_by_field_name("body")
            .map(|body| count_complexity(&body, source, "go"))
            .or(Some(1));

        entities.push(entity(
            kind,
            name,
            qualified.clone(),
            callable,
            source,
            Some(owner.clone()),
            signature_before_body(callable, source),
            doc_comment(callable, source),
            vec![],
            vec![],
            complexity,
        ));
        edges.push(ParsedEdge::new(owner, qualified.clone(), "contains"));

        if let Some(body) = callable.child_by_field_name("body") {
            for (callee, local) in go_calls(body, source) {
                let target = if local {
                    format!("{package_qualified}.{callee}")
                } else {
                    callee
                };
                edges.push(ParsedEdge::new(qualified.clone(), target, "calls"));
            }
        }
    }

    fn handle_values(
        &self,
        spec: Node,
        source: &[u8],
        package_qualified: &str,
        kind: NodeType,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
    ) {
        let signature = value_spec_signature(spec, source);

        for name_node in children_by_field(spec, "name") {
            let name = text(name_node, source).to_string();
            let qualified = format!("{package_qualified}.{name}");
            entities.push(entity(
                kind,
                name,
                qualified.clone(),
                spec,
                source,
                Some(package_qualified.to_string()),
                signature.clone(),
                doc_comment(spec, source),
                vec![],
                vec![],
                None,
            ));
            edges.push(ParsedEdge::new(
                package_qualified.to_string(),
                qualified,
                "contains",
            ));
        }
    }
}

impl LanguageParser for GoParser {
    fn parse(&self, source: &str, file_path: &str) -> (Vec<ParsedEntity>, Vec<ParsedEdge>) {
        let mut parser = Parser::new();
        parser
            .set_language(&tree_sitter_go::LANGUAGE.into())
            .expect("failed to load Go grammar");
        let Some(tree) = parser.parse(source, None) else {
            return (vec![], vec![]);
        };
        let source_bytes = source.as_bytes();
        let root = tree.root_node();
        let Some(package_node) = first_descendant(root, "package_clause") else {
            return (vec![], vec![]);
        };
        let Some(package_name_node) = package_node.named_child(0) else {
            return (vec![], vec![]);
        };
        let package_name = text(package_name_node, source_bytes).to_string();
        let file_qualified = file_path
            .replace('/', ".")
            .strip_suffix(".go")
            .unwrap_or(file_path)
            .to_string();
        let directory = file_path
            .rsplit_once('/')
            .map(|(dir, _)| dir.replace('/', "."));
        let package_qualified = directory
            .filter(|dir| !dir.is_empty())
            .map(|dir| format!("{dir}.{package_name}"))
            .unwrap_or(package_name.clone());

        let mut entities = vec![entity(
            NodeType::File,
            file_path.to_string(),
            file_qualified.clone(),
            root,
            source_bytes,
            None,
            String::new(),
            String::new(),
            vec![],
            vec![],
            None,
        )];
        entities.push(entity(
            NodeType::Module,
            package_name,
            package_qualified.clone(),
            package_node,
            source_bytes,
            Some(file_qualified.clone()),
            text(package_node, source_bytes).to_string(),
            doc_comment(package_node, source_bytes),
            vec![],
            vec![],
            None,
        ));
        let mut edges = vec![ParsedEdge::new(
            file_qualified.clone(),
            package_qualified.clone(),
            "contains",
        )];

        self.walk_top_level(
            root,
            source_bytes,
            &file_qualified,
            &package_qualified,
            &mut entities,
            &mut edges,
        );
        (entities, edges)
    }
}

#[allow(clippy::too_many_arguments)]
fn entity(
    kind: NodeType,
    name: String,
    qualified_name: String,
    node: Node,
    source: &[u8],
    parent_qualified_name: Option<String>,
    signature: String,
    docstring: String,
    bases: Vec<String>,
    imports: Vec<String>,
    cyclomatic_complexity: Option<u32>,
) -> ParsedEntity {
    let limit = match kind {
        NodeType::Class | NodeType::Function | NodeType::Method | NodeType::File => RAW_TEXT_LIMIT,
        _ => RAW_TEXT_SMALL_LIMIT,
    };
    ParsedEntity {
        kind,
        name,
        qualified_name,
        start_line: node.start_position().row,
        end_line: node.end_position().row,
        signature,
        docstring,
        raw_text: truncate_str(text(node, source), limit).to_string(),
        parent_qualified_name,
        bases,
        imports,
        cyclomatic_complexity,
    }
}

fn text<'a>(node: Node, source: &'a [u8]) -> &'a str {
    std::str::from_utf8(&source[node.byte_range()]).unwrap_or("")
}

fn children_by_field<'tree>(node: Node<'tree>, field: &str) -> Vec<Node<'tree>> {
    let Some(field_id) = node.language().field_id_for_name(field) else {
        return vec![];
    };
    let mut cursor = node.walk();
    node.children_by_field_id(field_id, &mut cursor).collect()
}

fn first_descendant<'tree>(node: Node<'tree>, kind: &str) -> Option<Node<'tree>> {
    if node.kind() == kind {
        return Some(node);
    }
    let mut cursor = node.walk();
    let result = node
        .named_children(&mut cursor)
        .find_map(|child| first_descendant(child, kind));
    result
}

fn for_each_descendant(node: Node, kind: &str, mut visit: impl FnMut(Node)) {
    fn walk(node: Node, kind: &str, visit: &mut impl FnMut(Node)) {
        if node.kind() == kind {
            visit(node);
            return;
        }
        let mut cursor = node.walk();
        for child in node.named_children(&mut cursor) {
            walk(child, kind, visit);
        }
    }
    walk(node, kind, &mut visit);
}

fn signature_before_body(node: Node, source: &[u8]) -> String {
    let end = node
        .child_by_field_name("body")
        .map(|body| body.start_byte())
        .unwrap_or(node.end_byte());
    std::str::from_utf8(&source[node.start_byte()..end])
        .unwrap_or("")
        .trim()
        .to_string()
}

fn value_spec_signature(spec: Node, source: &[u8]) -> String {
    let names = children_by_field(spec, "name")
        .into_iter()
        .map(|name| text(name, source))
        .collect::<Vec<_>>()
        .join(", ");
    let mut signature = names;

    if let Some(type_node) = spec.child_by_field_name("type") {
        signature.push(' ');
        signature.push_str(text(type_node, source).trim());
    }

    if let Some(values) = spec.child_by_field_name("value") {
        signature.push_str(" = ");
        signature.push_str(&compact_value_list(values, source));
    }

    signature
}

fn compact_value_list(values: Node, source: &[u8]) -> String {
    let mut cursor = values.walk();
    let expressions = values
        .named_children(&mut cursor)
        .map(|value| compact_value(value, source))
        .collect::<Vec<_>>();

    if expressions.is_empty() {
        text(values, source).trim().to_string()
    } else {
        expressions.join(", ")
    }
}

fn compact_value(value: Node, source: &[u8]) -> String {
    if value.kind() != "composite_literal" {
        return text(value, source).trim().to_string();
    }

    value
        .child_by_field_name("type")
        .map(|type_node| format!("{}{{…}}", text(type_node, source).trim()))
        .unwrap_or_else(|| "{…}".to_string())
}

fn doc_comment(node: Node, source: &[u8]) -> String {
    let Some(comment) = node.prev_named_sibling() else {
        return String::new();
    };
    if comment.kind() != "comment" || comment.end_position().row + 1 < node.start_position().row {
        return String::new();
    }
    text(comment, source)
        .lines()
        .map(|line| {
            line.trim()
                .trim_start_matches("//")
                .trim_start_matches("/*")
                .trim_end_matches("*/")
                .trim()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn normalized_receiver(receiver: Node, source: &[u8]) -> String {
    let raw = text(receiver, source);
    let type_text = raw
        .trim_matches(['(', ')'])
        .split_whitespace()
        .last()
        .unwrap_or("");
    normalize_type_name(type_text)
}

fn normalize_type_name(raw: &str) -> String {
    raw.trim()
        .trim_start_matches('*')
        .split('[')
        .next()
        .unwrap_or("")
        .rsplit('.')
        .next()
        .unwrap_or("")
        .to_string()
}

fn go_calls(node: Node, source: &[u8]) -> HashSet<(String, bool)> {
    fn walk(node: Node, source: &[u8], calls: &mut HashSet<(String, bool)>) {
        if node.kind() == "call_expression" {
            if let Some(function) = node.child_by_field_name("function") {
                let extracted = match function.kind() {
                    "identifier" => Some((text(function, source).to_string(), true)),
                    "selector_expression" => function
                        .child_by_field_name("field")
                        .map(|field| (text(field, source).to_string(), false)),
                    _ => None,
                };
                if let Some((name, local)) = extracted {
                    if !GO_BUILTINS.contains(&name.as_str()) {
                        calls.insert((name, local));
                    }
                }
            }
        }

        let mut cursor = node.walk();
        for child in node.named_children(&mut cursor) {
            walk(child, source, calls);
        }
    }

    let mut calls = HashSet::new();
    walk(node, source, &mut calls);
    calls
}

#[cfg(test)]
mod tests {
    use super::*;

    const REPRESENTATIVE_GO: &str = r#"
// Package model owns graph values.
package model

import (
    "context"
    json "encoding/json"
    _ "net/http/pprof"
    . "math"
)

// Entity is stored in the graph.
type Entity[T any] struct {
    ID string
    Payload T
    Metadata
}

type Reader interface {
    Read(context.Context) error
    Closer
}

type Identifier = string
type Status string

const (
    Ready Status = "ready"
    Pending = "pending"
)

var First, Second int

func Load[T any](ctx context.Context) (*Entity[T], error) {
    if ctx == nil || len("x") == 0 {
        return nil, nil
    }
    for i := 0; i < 1; i++ {
        json.Marshal(i)
    }
    return helper(), nil
}

func (e *Entity[T]) Save() error {
    return persist(e)
}
"#;

    #[test]
    fn extracts_full_go_surface() {
        let (entities, edges) =
            GoParser::new().parse(REPRESENTATIVE_GO, "internal/model/entity.go");

        assert!(entities
            .iter()
            .any(|e| e.kind == NodeType::Module && e.qualified_name == "internal.model.model"));
        assert!(entities
            .iter()
            .any(|e| e.kind == NodeType::Class && e.name == "Entity" && e.bases == ["Metadata"]));
        assert!(entities
            .iter()
            .any(|e| e.kind == NodeType::Class && e.name == "Reader" && e.bases == ["Closer"]));
        assert!(entities
            .iter()
            .any(|e| e.kind == NodeType::TypeAlias && e.name == "Identifier"));
        assert!(entities
            .iter()
            .any(|e| e.kind == NodeType::Class && e.name == "Status"));
        assert_eq!(
            entities
                .iter()
                .filter(|e| e.kind == NodeType::Import)
                .count(),
            4
        );
        let import_names: HashSet<_> = entities
            .iter()
            .filter(|entity| entity.kind == NodeType::Import)
            .map(|entity| entity.name.as_str())
            .collect();
        assert_eq!(import_names, HashSet::from(["context", "json", "_", "."]));
        assert!(entities
            .iter()
            .any(|e| e.kind == NodeType::Variable && e.qualified_name.ends_with("Entity.ID")));
        assert!(entities
            .iter()
            .any(|e| e.kind == NodeType::Method && e.qualified_name.ends_with("Entity.Save")));
        assert!(entities
            .iter()
            .any(|e| e.kind == NodeType::Method && e.qualified_name.ends_with("Reader.Read")));
        assert!(entities
            .iter()
            .any(|e| e.kind == NodeType::Constant && e.name == "Pending"));
        assert_eq!(
            entities
                .iter()
                .filter(
                    |e| e.kind == NodeType::Variable && (e.name == "First" || e.name == "Second")
                )
                .count(),
            2
        );

        let load = entities.iter().find(|e| e.name == "Load").unwrap();
        assert_eq!(load.cyclomatic_complexity, Some(4));
        assert!(load.signature.contains("[T any]"));
        assert!(edges
            .iter()
            .any(|e| e.relationship == "calls" && e.target_qualified_name == "Marshal"));
        assert!(edges.iter().any(|e| e.relationship == "calls"
            && e.target_qualified_name == "internal.model.model.helper"));
        assert!(!edges
            .iter()
            .any(|e| e.relationship == "calls" && e.target_qualified_name == "len"));
        assert!(edges
            .iter()
            .any(|e| e.relationship == "inherits" && e.target_qualified_name == "Metadata"));
        assert!(!edges.iter().any(|e| e.relationship == "implements"));
    }

    #[test]
    fn package_identity_is_shared_by_files_and_isolated_by_directory() {
        let parser = GoParser::new();
        let (first, _) = parser.parse("package api\nfunc First() {}", "a/api/first.go");
        let (second, _) = parser.parse("package api\nfunc Second() {}", "a/api/second.go");
        let (other, _) = parser.parse("package api\nfunc First() {}", "b/api/first.go");

        assert!(first.iter().any(|e| e.qualified_name == "a.api.api.First"));
        assert!(second
            .iter()
            .any(|e| e.qualified_name == "a.api.api.Second"));
        assert!(other.iter().any(|e| e.qualified_name == "b.api.api.First"));
    }

    #[test]
    fn malformed_and_unicode_source_is_safe() {
        let source = format!("package main\n// {}\nfunc Broken( {{", "🔥".repeat(600));
        let (entities, _) = GoParser::new().parse(&source, "main.go");

        assert!(!entities.is_empty());
        assert!(entities
            .iter()
            .all(|entity| entity.raw_text.is_char_boundary(entity.raw_text.len())));
        assert!(entities
            .iter()
            .all(|entity| entity.raw_text.len() <= RAW_TEXT_LIMIT));
    }

    #[test]
    fn compacts_large_package_value_signatures() {
        let entries = (0..10_000)
            .map(|index| format!("{index}: {{Name: \"operation-{index}\"}},"))
            .collect::<Vec<_>>()
            .join("\n");
        let source = format!(
            "package session\n\nvar OperationRegistry = map[OperationID]*OpMeta{{\n{entries}\n}}\n"
        );

        let (entities, _) = GoParser::new().parse(&source, "internal/session/registry.go");
        let registry = entities
            .iter()
            .find(|entity| entity.name == "OperationRegistry")
            .expect("OperationRegistry variable should be extracted");

        assert_eq!(
            registry.signature,
            "OperationRegistry = map[OperationID]*OpMeta{…}"
        );
        assert!(registry.signature.len() < RAW_TEXT_SMALL_LIMIT);
        assert!(registry.raw_text.len() <= RAW_TEXT_SMALL_LIMIT);
    }

    #[test]
    fn preserves_explicit_types_values_and_multi_name_declarations() {
        let source = r#"package values

const Ready Status = "ready"
var First, Second int
var Lower, Upper = 1, calculateUpper()
"#;

        let (entities, _) = GoParser::new().parse(source, "values.go");

        for name in ["First", "Second"] {
            let entity = entities
                .iter()
                .find(|entity| entity.name == name)
                .expect("multi-name variable should be extracted");
            assert_eq!(entity.signature, "First, Second int");
        }

        for name in ["Lower", "Upper"] {
            let entity = entities
                .iter()
                .find(|entity| entity.name == name)
                .expect("multi-name initialized variable should be extracted");
            assert_eq!(entity.signature, "Lower, Upper = 1, calculateUpper()");
        }

        let ready = entities
            .iter()
            .find(|entity| entity.name == "Ready")
            .expect("typed constant should be extracted");
        assert_eq!(ready.signature, "Ready Status = \"ready\"");
    }
}
