//! Swift AST extraction via tree-sitter.
//!
//! Extracts classes, structs, enums, protocols, functions, methods,
//! properties, imports, and type aliases from Swift source files.
//! Uses a scope stack to build fully qualified names.

use std::collections::HashSet;

use tree_sitter::{Node, Parser};

use scs_core::node_types::NodeType;

use super::{count_complexity, extract_calls, LanguageParser, ParsedEdge, ParsedEntity};

/// Built-in names to exclude from call-graph edges (Swift).
const SWIFT_BUILTIN_CALLS: &[&str] = &[
    "print",
    "debugPrint",
    "dump",
    "fatalError",
    "precondition",
    "preconditionFailure",
    "assert",
    "assertionFailure",
    "abs",
    "min",
    "max",
    "stride",
    "zip",
    "type",
    "unsafeBitCast",
    "withUnsafePointer",
    "withUnsafeMutablePointer",
    "MemoryLayout",
    "Mirror",
    "Optional",
    "Result",
    "DispatchQueue",
    "NSLog",
];

fn get_text<'a>(node: &Node, source: &'a [u8]) -> &'a str {
    std::str::from_utf8(&source[node.start_byte()..node.end_byte()]).unwrap_or("")
}

fn find_child_by_kind<'a>(node: &Node<'a>, kind: &str) -> Option<Node<'a>> {
    let mut cursor = node.walk();
    let result = node.children(&mut cursor).find(|c| c.kind() == kind);
    result
}

/// Extract the name identifier from a Swift definition node.
///
/// Swift uses `type_identifier` for types and `simple_identifier` for
/// function/variable names.
fn get_name(node: &Node, source: &[u8]) -> String {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "type_identifier" || child.kind() == "simple_identifier" {
            return get_text(&child, source).to_string();
        }
    }
    String::new()
}

/// Extract a doc comment preceding a Swift declaration node.
///
/// Handles two styles:
/// - `///` line doc comments: consecutive siblings walked backward, reversed
/// - `/** ... */` block doc comments: single preceding sibling starting with `/**`
///
/// Non-doc comments (`//`, `/* */` without `**`) are ignored.
fn get_doc_comment(node: &Node, source: &[u8]) -> String {
    let mut sibling = node.prev_named_sibling();

    // Check for block doc comment first (`/** ... */`).
    if let Some(ref s) = sibling {
        if s.kind() == "multiline_comment" || s.kind() == "comment" {
            let text = get_text(s, source).trim().to_string();
            if let Some(without_prefix) = text.strip_prefix("/**") {
                let stripped = without_prefix.strip_suffix("*/").unwrap_or(without_prefix);
                let lines: Vec<&str> = stripped
                    .lines()
                    .map(|l| l.trim().trim_start_matches('*').trim())
                    .filter(|l| !l.is_empty())
                    .collect();
                return lines.join("\n");
            }
        }
    }

    // Walk backward collecting `///` line doc comments.
    let mut lines: Vec<String> = Vec::new();
    while let Some(s) = sibling {
        if s.kind() != "comment" {
            break;
        }
        let text = get_text(&s, source).trim().to_string();
        if let Some(without_prefix) = text.strip_prefix("///") {
            lines.push(without_prefix.trim().to_string());
        } else {
            break;
        }
        sibling = s.prev_named_sibling();
    }
    if lines.is_empty() {
        return String::new();
    }
    lines.reverse();
    lines.join("\n")
}

/// Extract inherited types from Swift inheritance specifiers.
///
/// tree-sitter-swift represents inheritance as `inheritance_specifier`
/// children containing `user_type > type_identifier`.
fn get_inheritance(node: &Node, source: &[u8]) -> Vec<String> {
    let mut bases = Vec::new();
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "inheritance_specifier" {
            let mut sub_cursor = child.walk();
            for sub in child.children(&mut sub_cursor) {
                if sub.kind() == "user_type" {
                    if let Some(ident) = find_child_by_kind(&sub, "type_identifier") {
                        bases.push(get_text(&ident, source).to_string());
                    }
                }
            }
        }
    }
    bases
}

/// Parse Swift source files using tree-sitter.
///
/// Handles classes, structs, enums, protocols, functions, computed
/// properties, and import statements. Nested types are tracked via
/// a scope stack for qualified name construction.
#[derive(Default)]
pub struct SwiftParser;

impl SwiftParser {
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
                "class_declaration"
                | "struct_declaration"
                | "enum_declaration"
                | "protocol_declaration" => {
                    self.handle_type_declaration(
                        child,
                        source,
                        module_name,
                        entities,
                        edges,
                        scope_stack,
                    );
                }
                "function_declaration" => {
                    self.handle_function(child, source, entities, edges, scope_stack);
                }
                "import_declaration" => {
                    self.handle_import(child, source, module_name, entities, edges);
                }
                "property_declaration" | "variable_declaration" => {
                    self.handle_property(child, source, entities, edges, scope_stack);
                }
                "typealias_declaration" => {
                    self.handle_typealias(child, source, entities, edges, scope_stack);
                }
                _ => {
                    // Recurse into container nodes.
                    if child.child_count() > 0 {
                        self.walk(child, source, module_name, entities, edges, scope_stack);
                    }
                }
            }
        }
    }

    fn handle_type_declaration(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let name = get_name(&node, source);
        if name.is_empty() {
            return;
        }

        // Skip CodingKeys enums — Codable boilerplate.
        if name == "CodingKeys" {
            return;
        }

        let qualified = format!("{}.{name}", scope_stack.join("."));
        let bases = get_inheritance(&node, source);

        let raw = get_text(&node, source);
        let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_LIMIT);

        entities.push(ParsedEntity {
            kind: NodeType::Class,
            name: name.clone(),
            qualified_name: qualified.clone(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            raw_text: raw_truncated.to_string(),
            parent_qualified_name: Some(scope_stack.join(".")),
            bases: bases.clone(),
            signature: String::new(),
            docstring: get_doc_comment(&node, source),
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

        // Recurse into the class body.
        let body = find_child_by_kind(&node, "class_body")
            .or_else(|| find_child_by_kind(&node, "enum_class_body"));
        if let Some(body) = body {
            let mut new_scope = scope_stack.to_vec();
            new_scope.push(name);
            self.walk(body, source, module_name, entities, edges, &new_scope);
        }
    }

    fn handle_function(
        &self,
        node: Node,
        source: &[u8],
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let name = get_name(&node, source);
        if name.is_empty() {
            return;
        }

        let qualified = format!("{}.{name}", scope_stack.join("."));
        let is_method = scope_stack.len() > 1;
        let kind = if is_method {
            NodeType::Method
        } else {
            NodeType::Function
        };

        // Extract parameter clause.
        let params = find_child_by_kind(&node, "parameter_clause")
            .or_else(|| find_child_by_kind(&node, "function_value_parameters"))
            .map(|p| get_text(&p, source).to_string())
            .unwrap_or_default();

        // Extract return type.
        let mut return_type = String::new();
        let mut fn_cursor = node.walk();
        for child in node.children(&mut fn_cursor) {
            if child.kind() == "function_type" {
                return_type = get_text(&child, source).to_string();
                break;
            }
        }

        let mut signature = params;
        if !return_type.is_empty() {
            signature = format!("{signature} -> {return_type}");
        }

        let raw = get_text(&node, source);
        let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_LIMIT);

        let complexity = count_complexity(&node, source, "swift");

        entities.push(ParsedEntity {
            kind,
            name,
            qualified_name: qualified.clone(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            signature,
            raw_text: raw_truncated.to_string(),
            parent_qualified_name: Some(scope_stack.join(".")),
            docstring: get_doc_comment(&node, source),
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
            let blocklist: HashSet<&str> = SWIFT_BUILTIN_CALLS.iter().copied().collect();
            let callees = extract_calls(&node, source, "swift", &blocklist);
            for callee in callees {
                edges.push(ParsedEdge::new(qualified.clone(), callee, "calls"));
            }
        }
    }

    fn handle_import(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
    ) {
        let text = get_text(&node, source);
        let parts: Vec<&str> = text.split_whitespace().collect();
        if parts.len() >= 2 {
            let name = parts.last().unwrap().to_string();
            let qualified = format!("{module_name}.import.{name}");
            entities.push(ParsedEntity {
                kind: NodeType::Import,
                name: name.clone(),
                qualified_name: qualified.clone(),
                start_line: node.start_position().row,
                end_line: node.end_position().row,
                imports: vec![name.clone()],
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
            // IMPORTS edge for semantic dependency tracking.
            edges.push(ParsedEdge::new(module_name.to_string(), name, "imports"));
        }
    }

    fn handle_property(
        &self,
        node: Node,
        source: &[u8],
        entities: &mut Vec<ParsedEntity>,
        _edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let name = get_name(&node, source);
        if name.is_empty() || name.len() < 2 {
            return;
        }

        let qualified = format!("{}.{name}", scope_stack.join("."));
        let raw = get_text(&node, source);
        let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_SMALL_LIMIT);

        entities.push(ParsedEntity {
            kind: NodeType::Variable,
            name,
            qualified_name: qualified,
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
    }

    fn handle_typealias(
        &self,
        node: Node,
        source: &[u8],
        entities: &mut Vec<ParsedEntity>,
        _edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let name = get_name(&node, source);
        if name.is_empty() {
            return;
        }

        let qualified = if scope_stack.is_empty() {
            name.clone()
        } else {
            format!("{}.{name}", scope_stack.join("."))
        };
        let raw = get_text(&node, source);
        let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_SMALL_LIMIT);

        entities.push(ParsedEntity {
            kind: NodeType::TypeAlias,
            name,
            qualified_name: qualified,
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            raw_text: raw_truncated.to_string(),
            parent_qualified_name: if scope_stack.is_empty() {
                None
            } else {
                Some(scope_stack.join("."))
            },
            signature: String::new(),
            docstring: String::new(),
            bases: Vec::new(),
            imports: Vec::new(),
            cyclomatic_complexity: None,
        });
    }
}

impl LanguageParser for SwiftParser {
    fn parse(&self, source: &str, file_path: &str) -> (Vec<ParsedEntity>, Vec<ParsedEdge>) {
        let source_bytes = source.as_bytes();

        let mut parser = Parser::new();
        let language = tree_sitter_swift::LANGUAGE;
        parser.set_language(&language.into()).unwrap();

        let tree = match parser.parse(source_bytes, None) {
            Some(t) => t,
            None => return (Vec::new(), Vec::new()),
        };

        let mut entities = Vec::new();
        let mut edges = Vec::new();

        let module_name = file_path
            .replace('/', ".")
            .strip_suffix(".swift")
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
    fn parses_class() {
        let parser = SwiftParser::new();
        let source = "class AppDelegate: NSObject {\n    var window: NSWindow?\n}\n";
        let (entities, _) = parser.parse(source, "AppDelegate.swift");
        let classes: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Class)
            .collect();
        assert!(classes.iter().any(|c| c.name == "AppDelegate"));
    }

    #[test]
    fn parses_struct() {
        let parser = SwiftParser::new();
        let source = "struct Settings {\n    let host: String\n    let port: Int\n}\n";
        let (entities, _) = parser.parse(source, "Settings.swift");
        assert!(entities
            .iter()
            .any(|e| e.name == "Settings" && e.kind == NodeType::Class));
    }

    #[test]
    fn parses_protocol() {
        let parser = SwiftParser::new();
        let source = "protocol Searchable {\n    func search(query: String) -> [String]\n}\n";
        let (entities, _) = parser.parse(source, "Searchable.swift");
        assert!(entities
            .iter()
            .any(|e| e.name == "Searchable" && e.kind == NodeType::Class));
    }

    #[test]
    fn parses_function() {
        let parser = SwiftParser::new();
        let source = "func processAudio(data: Data) -> Bool {\n    return true\n}\n";
        let (entities, _) = parser.parse(source, "Audio.swift");
        assert!(entities
            .iter()
            .any(|e| e.name == "processAudio" && e.kind == NodeType::Function));
    }

    #[test]
    fn cyclomatic_complexity() {
        let parser = SwiftParser::new();

        // Simple function — no branches → complexity 1.
        let source_simple =
            "func greet(name: String) -> String {\n    return \"Hello, \\(name)\"\n}\n";
        let (entities, _) = parser.parse(source_simple, "Greet.swift");
        let func = entities.iter().find(|e| e.name == "greet").unwrap();
        assert_eq!(func.kind, NodeType::Function);
        assert_eq!(func.cyclomatic_complexity, Some(1));

        // Branching function — if + for → complexity 3.
        let source_branch = r#"
func process(items: [Int]) {
    for item in items {
        if item > 0 {
            print(item)
        }
    }
}
"#;
        let (entities2, _) = parser.parse(source_branch, "Process.swift");
        let func2 = entities2.iter().find(|e| e.name == "process").unwrap();
        assert_eq!(func2.kind, NodeType::Function);
        // 1 base + for_statement + if_statement = 3
        assert_eq!(func2.cyclomatic_complexity, Some(3));

        // Struct (class-like) should not have cyclomatic complexity.
        let source_struct = "struct Config {\n    let port: Int\n}\n";
        let (entities3, _) = parser.parse(source_struct, "Config.swift");
        let st = entities3.iter().find(|e| e.name == "Config").unwrap();
        assert_eq!(st.kind, NodeType::Class);
        assert_eq!(st.cyclomatic_complexity, None);
    }

    #[test]
    fn parses_imports() {
        let parser = SwiftParser::new();
        let source = "import Foundation\nimport AppKit\n";
        let (entities, _) = parser.parse(source, "Main.swift");
        let imports: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Import)
            .collect();
        let names: Vec<&str> = imports.iter().map(|i| i.name.as_str()).collect();
        assert!(names.contains(&"Foundation"));
        assert!(names.contains(&"AppKit"));
    }

    #[test]
    fn import_has_contains_edge() {
        let parser = SwiftParser::new();
        let source = "import Foundation\nimport AppKit\n";
        let (entities, edges) = parser.parse(source, "Main.swift");
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
    fn inheritance_edges() {
        let parser = SwiftParser::new();
        let source = "class MyView: NSView, Drawable {\n}\n";
        let (entities, edges) = parser.parse(source, "MyView.swift");
        let cls = entities.iter().find(|e| e.name == "MyView").unwrap();
        assert!(cls.bases.contains(&"NSView".to_string()));
        assert!(cls.bases.contains(&"Drawable".to_string()));
        let inherits: Vec<_> = edges
            .iter()
            .filter(|e| e.relationship == "inherits")
            .collect();
        assert_eq!(inherits.len(), 2);
    }

    #[test]
    fn file_entity_created() {
        let parser = SwiftParser::new();
        let (entities, _) = parser.parse("// empty", "App.swift");
        let files: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::File)
            .collect();
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].name, "App.swift");
    }

    #[test]
    fn doc_comments_extracted() {
        let parser = SwiftParser::new();
        let source = "/// Creates a new instance.\n/// With default settings.\nfunc create() {}\n";
        let (entities, _) = parser.parse(source, "Factory.swift");
        let func = entities.iter().find(|e| e.name == "create").unwrap();
        assert!(func.docstring.contains("Creates a new instance"));
        assert!(func.docstring.contains("With default settings"));
    }

    #[test]
    fn block_doc_comment_extracted() {
        let parser = SwiftParser::new();
        let source = "/** A view model for the main screen. */\nclass MainViewModel {}\n";
        let (entities, _) = parser.parse(source, "MainViewModel.swift");
        let cls = entities.iter().find(|e| e.name == "MainViewModel").unwrap();
        assert!(
            cls.docstring.contains("A view model"),
            "Expected docstring to contain 'A view model', got: {:?}",
            cls.docstring
        );
    }

    #[test]
    fn calls_edges_simple() {
        let parser = SwiftParser::new();
        let source = r#"
func caller() {
    helper()
    let r = process(data)
}
"#;
        let (_, edges) = parser.parse(source, "Sources/App.swift");
        let calls: Vec<&ParsedEdge> = edges.iter().filter(|e| e.relationship == "calls").collect();
        assert!(calls.iter().any(|e| e.target_qualified_name == "helper"));
        assert!(calls.iter().any(|e| e.target_qualified_name == "process"));
    }

    #[test]
    fn calls_edges_method_call() {
        let parser = SwiftParser::new();
        let source = r#"
class Foo {
    func bar() {
        self.baz()
        standalone()
    }
}
"#;
        let (_, edges) = parser.parse(source, "Sources/Foo.swift");
        let calls: Vec<&ParsedEdge> = edges.iter().filter(|e| e.relationship == "calls").collect();
        assert!(calls.iter().any(|e| e.target_qualified_name == "baz"));
        assert!(calls
            .iter()
            .any(|e| e.target_qualified_name == "standalone"));
    }

    #[test]
    fn calls_edges_skip_builtins() {
        let parser = SwiftParser::new();
        let source = "func f() { print(\"hi\") }\n";
        let (_, edges) = parser.parse(source, "Sources/App.swift");
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
