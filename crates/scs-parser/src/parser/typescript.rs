//! TypeScript/JavaScript AST extraction via tree-sitter.
//!
//! Extracts classes, functions, interfaces, type aliases, imports,
//! and constants from TypeScript, TSX, JavaScript, and JSX files.
//! Uses a scope stack for qualified name construction.

use std::collections::HashSet;

use tree_sitter::{Node, Parser};

use scs_core::node_types::NodeType;

use super::{count_complexity, extract_calls, LanguageParser, ParsedEdge, ParsedEntity};

/// Built-in names to exclude from call-graph edges (TypeScript/JavaScript).
const TS_BUILTIN_CALLS: &[&str] = &[
    "console",
    "Math",
    "JSON",
    "Object",
    "Array",
    "String",
    "Number",
    "Boolean",
    "Promise",
    "setTimeout",
    "setInterval",
    "clearTimeout",
    "clearInterval",
    "parseInt",
    "parseFloat",
    "require",
    "fetch",
    "alert",
    "confirm",
    "prompt",
    "Error",
    "TypeError",
    "RangeError",
    "SyntaxError",
    "ReferenceError",
    "Map",
    "Set",
    "WeakMap",
    "WeakSet",
    "Symbol",
    "Proxy",
    "Reflect",
    "RegExp",
    "Date",
    "Intl",
    "URL",
    "URLSearchParams",
    "TextEncoder",
    "TextDecoder",
    "atob",
    "btoa",
    "structuredClone",
    "queueMicrotask",
    "requestAnimationFrame",
    "cancelAnimationFrame",
    "addEventListener",
    "removeEventListener",
    "log",
    "warn",
    "error",
    "info",
    "debug",
    "trace",
    "assert",
];

/// Pattern to detect UPPER_CASE constants.
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

/// Extract a JSDoc comment (`/** ... */`) preceding a declaration node.
///
/// Tree-sitter for TypeScript uses `comment` for all comment types.
/// Only `/** ... */` JSDoc-style comments are extracted — regular
/// `//` and `/* */` comments are ignored.
fn get_doc_comment(node: &Node, source: &[u8]) -> String {
    let mut sibling = node.prev_named_sibling();

    // Also check prev_sibling (unnamed) — comments may not be named nodes.
    if sibling.is_none() {
        let mut unnamed = node.prev_sibling();
        while let Some(s) = unnamed {
            if s.kind() == "comment" {
                sibling = Some(s);
                break;
            }
            // Skip decorators and other non-comment siblings.
            if !s.kind().contains("decorator") {
                break;
            }
            unnamed = s.prev_sibling();
        }
    }

    if let Some(ref s) = sibling {
        if s.kind() == "comment" {
            let text = get_text(s, source).trim().to_string();
            if text.starts_with("/**") && text.ends_with("*/") {
                let stripped = &text[3..text.len() - 2];
                let lines: Vec<&str> = stripped
                    .lines()
                    .map(|l| l.trim().trim_start_matches('*').trim())
                    .filter(|l| !l.is_empty())
                    .collect();
                return lines.join("\n");
            }
        }
    }

    String::new()
}

/// Parse TypeScript/JavaScript files using tree-sitter.
///
/// Handles classes, functions, arrow functions, interfaces, type
/// aliases, import statements, and const/let/var declarations.
/// TSX/JSX are parsed using the TSX grammar.
pub struct TypeScriptParser {
    language_name: String,
}

impl TypeScriptParser {
    /// Create a parser for the given language variant.
    ///
    /// # Arguments
    /// * `language` — One of "typescript", "tsx", "javascript", "jsx"
    pub fn new(language: &str) -> Self {
        Self {
            language_name: language.to_string(),
        }
    }

    /// Create a fresh tree-sitter parser configured for the right language.
    fn create_parser(&self) -> Parser {
        let mut parser = Parser::new();
        let lang = if self.language_name == "tsx" || self.language_name == "jsx" {
            tree_sitter_typescript::LANGUAGE_TSX
        } else {
            tree_sitter_typescript::LANGUAGE_TYPESCRIPT
        };
        parser.set_language(&lang.into()).unwrap();
        parser
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
                "class_declaration" => {
                    self.handle_class(child, source, module_name, entities, edges, scope_stack);
                }
                "function_declaration" => {
                    self.handle_function(child, source, module_name, entities, edges, scope_stack);
                }
                "interface_declaration" | "type_alias_declaration" => {
                    self.handle_type_declaration(
                        child,
                        source,
                        module_name,
                        entities,
                        edges,
                        scope_stack,
                    );
                }
                "import_statement" | "import_declaration" => {
                    self.handle_import(child, source, module_name, entities, edges);
                }
                "lexical_declaration" | "variable_declaration" => {
                    self.handle_variable(child, source, module_name, entities, edges, scope_stack);
                }
                "export_statement" => {
                    // Unwrap export and process the inner declaration.
                    self.walk(child, source, module_name, entities, edges, scope_stack);
                }
                "method_definition" | "public_field_definition" => {
                    self.handle_method(child, source, module_name, entities, edges, scope_stack);
                }
                _ => {
                    // Don't recurse into function bodies.
                    if child.child_count() > 0
                        && child.kind() != "statement_block"
                        && child.kind() != "function_body"
                    {
                        self.walk(child, source, module_name, entities, edges, scope_stack);
                    }
                }
            }
        }
    }

    fn handle_class(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let mut name = String::new();
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "type_identifier" || child.kind() == "identifier" {
                name = get_text(&child, source).to_string();
                break;
            }
        }
        if name.is_empty() {
            return;
        }

        let qualified = format!("{}.{name}", scope_stack.join("."));

        // Extract heritage (extends/implements).
        let mut bases = Vec::new();
        let mut cursor2 = node.walk();
        for child in node.children(&mut cursor2) {
            if matches!(
                child.kind(),
                "class_heritage" | "extends_clause" | "implements_clause"
            ) {
                let mut sub_cursor = child.walk();
                for sub in child.children(&mut sub_cursor) {
                    if sub.kind() == "type_identifier" || sub.kind() == "identifier" {
                        bases.push(get_text(&sub, source).to_string());
                    }
                }
            }
        }

        let raw = get_text(&node, source);
        entities.push(ParsedEntity {
            kind: NodeType::Class,
            name: name.clone(),
            qualified_name: qualified.clone(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            raw_text: super::truncate_str(raw, super::RAW_TEXT_LIMIT).to_string(),
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

        // Recurse into class body.
        if let Some(body) = find_child_by_kind(&node, "class_body") {
            let mut new_scope = scope_stack.to_vec();
            new_scope.push(name);
            self.walk(body, source, module_name, entities, edges, &new_scope);
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
        let mut name = String::new();
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "identifier" {
                name = get_text(&child, source).to_string();
                break;
            }
        }
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

        let params = find_child_by_kind(&node, "formal_parameters")
            .map(|p| get_text(&p, source).to_string())
            .unwrap_or_default();

        let raw = get_text(&node, source);
        let complexity = count_complexity(&node, source, &self.language_name);
        entities.push(ParsedEntity {
            kind,
            name,
            qualified_name: qualified.clone(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            signature: params,
            raw_text: super::truncate_str(raw, super::RAW_TEXT_LIMIT).to_string(),
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

        // CALLS edges from this function to callees in its body.
        if let Some(body) = find_child_by_kind(&node, "statement_block") {
            let blocklist: HashSet<&str> = TS_BUILTIN_CALLS.iter().copied().collect();
            let callees = extract_calls(&body, source, &self.language_name, &blocklist);
            for callee in callees {
                edges.push(ParsedEdge::new(qualified.clone(), callee, "calls"));
            }
        }
    }

    fn handle_method(
        &self,
        node: Node,
        source: &[u8],
        _module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let mut name = String::new();
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "property_identifier" || child.kind() == "identifier" {
                name = get_text(&child, source).to_string();
                break;
            }
        }
        if name.is_empty() {
            return;
        }

        let qualified = format!("{}.{name}", scope_stack.join("."));
        let params = find_child_by_kind(&node, "formal_parameters")
            .map(|p| get_text(&p, source).to_string())
            .unwrap_or_default();

        let raw = get_text(&node, source);
        let complexity = count_complexity(&node, source, &self.language_name);
        entities.push(ParsedEntity {
            kind: NodeType::Method,
            name,
            qualified_name: qualified.clone(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            signature: params,
            raw_text: super::truncate_str(raw, super::RAW_TEXT_LIMIT).to_string(),
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

        // CALLS edges from this method to callees in its body.
        if let Some(body) = find_child_by_kind(&node, "statement_block") {
            let blocklist: HashSet<&str> = TS_BUILTIN_CALLS.iter().copied().collect();
            let callees = extract_calls(&body, source, &self.language_name, &blocklist);
            for callee in callees {
                edges.push(ParsedEdge::new(qualified.clone(), callee, "calls"));
            }
        }
    }

    fn handle_type_declaration(
        &self,
        node: Node,
        source: &[u8],
        _module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let mut name = String::new();
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "type_identifier" || child.kind() == "identifier" {
                name = get_text(&child, source).to_string();
                break;
            }
        }
        if name.is_empty() {
            return;
        }

        let qualified = format!("{}.{name}", scope_stack.join("."));
        let raw = get_text(&node, source);

        // Interfaces are class-like; type aliases are TYPE_ALIAS.
        let kind = if node.kind() == "interface_declaration" {
            NodeType::Class
        } else {
            NodeType::TypeAlias
        };

        entities.push(ParsedEntity {
            kind,
            name,
            qualified_name: qualified.clone(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            raw_text: super::truncate_str(raw, super::RAW_TEXT_SMALL_LIMIT).to_string(),
            parent_qualified_name: Some(scope_stack.join(".")),
            signature: String::new(),
            docstring: get_doc_comment(&node, source),
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

    fn handle_import(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
    ) {
        // Extract the module path from the import string literal.
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "string" {
                let import_path = get_text(&child, source)
                    .trim_matches('"')
                    .trim_matches('\'')
                    .to_string();
                let qualified = format!("{module_name}.import.{import_path}");
                entities.push(ParsedEntity {
                    kind: NodeType::Import,
                    name: import_path.clone(),
                    qualified_name: qualified.clone(),
                    start_line: node.start_position().row,
                    end_line: node.end_position().row,
                    imports: vec![import_path.clone()],
                    signature: String::new(),
                    docstring: String::new(),
                    raw_text: String::new(),
                    parent_qualified_name: Some(module_name.to_string()),
                    bases: Vec::new(),
                    cyclomatic_complexity: None,
                });
                // CONTAINS edge from file → import node so it isn't an orphan.
                edges.push(ParsedEdge::new(
                    module_name.to_string(),
                    qualified,
                    "contains",
                ));
                edges.push(ParsedEdge::new(
                    module_name.to_string(),
                    import_path,
                    "imports",
                ));
                break;
            }
        }
    }

    #[allow(clippy::ptr_arg)] // Signature mirrors walk() callback pattern.
    fn handle_variable(
        &self,
        node: Node,
        source: &[u8],
        _module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        // Only extract top-level declarations.
        if scope_stack.len() > 1 {
            return;
        }

        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "variable_declarator" {
                let mut name = String::new();
                let mut sub_cursor = child.walk();
                for sub in child.children(&mut sub_cursor) {
                    if sub.kind() == "identifier" {
                        name = get_text(&sub, source).to_string();
                        break;
                    }
                }
                if name.is_empty() || name.len() < 2 {
                    continue;
                }

                let qualified = format!("{}.{name}", scope_stack.join("."));
                let raw = get_text(&child, source);
                let kind = if is_constant_name(&name) {
                    NodeType::Constant
                } else {
                    NodeType::Variable
                };

                entities.push(ParsedEntity {
                    kind,
                    name,
                    qualified_name: qualified.clone(),
                    start_line: child.start_position().row,
                    end_line: child.end_position().row,
                    signature: super::truncate_str(raw, super::RAW_TEXT_SMALL_LIMIT).to_string(),
                    raw_text: super::truncate_str(raw, super::RAW_TEXT_SMALL_LIMIT).to_string(),
                    parent_qualified_name: Some(scope_stack.join(".")),
                    docstring: String::new(),
                    bases: Vec::new(),
                    imports: Vec::new(),
                    cyclomatic_complexity: None,
                });

                // CONTAINS edge from parent scope — connects the
                // variable/constant to its module so it appears in
                // graph traversals. Without this, these nodes are
                // orphans with zero edges (audit issue #5).
                edges.push(ParsedEdge::new(
                    scope_stack.join("."),
                    qualified,
                    "contains",
                ));
            }
        }
    }
}

impl LanguageParser for TypeScriptParser {
    fn parse(&self, source: &str, file_path: &str) -> (Vec<ParsedEntity>, Vec<ParsedEdge>) {
        let source_bytes = source.as_bytes();
        let mut parser = self.create_parser();

        let tree = match parser.parse(source_bytes, None) {
            Some(t) => t,
            None => return (Vec::new(), Vec::new()),
        };

        let mut entities = Vec::new();
        let mut edges = Vec::new();

        // Module name: strip extension and replace slashes with dots.
        let mut module_name = file_path.replace('/', ".");
        for suffix in [".ts", ".tsx", ".js", ".jsx"] {
            if let Some(stripped) = module_name.strip_suffix(suffix) {
                module_name = stripped.to_string();
                break;
            }
        }

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
    fn parses_typescript_class() {
        let parser = TypeScriptParser::new("typescript");
        let source = r#"
class UserService {
    private db: Database;

    async getUser(id: string): Promise<User> {
        return this.db.find(id);
    }
}
"#;
        let (entities, edges) = parser.parse(source, "services/user.ts");
        let names: Vec<&str> = entities.iter().map(|e| e.name.as_str()).collect();
        assert!(names.contains(&"UserService"));
        assert!(!edges.is_empty());
    }

    #[test]
    fn parses_interface() {
        let parser = TypeScriptParser::new("typescript");
        let source = "interface Config {\n  port: number;\n  host: string;\n}\n";
        let (entities, _) = parser.parse(source, "config.ts");

        let iface = entities.iter().find(|e| e.name == "Config").unwrap();
        // Interfaces are mapped to Class type.
        assert_eq!(iface.kind, NodeType::Class);
    }

    #[test]
    fn cyclomatic_complexity() {
        let parser = TypeScriptParser::new("typescript");

        // Simple function — no branches → complexity 1.
        let source_simple =
            "function greet(name: string): string {\n  return `Hello, ${name}`;\n}\n";
        let (entities, _) = parser.parse(source_simple, "greet.ts");
        let func = entities.iter().find(|e| e.name == "greet").unwrap();
        assert_eq!(func.kind, NodeType::Function);
        assert_eq!(func.cyclomatic_complexity, Some(1));

        // Branching function — if + for → complexity 3.
        let source_branch = r#"
function process(items: number[]): void {
    for (const item of items) {
        if (item > 0) {
            console.log(item);
        }
    }
}
"#;
        let (entities2, _) = parser.parse(source_branch, "process.ts");
        let func2 = entities2.iter().find(|e| e.name == "process").unwrap();
        assert_eq!(func2.kind, NodeType::Function);
        // 1 base + for_in_statement + if_statement = 3
        assert_eq!(func2.cyclomatic_complexity, Some(3));

        // Interface (class-like) should not have cyclomatic complexity.
        let source_iface = "interface Config {\n  port: number;\n}\n";
        let (entities3, _) = parser.parse(source_iface, "config.ts");
        let iface = entities3.iter().find(|e| e.name == "Config").unwrap();
        assert_eq!(iface.kind, NodeType::Class);
        assert_eq!(iface.cyclomatic_complexity, None);
    }

    #[test]
    fn parses_imports() {
        let parser = TypeScriptParser::new("typescript");
        let source = "import { Router } from 'express';\n";
        let (entities, _) = parser.parse(source, "app.ts");

        let imports: Vec<&ParsedEntity> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Import)
            .collect();
        assert!(!imports.is_empty());
    }

    #[test]
    fn jsdoc_comments_extracted() {
        let parser = TypeScriptParser::new("typescript");
        let source = "/** Creates a thing. */\nfunction create() {}\n";
        let (entities, _) = parser.parse(source, "factory.ts");
        let func = entities.iter().find(|e| e.name == "create").unwrap();
        assert!(
            func.docstring.contains("Creates a thing"),
            "Expected docstring to contain 'Creates a thing', got: {:?}",
            func.docstring
        );
    }

    #[test]
    fn jsdoc_on_class_extracted() {
        let parser = TypeScriptParser::new("typescript");
        let source = "/** User service. */\nclass UserService {}\n";
        let (entities, _) = parser.parse(source, "service.ts");
        let cls = entities.iter().find(|e| e.name == "UserService").unwrap();
        assert!(
            cls.docstring.contains("User service"),
            "Expected docstring to contain 'User service', got: {:?}",
            cls.docstring
        );
    }

    #[test]
    fn calls_edges_simple_function() {
        let parser = TypeScriptParser::new("typescript");
        let source = r#"
function handler() {
    validate();
    const result = transform(data);
}
"#;
        let (_, edges) = parser.parse(source, "handler.ts");
        let calls: Vec<&ParsedEdge> = edges.iter().filter(|e| e.relationship == "calls").collect();
        assert!(calls.iter().any(|e| e.target_qualified_name == "validate"));
        assert!(calls.iter().any(|e| e.target_qualified_name == "transform"));
    }

    #[test]
    fn calls_edges_method_call() {
        let parser = TypeScriptParser::new("typescript");
        let source = r#"
class Service {
    process() {
        this.helper();
        externalFn();
    }
}
"#;
        let (_, edges) = parser.parse(source, "service.ts");
        let calls: Vec<&ParsedEdge> = edges.iter().filter(|e| e.relationship == "calls").collect();
        assert!(calls.iter().any(|e| e.target_qualified_name == "helper"));
        assert!(calls
            .iter()
            .any(|e| e.target_qualified_name == "externalFn"));
    }

    #[test]
    fn calls_edges_skip_builtins() {
        let parser = TypeScriptParser::new("typescript");
        let source = "function f() { parseInt('42'); }\n";
        let (_, edges) = parser.parse(source, "num.ts");
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

    #[test]
    fn calls_edges_deduplicated() {
        let parser = TypeScriptParser::new("typescript");
        let source = "function f() { foo(); foo(); foo(); }\n";
        let (_, edges) = parser.parse(source, "dedup.ts");
        let foo_calls: Vec<&ParsedEdge> = edges
            .iter()
            .filter(|e| e.relationship == "calls" && e.target_qualified_name == "foo")
            .collect();
        assert_eq!(foo_calls.len(), 1, "Duplicate calls should be deduplicated");
    }
}
