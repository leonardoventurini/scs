//! Rust AST extraction via tree-sitter.
//!
//! Extracts structs, enums, traits, functions, methods (via impl blocks),
//! use statements, constants, type aliases, and static variables from Rust
//! source files. Uses a scope stack for qualified names — impl blocks push
//! the implementing type onto the scope so methods get proper nesting.

use std::collections::HashSet;

use tree_sitter::{Node, Parser};

use scs_core::node_types::NodeType;

use super::{count_complexity, extract_calls, LanguageParser, ParsedEdge, ParsedEntity};

/// Built-in names to exclude from call-graph edges (Rust).
const RUST_BUILTIN_CALLS: &[&str] = &[
    "println",
    "eprintln",
    "print",
    "eprint",
    "format",
    "write",
    "writeln",
    "vec",
    "todo",
    "unimplemented",
    "unreachable",
    "assert",
    "assert_eq",
    "assert_ne",
    "panic",
    "dbg",
    "cfg",
    "include",
    "include_str",
    "include_bytes",
    "env",
    "concat",
    "stringify",
    "file",
    "line",
    "column",
    "module_path",
    "compile_error",
    "option_env",
    "matches",
    "Ok",
    "Err",
    "Some",
    "None",
    "Box",
    "Arc",
    "Rc",
    "Vec",
    "String",
    "HashMap",
    "HashSet",
    "BTreeMap",
    "BTreeSet",
];

/// UPPER_SNAKE_CASE detection for Rust constants.
fn is_constant_name(name: &str) -> bool {
    !name.is_empty()
        && name.chars().next().unwrap().is_ascii_uppercase()
        && name
            .chars()
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
}

fn get_text<'a>(node: &Node, source: &'a [u8]) -> &'a str {
    std::str::from_utf8(&source[node.start_byte()..node.end_byte()]).unwrap_or("")
}

fn find_child_by_kind<'a>(node: &Node<'a>, kind: &str) -> Option<Node<'a>> {
    let mut cursor = node.walk();
    let result = node.children(&mut cursor).find(|c| c.kind() == kind);
    result
}

/// Extract a doc comment (`///` or `//!`) preceding a node.
///
/// Tree-sitter for Rust represents doc comments as `line_comment` nodes
/// appearing as siblings before the item. We walk backwards to collect them.
fn get_doc_comment(node: &Node, source: &[u8]) -> String {
    let mut lines: Vec<String> = Vec::new();
    let mut sibling = node.prev_named_sibling();
    while let Some(s) = sibling {
        if s.kind() != "line_comment" {
            break;
        }
        let text = get_text(&s, source).trim().to_string();
        if text.starts_with("///") || text.starts_with("//!") {
            lines.push(text[3..].trim().to_string());
        }
        sibling = s.prev_named_sibling();
    }
    if lines.is_empty() {
        return String::new();
    }
    lines.reverse();
    lines.join("\n")
}

/// Extract the parameter list from a function_item.
fn get_parameters(node: &Node, source: &[u8]) -> String {
    find_child_by_kind(node, "parameters")
        .map(|p| get_text(&p, source).to_string())
        .unwrap_or_else(|| "()".to_string())
}

/// Extract the return type from a function (text after `→`).
fn get_return_type(node: &Node, source: &[u8]) -> String {
    let mut found_arrow = false;
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "->" {
            found_arrow = true;
            continue;
        }
        if found_arrow && child.kind() != "block" {
            return get_text(&child, source).to_string();
        }
    }
    String::new()
}

/// Expand a Rust use path that may contain brace groups.
///
/// Examples:
///   `"std::io"` → `["std::io"]`
///   `"serde::{Serialize, Deserialize}"` → `["serde::Serialize", "serde::Deserialize"]`
fn expand_use_path(path: &str) -> Vec<String> {
    let brace_start = match path.find('{') {
        Some(pos) => pos,
        None => return vec![path.trim().to_string()],
    };
    let brace_end = match path.rfind('}') {
        Some(pos) => pos,
        None => return vec![path.trim().to_string()],
    };

    let prefix = path[..brace_start]
        .trim_end_matches(':')
        .trim_end_matches(':');
    let inner = &path[brace_start + 1..brace_end];

    inner
        .split(',')
        .filter_map(|item| {
            let item = item.trim();
            if item.is_empty() {
                return None;
            }
            if item == "self" {
                Some(prefix.to_string())
            } else if prefix.is_empty() {
                Some(item.to_string())
            } else {
                Some(format!("{prefix}::{item}"))
            }
        })
        .collect()
}

/// Parse Rust source files using tree-sitter.
///
/// Walks the CST to extract type definitions, functions, impl blocks,
/// use declarations, and module-level constants. Impl blocks push their
/// type onto the scope stack so methods are correctly nested.
#[derive(Default)]
pub struct RustParser;

impl RustParser {
    pub fn new() -> Self {
        Self
    }

    fn walk(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            match child.kind() {
                "struct_item" | "enum_item" | "trait_item" => {
                    self.handle_type_def(child, source, module_name, entities, edges, scope_stack);
                }
                "impl_item" => {
                    self.handle_impl(child, source, module_name, entities, edges, scope_stack);
                }
                "function_item" | "function_signature_item" => {
                    self.handle_function(child, source, module_name, entities, edges, scope_stack);
                }
                "use_declaration" => {
                    self.handle_use(child, source, module_name, entities, edges);
                }
                "const_item" | "static_item" => {
                    self.handle_const(child, source, entities, edges, scope_stack);
                }
                "type_item" => {
                    self.handle_type_alias(child, source, entities, edges, scope_stack);
                }
                "mod_item" => {
                    self.handle_mod(child, source, module_name, entities, edges, scope_stack);
                }
                _ => {}
            }
        }
    }

    fn handle_type_def(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let name = match find_child_by_kind(&node, "type_identifier") {
            Some(n) => get_text(&n, source).to_string(),
            None => return,
        };

        let qualified = format!("{}.{name}", scope_stack.join("."));
        let docstring = get_doc_comment(&node, source);

        // Extract trait supertraits.
        let mut bases = Vec::new();
        if node.kind() == "trait_item" {
            let mut cursor = node.walk();
            for child in node.children(&mut cursor) {
                if child.kind() == "trait_bounds" {
                    let mut sub_cursor = child.walk();
                    for sub in child.children(&mut sub_cursor) {
                        if sub.kind() == "type_identifier" {
                            bases.push(get_text(&sub, source).to_string());
                        }
                    }
                }
            }
        }

        let raw = get_text(&node, source);
        let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_LIMIT);

        entities.push(ParsedEntity {
            kind: NodeType::Class,
            name: name.clone(),
            qualified_name: qualified.clone(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            docstring,
            raw_text: raw_truncated.to_string(),
            parent_qualified_name: Some(scope_stack.join(".")),
            bases: bases.clone(),
            signature: String::new(),
            imports: Vec::new(),
            cyclomatic_complexity: None,
        });

        edges.push(ParsedEdge::new(
            scope_stack.join("."),
            qualified.clone(),
            "contains",
        ));

        for base in &bases {
            edges.push(ParsedEdge::new(qualified.clone(), base.clone(), "inherits"));
        }

        // Recurse into declaration_list for trait methods.
        if let Some(body) = find_child_by_kind(&node, "declaration_list") {
            let mut new_scope = scope_stack.to_vec();
            new_scope.push(name);
            let mut body_cursor = body.walk();
            for child in body.children(&mut body_cursor) {
                if child.kind() == "function_item" || child.kind() == "function_signature_item" {
                    self.handle_function(child, source, module_name, entities, edges, &new_scope);
                }
            }
        }
    }

    fn handle_impl(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let mut impl_type = String::new();
        let mut trait_name = String::new();
        let mut found_for = false;

        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            match child.kind() {
                "type_identifier" => {
                    impl_type = get_text(&child, source).to_string();
                }
                "for" => {
                    trait_name = impl_type.clone();
                    impl_type.clear();
                    found_for = true;
                }
                "generic_type" => {
                    if let Some(ident) = find_child_by_kind(&child, "type_identifier") {
                        if found_for || trait_name.is_empty() {
                            impl_type = get_text(&ident, source).to_string();
                        }
                    }
                }
                _ => {}
            }
        }

        if impl_type.is_empty() {
            return;
        }

        // IMPLEMENTS edge: `impl Trait for Type`.
        if !trait_name.is_empty() {
            let type_qualified = format!("{}.{impl_type}", scope_stack.join("."));
            edges.push(ParsedEdge::new(type_qualified, trait_name, "implements"));
        }

        // Recurse into the impl body.
        if let Some(body) = find_child_by_kind(&node, "declaration_list") {
            let mut new_scope = scope_stack.to_vec();
            new_scope.push(impl_type);
            let mut body_cursor = body.walk();
            for child in body.children(&mut body_cursor) {
                match child.kind() {
                    "function_item" => {
                        self.handle_function(
                            child,
                            source,
                            module_name,
                            entities,
                            edges,
                            &new_scope,
                        );
                    }
                    "const_item" | "static_item" => {
                        self.handle_const(child, source, entities, edges, &new_scope);
                    }
                    "type_item" => {
                        self.handle_type_alias(child, source, entities, edges, &new_scope);
                    }
                    _ => {}
                }
            }
        }
    }

    fn handle_function(
        &self,
        node: Node,
        source: &[u8],
        _module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let name = match find_child_by_kind(&node, "identifier") {
            Some(n) => get_text(&n, source).to_string(),
            None => return,
        };

        let qualified = format!("{}.{name}", scope_stack.join("."));
        let is_method = scope_stack.len() > 1;
        let kind = if is_method {
            NodeType::Method
        } else {
            NodeType::Function
        };

        let params = get_parameters(&node, source);
        let return_type = get_return_type(&node, source);
        let mut signature = params;
        if !return_type.is_empty() {
            signature = format!("{signature} -> {return_type}");
        }

        let docstring = get_doc_comment(&node, source);
        let raw = get_text(&node, source);
        let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_LIMIT);
        let complexity = count_complexity(&node, source, "rust");

        entities.push(ParsedEntity {
            kind,
            name,
            qualified_name: qualified.clone(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            signature,
            docstring,
            raw_text: raw_truncated.to_string(),
            parent_qualified_name: Some(scope_stack.join(".")),
            bases: Vec::new(),
            imports: Vec::new(),
            cyclomatic_complexity: Some(complexity),
        });

        edges.push(ParsedEdge::new(
            scope_stack.join("."),
            qualified.clone(),
            "contains",
        ));

        // CALLS edges from this function/method to callees in its body.
        {
            let blocklist: HashSet<&str> = RUST_BUILTIN_CALLS.iter().copied().collect();
            let callees = extract_calls(&node, source, "rust", &blocklist);
            for callee in callees {
                edges.push(ParsedEdge::new(qualified.clone(), callee, "calls"));
            }
        }
    }

    fn handle_use(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
    ) {
        let text = get_text(&node, source);
        // Strip `use ` / `pub use ` prefix and trailing `;`.
        let use_path = text
            .trim_start_matches("pub ")
            .trim_start_matches("use ")
            .trim_end_matches(';')
            .trim();

        let imports = expand_use_path(use_path);

        for imp in imports {
            let name = imp.rsplit("::").next().unwrap_or(&imp).to_string();
            let qualified = format!("{module_name}.import.{imp}");
            entities.push(ParsedEntity {
                kind: NodeType::Import,
                name,
                qualified_name: qualified.clone(),
                start_line: node.start_position().row,
                end_line: node.end_position().row,
                imports: vec![imp.clone()],
                signature: String::new(),
                docstring: String::new(),
                raw_text: String::new(),
                parent_qualified_name: Some(module_name.to_string()),
                bases: Vec::new(),
                cyclomatic_complexity: None,
            });
            // CONTAINS edge from file → import node.
            edges.push(ParsedEdge::new(
                module_name.to_string(),
                qualified,
                "contains",
            ));
            edges.push(ParsedEdge::new(module_name.to_string(), imp, "imports"));
        }
    }

    fn handle_const(
        &self,
        node: Node,
        source: &[u8],
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let name = match find_child_by_kind(&node, "identifier") {
            Some(n) => get_text(&n, source).to_string(),
            None => return,
        };
        if name.len() < 2 {
            return;
        }

        let kind = if is_constant_name(&name) {
            NodeType::Constant
        } else {
            NodeType::Variable
        };

        let qualified = format!("{}.{name}", scope_stack.join("."));
        let raw = get_text(&node, source);
        let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_SMALL_LIMIT);

        entities.push(ParsedEntity {
            kind,
            name,
            qualified_name: qualified.clone(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            signature: raw_truncated.to_string(),
            raw_text: raw_truncated.to_string(),
            parent_qualified_name: Some(scope_stack.join(".")),
            docstring: String::new(),
            bases: Vec::new(),
            imports: Vec::new(),
            cyclomatic_complexity: None,
        });

        edges.push(ParsedEdge::new(
            scope_stack.join("."),
            qualified,
            "contains",
        ));
    }

    fn handle_type_alias(
        &self,
        node: Node,
        source: &[u8],
        entities: &mut Vec<ParsedEntity>,
        _edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let name = match find_child_by_kind(&node, "type_identifier") {
            Some(n) => get_text(&n, source).to_string(),
            None => return,
        };

        let qualified = format!("{}.{name}", scope_stack.join("."));
        let raw = get_text(&node, source);
        let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_SMALL_LIMIT);

        entities.push(ParsedEntity {
            kind: NodeType::TypeAlias,
            name,
            qualified_name: qualified,
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            raw_text: raw_truncated.to_string(),
            parent_qualified_name: Some(scope_stack.join(".")),
            signature: String::new(),
            docstring: String::new(),
            bases: Vec::new(),
            imports: Vec::new(),
            cyclomatic_complexity: None,
        });
    }

    fn handle_mod(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let name = match find_child_by_kind(&node, "identifier") {
            Some(n) => get_text(&n, source).to_string(),
            None => return,
        };

        // Only process inline modules (with a body), not `mod foo;` declarations.
        if let Some(body) = find_child_by_kind(&node, "declaration_list") {
            let mut new_scope = scope_stack.to_vec();
            new_scope.push(name);
            self.walk(body, source, module_name, entities, edges, &new_scope);
        }
    }
}

impl LanguageParser for RustParser {
    fn parse(&self, source: &str, file_path: &str) -> (Vec<ParsedEntity>, Vec<ParsedEdge>) {
        let source_bytes = source.as_bytes();

        let mut parser = Parser::new();
        let language = tree_sitter_rust::LANGUAGE;
        parser.set_language(&language.into()).unwrap();

        let tree = match parser.parse(source_bytes, None) {
            Some(t) => t,
            None => return (Vec::new(), Vec::new()),
        };

        let mut entities = Vec::new();
        let mut edges = Vec::new();

        let module_name = file_path
            .replace('/', ".")
            .strip_suffix(".rs")
            .unwrap_or(file_path)
            .to_string();

        entities.push(ParsedEntity {
            kind: NodeType::File,
            name: file_path.to_string(),
            qualified_name: module_name.clone(),
            start_line: 0,
            end_line: source.lines().count(),
            raw_text: format!("file: {file_path}"),
            signature: String::new(),
            docstring: String::new(),
            parent_qualified_name: None,
            bases: Vec::new(),
            imports: Vec::new(),
            cyclomatic_complexity: None,
        });

        let scope_stack = vec![module_name.clone()];
        self.walk(
            tree.root_node(),
            source_bytes,
            &module_name,
            &mut entities,
            &mut edges,
            &scope_stack,
        );

        (entities, edges)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_struct() {
        let parser = RustParser::new();
        let source = "pub struct Graph {\n    nodes: Vec<Node>,\n}\n";
        let (entities, _) = parser.parse(source, "graph.rs");
        let classes: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Class)
            .collect();
        assert_eq!(classes.len(), 1);
        assert_eq!(classes[0].name, "Graph");
    }

    #[test]
    fn parses_enum() {
        let parser = RustParser::new();
        let source = "pub enum Color { Red, Green, Blue }\n";
        let (entities, _) = parser.parse(source, "types.rs");
        assert!(entities
            .iter()
            .any(|e| e.name == "Color" && e.kind == NodeType::Class));
    }

    #[test]
    fn parses_trait() {
        let parser = RustParser::new();
        let source =
            "pub trait Searchable {\n    fn search(&self, query: &str) -> Vec<String>;\n}\n";
        let (entities, _) = parser.parse(source, "traits.rs");
        assert!(entities
            .iter()
            .any(|e| e.name == "Searchable" && e.kind == NodeType::Class));
    }

    #[test]
    fn parses_top_level_function() {
        let parser = RustParser::new();
        let source = "pub fn process(x: i32) -> bool { x > 0 }\n";
        let (entities, _) = parser.parse(source, "lib.rs");
        let fns: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Function)
            .collect();
        assert_eq!(fns.len(), 1);
        assert_eq!(fns[0].name, "process");
    }

    #[test]
    fn parses_impl_methods() {
        let parser = RustParser::new();
        let source = "struct Foo;\n\nimpl Foo {\n    pub fn new() -> Self { Foo }\n    fn helper(&self) {}\n}\n";
        let (entities, _) = parser.parse(source, "foo.rs");
        let methods: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Method)
            .collect();
        assert_eq!(methods.len(), 2);
        assert!(methods
            .iter()
            .any(|m| m.name == "new" && m.qualified_name.contains("Foo")));
        assert!(methods.iter().any(|m| m.name == "helper"));
    }

    #[test]
    fn cyclomatic_complexity() {
        let parser = RustParser::new();

        // Simple function — no branches → complexity 1.
        let source_simple = "pub fn greet(name: &str) -> String { format!(\"Hello, {name}\") }\n";
        let (entities, _) = parser.parse(source_simple, "greet.rs");
        let func = entities.iter().find(|e| e.name == "greet").unwrap();
        assert_eq!(func.kind, NodeType::Function);
        assert_eq!(func.cyclomatic_complexity, Some(1));

        // Branching function — if + for → complexity 3.
        let source_branch = r#"
fn process(items: &[i32]) {
    for item in items {
        if *item > 0 {
            println!("{item}");
        }
    }
}
"#;
        let (entities2, _) = parser.parse(source_branch, "process.rs");
        let func2 = entities2.iter().find(|e| e.name == "process").unwrap();
        assert_eq!(func2.kind, NodeType::Function);
        // 1 base + for_expression + if_expression = 3
        assert_eq!(func2.cyclomatic_complexity, Some(3));

        // Struct (class-like) should not have cyclomatic complexity.
        let source_struct = "pub struct Config {\n    pub port: u16,\n}\n";
        let (entities3, _) = parser.parse(source_struct, "config.rs");
        let st = entities3.iter().find(|e| e.name == "Config").unwrap();
        assert_eq!(st.kind, NodeType::Class);
        assert_eq!(st.cyclomatic_complexity, None);
    }

    #[test]
    fn parses_use_declarations() {
        let parser = RustParser::new();
        let source = "use std::collections::HashMap;\nuse serde::{Deserialize, Serialize};\n";
        let (entities, _) = parser.parse(source, "lib.rs");
        let imports: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Import)
            .collect();
        let names: Vec<&str> = imports.iter().map(|i| i.name.as_str()).collect();
        assert!(names.contains(&"HashMap"));
        assert!(names.contains(&"Deserialize"));
        assert!(names.contains(&"Serialize"));
    }

    #[test]
    fn parses_constants() {
        let parser = RustParser::new();
        let source = "const MAX_SIZE: usize = 100;\n";
        let (entities, _) = parser.parse(source, "config.rs");
        assert!(entities
            .iter()
            .any(|e| e.name == "MAX_SIZE" && e.kind == NodeType::Constant));
    }

    #[test]
    fn parses_type_alias() {
        let parser = RustParser::new();
        let source = "type NodeId = String;\n";
        let (entities, _) = parser.parse(source, "types.rs");
        assert!(entities
            .iter()
            .any(|e| e.name == "NodeId" && e.kind == NodeType::TypeAlias));
    }

    #[test]
    fn impl_trait_for_creates_implements_edge() {
        let parser = RustParser::new();
        let source = "trait Drawable {}\nstruct Circle;\n\nimpl Drawable for Circle {}\n";
        let (_, edges) = parser.parse(source, "shapes.rs");
        let impl_edges: Vec<_> = edges
            .iter()
            .filter(|e| e.relationship == "implements")
            .collect();
        assert!(!impl_edges.is_empty());
        assert_eq!(impl_edges[0].target_qualified_name, "Drawable");
    }

    #[test]
    fn contains_edges() {
        let parser = RustParser::new();
        let source = "pub struct Foo;\npub fn bar() {}\n";
        let (_, edges) = parser.parse(source, "lib.rs");
        let contains: Vec<_> = edges
            .iter()
            .filter(|e| e.relationship == "contains")
            .collect();
        assert!(contains.len() >= 2);
    }

    #[test]
    fn use_has_contains_edge() {
        let parser = RustParser::new();
        let source = "use std::io;\nuse std::fmt;\n";
        let (entities, edges) = parser.parse(source, "lib.rs");
        let imports: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Import)
            .collect();
        assert!(imports.len() >= 2);
        let contains_targets: std::collections::HashSet<_> = edges
            .iter()
            .filter(|e| e.relationship == "contains")
            .map(|e| e.target_qualified_name.as_str())
            .collect();
        for imp in &imports {
            assert!(contains_targets.contains(imp.qualified_name.as_str()));
        }
    }

    #[test]
    fn file_entity_created() {
        let parser = RustParser::new();
        let (entities, _) = parser.parse("// empty", "src/main.rs");
        let files: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::File)
            .collect();
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].name, "src/main.rs");
    }

    #[test]
    fn doc_comments_extracted() {
        let parser = RustParser::new();
        let source = "/// Creates a new instance.\npub fn create() {}\n";
        let (entities, _) = parser.parse(source, "lib.rs");
        let fns: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Function)
            .collect();
        assert!(fns
            .iter()
            .any(|f| f.docstring.contains("Creates a new instance")));
    }

    #[test]
    fn calls_edges_simple() {
        let parser = RustParser::new();
        let source = r#"
fn caller() {
    helper();
    let r = process(data);
}
"#;
        let (_, edges) = parser.parse(source, "src/lib.rs");
        let calls: Vec<&ParsedEdge> = edges.iter().filter(|e| e.relationship == "calls").collect();
        assert!(calls.iter().any(|e| e.target_qualified_name == "helper"));
        assert!(calls.iter().any(|e| e.target_qualified_name == "process"));
    }

    #[test]
    fn calls_edges_method_call() {
        let parser = RustParser::new();
        let source = r#"
impl Foo {
    fn bar(&self) {
        self.baz();
        standalone();
    }
}
"#;
        let (_, edges) = parser.parse(source, "src/foo.rs");
        let calls: Vec<&ParsedEdge> = edges.iter().filter(|e| e.relationship == "calls").collect();
        assert!(calls.iter().any(|e| e.target_qualified_name == "baz"));
        assert!(calls
            .iter()
            .any(|e| e.target_qualified_name == "standalone"));
    }

    #[test]
    fn calls_edges_skip_builtins() {
        let parser = RustParser::new();
        let source = "fn f() { println!(\"hi\"); vec![1, 2, 3]; }\n";
        let (_, edges) = parser.parse(source, "src/lib.rs");
        let calls: Vec<&ParsedEdge> = edges.iter().filter(|e| e.relationship == "calls").collect();
        assert!(
            calls.is_empty(),
            "Built-in calls should be filtered, got: {:?}",
            calls
                .iter()
                .map(|e| &e.target_qualified_name)
                .collect::<Vec<_>>()
        );
    }
}
