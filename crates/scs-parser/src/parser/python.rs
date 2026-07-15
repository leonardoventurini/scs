//! Python AST extraction via tree-sitter.
//!
//! Extracts classes, functions, methods, constants, imports, and type
//! aliases from Python source files. Uses a scope stack to build
//! fully qualified names (e.g. "MyModule.MyClass.my_method").

use std::collections::HashSet;

use tree_sitter::{Node, Parser};

use scs_core::node_types::NodeType;

use super::{count_complexity, extract_calls, LanguageParser, ParsedEdge, ParsedEntity};

/// Built-in function names to exclude from call-graph edges.
/// These are stdlib/builtin calls that would never match a project entity.
const PYTHON_BUILTIN_CALLS: &[&str] = &[
    "print",
    "len",
    "range",
    "str",
    "int",
    "float",
    "bool",
    "list",
    "dict",
    "set",
    "tuple",
    "type",
    "isinstance",
    "issubclass",
    "super",
    "getattr",
    "setattr",
    "hasattr",
    "delattr",
    "enumerate",
    "zip",
    "map",
    "filter",
    "sorted",
    "reversed",
    "any",
    "all",
    "min",
    "max",
    "sum",
    "abs",
    "round",
    "open",
    "format",
    "repr",
    "id",
    "hash",
    "iter",
    "next",
    "input",
    "vars",
    "dir",
    "callable",
    "classmethod",
    "staticmethod",
    "property",
    "object",
    "bytes",
    "bytearray",
    "memoryview",
    "frozenset",
    "complex",
    "hex",
    "oct",
    "bin",
    "ord",
    "chr",
    "ascii",
    "exec",
    "eval",
    "compile",
    "globals",
    "locals",
    "breakpoint",
    "exit",
    "quit",
    "help",
    "__import__",
    "ValueError",
    "TypeError",
    "KeyError",
    "IndexError",
    "AttributeError",
    "RuntimeError",
    "Exception",
    "NotImplementedError",
    "StopIteration",
    "OSError",
    "IOError",
];

/// Pattern to detect UPPER_CASE constants (Python convention).
fn is_constant_name(name: &str) -> bool {
    !name.is_empty()
        && name.chars().next().unwrap().is_ascii_uppercase()
        && name
            .chars()
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
}

/// Extract the text content of a tree-sitter node.
fn get_text<'a>(node: &Node, source: &'a [u8]) -> &'a str {
    let bytes = &source[node.start_byte()..node.end_byte()];
    std::str::from_utf8(bytes).unwrap_or("")
}

/// Extract a docstring from the first statement in a function/class body.
///
/// Python docstrings are string literals appearing as the first statement
/// in a class or function body. Tree-sitter represents these as
/// `expression_statement` → `string` nodes.
fn get_docstring(body_node: Option<Node>, source: &[u8]) -> String {
    let body = match body_node {
        Some(b) => b,
        None => return String::new(),
    };

    let mut cursor = body.walk();
    for child in body.children(&mut cursor) {
        if child.kind() == "expression_statement" {
            let mut sub_cursor = child.walk();
            for sub in child.children(&mut sub_cursor) {
                if sub.kind() == "string" {
                    let text = get_text(&sub, source);
                    // Strip triple-quote delimiters.
                    for delim in ["\"\"\"", "'''"] {
                        if text.starts_with(delim)
                            && text.ends_with(delim)
                            && text.len() >= delim.len() * 2
                        {
                            return text[delim.len()..text.len() - delim.len()]
                                .trim()
                                .to_string();
                        }
                    }
                    return text
                        .trim_matches(|c| c == '"' || c == '\'')
                        .trim()
                        .to_string();
                }
            }
            break;
        }
        // Skip comments and pass statements.
        if child.kind() != "comment" && child.kind() != "pass_statement" {
            break;
        }
    }
    String::new()
}

/// Extract the parameter list from a function definition.
fn get_parameters(node: &Node, source: &[u8]) -> String {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "parameters" {
            return get_text(&child, source).to_string();
        }
    }
    "()".to_string()
}

/// Extract the return type annotation from a function definition.
fn get_return_type(node: &Node, source: &[u8]) -> String {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "type" {
            return get_text(&child, source).to_string();
        }
    }
    String::new()
}

/// Extract base classes from a class definition's argument_list.
fn get_bases(node: &Node, source: &[u8]) -> Vec<String> {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        if child.kind() == "argument_list" {
            let mut bases = Vec::new();
            let mut arg_cursor = child.walk();
            for arg in child.children(&mut arg_cursor) {
                if arg.kind() == "identifier" || arg.kind() == "attribute" {
                    bases.push(get_text(&arg, source).to_string());
                }
            }
            return bases;
        }
    }
    Vec::new()
}

/// Parse Python source files using tree-sitter.
///
/// Walks the concrete syntax tree to extract structural entities and
/// their relationships. Handles nested classes and functions via a
/// scope stack for qualified name construction.
#[derive(Default)]
pub struct PythonParser;

impl PythonParser {
    /// Create a new Python parser (grammar is loaded on each parse call).
    pub fn new() -> Self {
        Self
    }

    /// Recursively walk the tree-sitter CST, extracting entities.
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
                "class_definition" => {
                    self.handle_class(child, source, module_name, entities, edges, scope_stack);
                }
                "function_definition" => {
                    self.handle_function(child, source, module_name, entities, edges, scope_stack);
                }
                "decorated_definition" => {
                    let mut sub_cursor = child.walk();
                    for sub in child.children(&mut sub_cursor) {
                        match sub.kind() {
                            "class_definition" => self.handle_class(
                                sub,
                                source,
                                module_name,
                                entities,
                                edges,
                                scope_stack,
                            ),
                            "function_definition" => self.handle_function(
                                sub,
                                source,
                                module_name,
                                entities,
                                edges,
                                scope_stack,
                            ),
                            _ => {}
                        }
                    }
                }
                "import_statement" => {
                    self.handle_import(child, source, module_name, entities, edges);
                }
                "import_from_statement" => {
                    self.handle_import_from(child, source, module_name, entities, edges);
                }
                "expression_statement" => {
                    self.handle_assignment(
                        child,
                        source,
                        module_name,
                        entities,
                        edges,
                        scope_stack,
                    );
                }
                "type_alias_statement" => {
                    self.handle_type_alias(
                        child,
                        source,
                        module_name,
                        entities,
                        edges,
                        scope_stack,
                    );
                }
                _ => {}
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
            if child.kind() == "identifier" {
                name = get_text(&child, source).to_string();
                break;
            }
        }
        if name.is_empty() {
            return;
        }

        let qualified = format!("{}.{name}", scope_stack.join("."));
        let bases = get_bases(&node, source);

        // Find the block/body node for docstring extraction and recursion.
        let mut body_node = None;
        let mut cursor2 = node.walk();
        for child in node.children(&mut cursor2) {
            if child.kind() == "block" {
                body_node = Some(child);
                break;
            }
        }

        let docstring = get_docstring(body_node, source);
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

        // CONTAINS edge from parent scope.
        edges.push(ParsedEdge::new(
            scope_stack.join("."),
            qualified.clone(),
            "contains",
        ));

        // INHERITS edges for base classes.
        for base in &bases {
            edges.push(ParsedEdge::new(qualified.clone(), base.clone(), "inherits"));
        }

        // Recurse into class body for methods and nested classes.
        if let Some(body) = body_node {
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

        // Determine if this is a method (inside a class) or a top-level function.
        let is_method = scope_stack.len() > 1; // module.Class.method = depth > 1
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

        // Find body for docstring.
        let mut body_node = None;
        let mut cursor2 = node.walk();
        for child in node.children(&mut cursor2) {
            if child.kind() == "block" {
                body_node = Some(child);
                break;
            }
        }
        let docstring = get_docstring(body_node, source);

        let raw = get_text(&node, source);
        let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_LIMIT);
        let complexity = count_complexity(&node, source, "python");

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

        // CONTAINS edge from parent scope.
        edges.push(ParsedEdge::new(
            scope_stack.join("."),
            qualified.clone(),
            "contains",
        ));

        // CALLS edges from this function/method to callees in its body.
        if let Some(body) = body_node {
            let blocklist: HashSet<&str> = PYTHON_BUILTIN_CALLS.iter().copied().collect();
            let callees = extract_calls(&body, source, "python", &blocklist);
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
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "dotted_name" {
                let name = get_text(&child, source).to_string();
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
                // CONTAINS edge from file → import node so it isn't an orphan.
                edges.push(ParsedEdge::new(
                    module_name.to_string(),
                    qualified,
                    "contains",
                ));
                edges.push(ParsedEdge::new(module_name.to_string(), name, "imports"));
            }
        }
    }

    fn handle_import_from(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
    ) {
        let mut from_module = String::new();
        let mut imported_names: Vec<String> = Vec::new();

        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "dotted_name" && from_module.is_empty() {
                from_module = get_text(&child, source).to_string();
            } else if child.kind() == "import_from_specifier" || child.kind() == "dotted_name" {
                let mut sub_cursor = child.walk();
                for sub in child.children(&mut sub_cursor) {
                    if sub.kind() == "identifier" {
                        imported_names.push(get_text(&sub, source).to_string());
                    }
                }
            } else if child.kind() == "identifier" {
                imported_names.push(get_text(&child, source).to_string());
            }
        }

        for name in imported_names {
            let full_import = if !from_module.is_empty() {
                format!("{from_module}.{name}")
            } else {
                name.clone()
            };
            let qualified = format!("{module_name}.import.{full_import}");
            entities.push(ParsedEntity {
                kind: NodeType::Import,
                name,
                qualified_name: qualified.clone(),
                start_line: node.start_position().row,
                end_line: node.end_position().row,
                imports: vec![full_import.clone()],
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
                full_import,
                "imports",
            ));
        }
    }

    #[allow(clippy::ptr_arg)] // Signature mirrors walk() callback pattern.
    fn handle_assignment(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        // Only extract module-level assignments (depth 1 in scope stack).
        if scope_stack.len() > 1 {
            return;
        }

        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "assignment" {
                let mut sub_cursor = child.walk();
                for sub in child.children(&mut sub_cursor) {
                    if sub.kind() == "identifier" {
                        let name = get_text(&sub, source);
                        if name.len() < 2 {
                            continue;
                        }

                        let kind = if is_constant_name(name) {
                            NodeType::Constant
                        } else {
                            NodeType::Variable
                        };
                        let qualified = format!("{module_name}.{name}");
                        let raw = get_text(&child, source);
                        let signature = super::truncate_str(raw, super::RAW_TEXT_SMALL_LIMIT);

                        entities.push(ParsedEntity {
                            kind,
                            name: name.to_string(),
                            qualified_name: qualified.clone(),
                            start_line: node.start_position().row,
                            end_line: node.end_position().row,
                            signature: signature.to_string(),
                            raw_text: signature.to_string(),
                            parent_qualified_name: Some(module_name.to_string()),
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

                        break; // Only first identifier (left side of assignment).
                    }
                }
            }
        }
    }

    #[allow(clippy::ptr_arg)] // Signature mirrors walk() callback pattern.
    fn handle_type_alias(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        _edges: &mut Vec<ParsedEdge>,
        _scope_stack: &[String],
    ) {
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            if child.kind() == "identifier" {
                let name = get_text(&child, source);
                if !name.is_empty() {
                    let qualified = format!("{module_name}.{name}");
                    let raw = get_text(&node, source);
                    let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_SMALL_LIMIT);

                    entities.push(ParsedEntity {
                        kind: NodeType::TypeAlias,
                        name: name.to_string(),
                        qualified_name: qualified,
                        start_line: node.start_position().row,
                        end_line: node.end_position().row,
                        raw_text: raw_truncated.to_string(),
                        parent_qualified_name: Some(module_name.to_string()),
                        signature: String::new(),
                        docstring: String::new(),
                        bases: Vec::new(),
                        imports: Vec::new(),
                        cyclomatic_complexity: None,
                    });
                    break;
                }
            }
        }
    }
}

impl LanguageParser for PythonParser {
    fn parse(&self, source: &str, file_path: &str) -> (Vec<ParsedEntity>, Vec<ParsedEdge>) {
        let source_bytes = source.as_bytes();

        // We need a mutable reference to parse, but the trait takes &self.
        // tree-sitter's Parser requires &mut self for parse(), so we use
        // an unsafe workaround: create a fresh parser per call.
        // This is safe because PythonParser only holds the parser for config.
        let mut parser = Parser::new();
        let language = tree_sitter_python::LANGUAGE;
        parser.set_language(&language.into()).unwrap();

        let tree = match parser.parse(source_bytes, None) {
            Some(t) => t,
            None => return (Vec::new(), Vec::new()),
        };

        let mut entities = Vec::new();
        let mut edges = Vec::new();

        // Module-level file entity.
        let module_name = file_path
            .replace('/', ".")
            .strip_suffix(".py")
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

        // Walk the tree using a scope stack.
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
    fn parses_simple_python_class() {
        let parser = PythonParser::new();
        let source = r#"
class MyClass:
    """A test class."""

    def my_method(self, x: int) -> str:
        return str(x)
"#;
        let (entities, edges) = parser.parse(source, "test_module.py");

        // Should find: file, class, method
        let names: Vec<&str> = entities.iter().map(|e| e.name.as_str()).collect();
        assert!(names.contains(&"test_module.py"));
        assert!(names.contains(&"MyClass"));
        assert!(names.contains(&"my_method"));

        // Method should have correct kind.
        let method = entities.iter().find(|e| e.name == "my_method").unwrap();
        assert_eq!(method.kind, NodeType::Method);

        // Should have CONTAINS edges.
        assert!(edges.iter().any(|e| e.relationship == "contains"));
    }

    #[test]
    fn parses_imports() {
        let parser = PythonParser::new();
        let source = "from pathlib import Path\nimport os\n";
        let (entities, _edges) = parser.parse(source, "mod.py");

        let imports: Vec<&ParsedEntity> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Import)
            .collect();
        assert!(!imports.is_empty());
    }

    #[test]
    fn detects_constants() {
        let parser = PythonParser::new();
        let source = "MAX_RETRIES = 3\nmy_var = 42\n";
        let (entities, _) = parser.parse(source, "config.py");

        let constant = entities.iter().find(|e| e.name == "MAX_RETRIES");
        assert!(constant.is_some());
        assert_eq!(constant.unwrap().kind, NodeType::Constant);

        let variable = entities.iter().find(|e| e.name == "my_var");
        assert!(variable.is_some());
        assert_eq!(variable.unwrap().kind, NodeType::Variable);
    }

    #[test]
    fn cyclomatic_complexity_simple_function() {
        let parser = PythonParser::new();
        let source = "def greet(name: str) -> str:\n    return f'Hello, {name}'\n";
        let (entities, _) = parser.parse(source, "greet.py");

        let func = entities.iter().find(|e| e.name == "greet").unwrap();
        assert_eq!(func.kind, NodeType::Function);
        assert_eq!(func.cyclomatic_complexity, Some(1));
    }

    #[test]
    fn cyclomatic_complexity_branching() {
        let parser = PythonParser::new();
        let source = r#"
def process(items):
    for item in items:
        if item > 0:
            print(item)
"#;
        let (entities, _) = parser.parse(source, "process.py");

        let func = entities.iter().find(|e| e.name == "process").unwrap();
        assert_eq!(func.kind, NodeType::Function);
        // 1 base + for_statement + if_statement = 3
        assert_eq!(func.cyclomatic_complexity, Some(3));

        // Class entities should not have cyclomatic complexity.
        let parser2 = PythonParser::new();
        let source2 = "class Empty:\n    pass\n";
        let (entities2, _) = parser2.parse(source2, "empty.py");
        let cls = entities2.iter().find(|e| e.name == "Empty").unwrap();
        assert_eq!(cls.kind, NodeType::Class);
        assert_eq!(cls.cyclomatic_complexity, None);
    }

    #[test]
    fn parses_inheritance() {
        let parser = PythonParser::new();
        let source = "class Child(Base, Mixin):\n    pass\n";
        let (entities, edges) = parser.parse(source, "hierarchy.py");

        let child = entities.iter().find(|e| e.name == "Child").unwrap();
        assert_eq!(child.bases, vec!["Base", "Mixin"]);

        let inherits: Vec<&ParsedEdge> = edges
            .iter()
            .filter(|e| e.relationship == "inherits")
            .collect();
        assert_eq!(inherits.len(), 2);
    }

    #[test]
    fn calls_edges_simple() {
        let parser = PythonParser::new();
        let source = r#"
def caller():
    helper()
    result = process(data)
"#;
        let (_, edges) = parser.parse(source, "mod.py");
        let calls: Vec<&ParsedEdge> = edges.iter().filter(|e| e.relationship == "calls").collect();
        assert!(calls.iter().any(|e| e.target_qualified_name == "helper"));
        assert!(calls.iter().any(|e| e.target_qualified_name == "process"));
        assert!(calls
            .iter()
            .all(|e| e.source_qualified_name.contains("caller")));
    }

    #[test]
    fn calls_edges_method_call() {
        let parser = PythonParser::new();
        let source = r#"
class MyClass:
    def method(self):
        self.other_method()
        external_func()
"#;
        let (_, edges) = parser.parse(source, "cls.py");
        let calls: Vec<&ParsedEdge> = edges.iter().filter(|e| e.relationship == "calls").collect();
        assert!(calls
            .iter()
            .any(|e| e.target_qualified_name == "other_method"));
        assert!(calls
            .iter()
            .any(|e| e.target_qualified_name == "external_func"));
    }

    #[test]
    fn calls_edges_deduplicated() {
        let parser = PythonParser::new();
        let source = "def f():\n    foo()\n    foo()\n    foo()\n";
        let (_, edges) = parser.parse(source, "dedup.py");
        let foo_calls: Vec<&ParsedEdge> = edges
            .iter()
            .filter(|e| e.relationship == "calls" && e.target_qualified_name == "foo")
            .collect();
        assert_eq!(foo_calls.len(), 1, "Duplicate calls should be deduplicated");
    }

    #[test]
    fn calls_edges_skip_builtins() {
        let parser = PythonParser::new();
        let source = "def f():\n    print('hello')\n    len(data)\n    range(10)\n";
        let (_, edges) = parser.parse(source, "builtins.py");
        let calls: Vec<&ParsedEdge> = edges.iter().filter(|e| e.relationship == "calls").collect();
        assert!(
            calls.is_empty(),
            "Built-in calls should be filtered out, got: {:?}",
            calls
                .iter()
                .map(|e| &e.target_qualified_name)
                .collect::<Vec<_>>()
        );
    }

    #[test]
    fn calls_edges_chained_method() {
        let parser = PythonParser::new();
        let source = "def f():\n    a.b.deep_call()\n";
        let (_, edges) = parser.parse(source, "chain.py");
        let calls: Vec<&ParsedEdge> = edges.iter().filter(|e| e.relationship == "calls").collect();
        // Should extract the rightmost identifier from the chain.
        assert!(calls.iter().any(|e| e.target_qualified_name == "deep_call"));
    }
}
