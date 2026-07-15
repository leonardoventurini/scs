//! Elixir AST extraction via tree-sitter.
//!
//! Extracts modules, functions, macros, protocols, implementations,
//! imports (use/import/alias/require), and module attributes from
//! Elixir source files.
//!
//! Elixir's tree-sitter grammar represents everything as `call` nodes —
//! `defmodule`, `def`, `defp`, `use`, `import`, etc. are all calls.
//! We dispatch by inspecting the `identifier` child text.

use std::collections::HashSet;

use tree_sitter::{Node, Parser};

use scs_core::node_types::NodeType;

use super::{count_complexity, extract_calls, LanguageParser, ParsedEdge, ParsedEntity};

/// Elixir language keywords and constructs to exclude from call-graph edges.
/// In Elixir's tree-sitter grammar, control flow and module definitions are
/// all represented as `call` nodes — we filter them out here.
const ELIXIR_KEYWORD_CALLS: &[&str] = &[
    "if",
    "unless",
    "cond",
    "case",
    "with",
    "try",
    "rescue",
    "catch",
    "after",
    "for",
    "receive",
    "raise",
    "throw",
    "exit",
    "def",
    "defp",
    "defmodule",
    "defmacro",
    "defmacrop",
    "defguard",
    "defguardp",
    "defprotocol",
    "defimpl",
    "defstruct",
    "defexception",
    "defdelegate",
    "defoverridable",
    "use",
    "import",
    "alias",
    "require",
    "quote",
    "unquote",
    "unquote_splicing",
    "fn",
    "do",
    "end",
    "when",
    "and",
    "or",
    "not",
    "in",
    "is_atom",
    "is_binary",
    "is_bitstring",
    "is_boolean",
    "is_float",
    "is_function",
    "is_integer",
    "is_list",
    "is_map",
    "is_nil",
    "is_number",
    "is_pid",
    "is_port",
    "is_reference",
    "is_tuple",
    "put_in",
    "get_in",
    "update_in",
    "pop_in",
    "inspect",
    "to_string",
    "to_charlist",
    "to_atom",
    "spawn",
    "spawn_link",
    "spawn_monitor",
    "send",
    "self",
    "apply",
];

fn get_text<'a>(node: &Node, source: &'a [u8]) -> &'a str {
    std::str::from_utf8(&source[node.start_byte()..node.end_byte()]).unwrap_or("")
}

fn find_child_by_kind<'a>(node: &Node<'a>, kind: &str) -> Option<Node<'a>> {
    let mut cursor = node.walk();
    let result = node.children(&mut cursor).find(|c| c.kind() == kind);
    result
}

/// Extract the identifier text from a call node.
fn get_call_name(node: &Node, source: &[u8]) -> String {
    find_child_by_kind(node, "identifier")
        .map(|i| get_text(&i, source).to_string())
        .unwrap_or_default()
}

/// Extract the module name from defmodule's arguments.
///
/// The module name is an `alias` node (e.g. `MyApp.Accounts.User`)
/// inside the `arguments` child.
fn get_module_name(args: &Node, source: &[u8]) -> String {
    find_child_by_kind(args, "alias")
        .map(|a| get_text(&a, source).to_string())
        .unwrap_or_default()
}

/// Extract the function name from def/defp/defmacro arguments.
fn get_function_name(args: &Node, source: &[u8]) -> String {
    // Pattern: def name(args) → arguments → call → identifier
    if let Some(call) = find_child_by_kind(args, "call") {
        if let Some(ident) = find_child_by_kind(&call, "identifier") {
            return get_text(&ident, source).to_string();
        }
    }
    // Pattern: def name → arguments → identifier (zero arity)
    find_child_by_kind(args, "identifier")
        .map(|i| get_text(&i, source).to_string())
        .unwrap_or_default()
}

/// Extract parameter list text from function arguments.
fn get_function_params(args: &Node, source: &[u8]) -> String {
    if let Some(call) = find_child_by_kind(args, "call") {
        if let Some(inner_args) = find_child_by_kind(&call, "arguments") {
            return get_text(&inner_args, source).to_string();
        }
    }
    String::new()
}

/// Extract the module target from use/import/alias/require.
fn get_import_target(args: &Node, source: &[u8]) -> String {
    if let Some(alias) = find_child_by_kind(args, "alias") {
        return get_text(&alias, source).to_string();
    }
    find_child_by_kind(args, "identifier")
        .map(|i| get_text(&i, source).to_string())
        .unwrap_or_default()
}

/// Extract @moduledoc or @doc string from a module's do_block.
fn extract_moduledoc(do_block: &Node, source: &[u8]) -> String {
    let mut cursor = do_block.walk();
    for child in do_block.children(&mut cursor) {
        if child.kind() == "unary_operator" {
            if let Some(call) = find_child_by_kind(&child, "call") {
                if let Some(ident) = find_child_by_kind(&call, "identifier") {
                    let attr_name = get_text(&ident, source);
                    if attr_name == "moduledoc" || attr_name == "doc" {
                        if let Some(args) = find_child_by_kind(&call, "arguments") {
                            let mut args_cursor = args.walk();
                            for sub in args.children(&mut args_cursor) {
                                if sub.kind() == "string" || sub.kind() == "sigil" {
                                    let text = get_text(&sub, source);
                                    for delim in ["\"\"\"", "'''", "\"", "'"] {
                                        if text.starts_with(delim)
                                            && text.ends_with(delim)
                                            && text.len() >= delim.len() * 2
                                        {
                                            return text[delim.len()..text.len() - delim.len()]
                                                .trim()
                                                .to_string();
                                        }
                                    }
                                    return text.trim().to_string();
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    String::new()
}

/// Extract `@doc` string from the sibling preceding a def/defp/defmacro node.
///
/// In Elixir's AST, `@doc "..."` appears as a `unary_operator` node before
/// the function's `call` node. We walk backward to find it.
fn get_preceding_doc(node: &Node, source: &[u8]) -> String {
    let mut sibling = node.prev_named_sibling();
    while let Some(s) = sibling {
        if s.kind() == "unary_operator" {
            if let Some(call) = find_child_by_kind(&s, "call") {
                if let Some(ident) = find_child_by_kind(&call, "identifier") {
                    let attr_name = get_text(&ident, source);
                    if attr_name == "doc" {
                        if let Some(args) = find_child_by_kind(&call, "arguments") {
                            let mut args_cursor = args.walk();
                            for sub in args.children(&mut args_cursor) {
                                if sub.kind() == "string" || sub.kind() == "sigil" {
                                    let text = get_text(&sub, source);
                                    for delim in ["\"\"\"", "'''", "\"", "'"] {
                                        if text.starts_with(delim)
                                            && text.ends_with(delim)
                                            && text.len() >= delim.len() * 2
                                        {
                                            return text[delim.len()..text.len() - delim.len()]
                                                .trim()
                                                .to_string();
                                        }
                                    }
                                    return text.trim().to_string();
                                }
                            }
                        }
                    }
                }
            }
            // Stop at the first unary_operator — don't walk further.
            break;
        }
        // Skip comments but stop at anything else.
        if s.kind() != "comment" {
            break;
        }
        sibling = s.prev_named_sibling();
    }
    String::new()
}

/// Extract the type name from `@type name :: ...` arguments.
fn get_type_name(args: &Node, source: &[u8]) -> String {
    // Pattern: arguments → binary_operator (::) → left side
    // The left side is either an identifier or a call (for parameterized types).
    let mut cursor = args.walk();
    for child in args.children(&mut cursor) {
        if child.kind() == "binary_operator" {
            // The left side of `::` is the type name.
            let mut bc = child.walk();
            for sub in child.children(&mut bc) {
                if sub.kind() == "identifier" {
                    return get_text(&sub, source).to_string();
                }
                if sub.kind() == "call" {
                    if let Some(ident) = find_child_by_kind(&sub, "identifier") {
                        return get_text(&ident, source).to_string();
                    }
                }
            }
        }
        // Fallback: direct identifier (rare, e.g. `@type t`)
        if child.kind() == "identifier" {
            return get_text(&child, source).to_string();
        }
    }
    String::new()
}

/// Metadata attributes to skip — documentation and type spec annotations.
const SKIP_ATTRS: &[&str] = &[
    "moduledoc",
    "doc",
    "spec",
    "opaque",
    "callback",
    "macrocallback",
    "impl",
    "derive",
    "enforce_keys",
];

/// Parse Elixir source files using tree-sitter.
///
/// Handles modules, functions (def/defp), macros (defmacro/defmacrop),
/// protocols, implementations, import-like statements (use/import/alias/
/// require), and module attributes.
#[derive(Default)]
pub struct ElixirParser;

impl ElixirParser {
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
            if child.kind() == "call" {
                let keyword = get_call_name(&child, source);
                match keyword.as_str() {
                    "defmodule" => {
                        self.handle_module(
                            child,
                            source,
                            module_name,
                            entities,
                            edges,
                            scope_stack,
                        );
                    }
                    "defprotocol" => {
                        self.handle_protocol(
                            child,
                            source,
                            module_name,
                            entities,
                            edges,
                            scope_stack,
                        );
                    }
                    "defimpl" => {
                        self.handle_impl(child, source, module_name, entities, edges, scope_stack);
                    }
                    "def" | "defp" => {
                        self.handle_function(
                            child,
                            source,
                            module_name,
                            entities,
                            edges,
                            scope_stack,
                        );
                    }
                    "defmacro" | "defmacrop" => {
                        self.handle_macro(child, source, entities, edges, scope_stack);
                    }
                    "use" | "import" | "alias" | "require" => {
                        self.handle_import(child, source, module_name, entities, edges);
                    }
                    _ => {}
                }
            } else if child.kind() == "unary_operator" {
                self.handle_module_attribute(child, source, module_name, entities, scope_stack);
            } else if child.child_count() > 0 && child.kind() != "do_block" {
                self.walk(child, source, module_name, entities, edges, scope_stack);
            }
        }
    }

    fn handle_module(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let args = match find_child_by_kind(&node, "arguments") {
            Some(a) => a,
            None => return,
        };
        let name = get_module_name(&args, source);
        if name.is_empty() {
            return;
        }

        let qualified = name.clone();
        let do_block = find_child_by_kind(&node, "do_block");
        let docstring = do_block
            .as_ref()
            .map(|db| extract_moduledoc(db, source))
            .unwrap_or_default();

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
            signature: String::new(),
            bases: Vec::new(),
            imports: Vec::new(),
            cyclomatic_complexity: None,
        });

        edges.push(ParsedEdge::new(
            scope_stack.join("."),
            qualified.clone(),
            "contains",
        ));

        if let Some(do_block) = do_block {
            self.walk(do_block, source, module_name, entities, edges, &[qualified]);
        }
    }

    fn handle_protocol(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let args = match find_child_by_kind(&node, "arguments") {
            Some(a) => a,
            None => return,
        };
        let name = get_module_name(&args, source);
        if name.is_empty() {
            return;
        }

        let qualified = name.clone();
        let raw = get_text(&node, source);
        let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_LIMIT);

        entities.push(ParsedEntity {
            kind: NodeType::Class,
            name,
            qualified_name: qualified.clone(),
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

        edges.push(ParsedEdge::new(
            scope_stack.join("."),
            qualified.clone(),
            "contains",
        ));

        if let Some(do_block) = find_child_by_kind(&node, "do_block") {
            self.walk(do_block, source, module_name, entities, edges, &[qualified]);
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
        let args = match find_child_by_kind(&node, "arguments") {
            Some(a) => a,
            None => return,
        };
        let protocol_name = get_module_name(&args, source);
        if protocol_name.is_empty() {
            return;
        }

        // Extract the `for:` target type.
        let mut impl_for = String::new();
        if let Some(keywords) = find_child_by_kind(&args, "keywords") {
            let mut kw_cursor = keywords.walk();
            for kw in keywords.children(&mut kw_cursor) {
                if kw.kind() == "pair" {
                    if let Some(key) = find_child_by_kind(&kw, "keyword") {
                        let key_text = get_text(&key, source).trim_end_matches(':');
                        if key_text == "for" {
                            if let Some(alias) = find_child_by_kind(&kw, "alias") {
                                impl_for = get_text(&alias, source).to_string();
                            }
                        }
                    }
                }
            }
        }

        let qualified = if impl_for.is_empty() {
            protocol_name.clone()
        } else {
            format!("{protocol_name}.{impl_for}")
        };

        let raw = get_text(&node, source);
        let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_LIMIT);

        entities.push(ParsedEntity {
            kind: NodeType::Class,
            name: qualified.clone(),
            qualified_name: qualified.clone(),
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

        if !impl_for.is_empty() && !protocol_name.is_empty() {
            edges.push(ParsedEdge::new(impl_for, protocol_name, "implements"));
        }

        if let Some(do_block) = find_child_by_kind(&node, "do_block") {
            self.walk(do_block, source, module_name, entities, edges, &[qualified]);
        }
    }

    fn handle_function(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let args = match find_child_by_kind(&node, "arguments") {
            Some(a) => a,
            None => return,
        };
        let name = get_function_name(&args, source);
        if name.is_empty() {
            return;
        }

        let qualified = format!("{}.{name}", scope_stack.join("."));
        let parent_scope = scope_stack.join(".");
        let is_method = parent_scope != module_name;
        let kind = if is_method {
            NodeType::Method
        } else {
            NodeType::Function
        };

        let params = get_function_params(&args, source);
        let do_block = find_child_by_kind(&node, "do_block");
        let raw = get_text(&node, source);
        let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_LIMIT);
        let complexity = count_complexity(&node, source, "elixir");

        entities.push(ParsedEntity {
            kind,
            name,
            qualified_name: qualified.clone(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            signature: params,
            raw_text: raw_truncated.to_string(),
            parent_qualified_name: Some(scope_stack.join(".")),
            docstring: get_preceding_doc(&node, source),
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
        if let Some(body) = do_block {
            let blocklist: HashSet<&str> = ELIXIR_KEYWORD_CALLS.iter().copied().collect();
            let callees = extract_calls(&body, source, "elixir", &blocklist);
            for callee in callees {
                edges.push(ParsedEdge::new(qualified.clone(), callee, "calls"));
            }
        }
    }

    fn handle_macro(
        &self,
        node: Node,
        source: &[u8],
        entities: &mut Vec<ParsedEntity>,
        edges: &mut Vec<ParsedEdge>,
        scope_stack: &[String],
    ) {
        let args = match find_child_by_kind(&node, "arguments") {
            Some(a) => a,
            None => return,
        };
        let name = get_function_name(&args, source);
        if name.is_empty() {
            return;
        }

        let qualified = format!("{}.{name}", scope_stack.join("."));
        let params = get_function_params(&args, source);
        let raw = get_text(&node, source);
        let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_LIMIT);
        let complexity = count_complexity(&node, source, "elixir");

        entities.push(ParsedEntity {
            kind: NodeType::Function,
            name,
            qualified_name: qualified.clone(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            signature: params,
            raw_text: raw_truncated.to_string(),
            parent_qualified_name: Some(scope_stack.join(".")),
            docstring: get_preceding_doc(&node, source),
            bases: Vec::new(),
            imports: Vec::new(),
            cyclomatic_complexity: Some(complexity),
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
        let args = match find_child_by_kind(&node, "arguments") {
            Some(a) => a,
            None => return,
        };
        let target = get_import_target(&args, source);
        if target.is_empty() {
            return;
        }

        let qualified = format!("{module_name}.import.{target}");
        entities.push(ParsedEntity {
            kind: NodeType::Import,
            name: target.clone(),
            qualified_name: qualified.clone(),
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            imports: vec![target.clone()],
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
        edges.push(ParsedEdge::new(module_name.to_string(), target, "imports"));
    }

    fn handle_module_attribute(
        &self,
        node: Node,
        source: &[u8],
        module_name: &str,
        entities: &mut Vec<ParsedEntity>,
        scope_stack: &[String],
    ) {
        let call = match find_child_by_kind(&node, "call") {
            Some(c) => c,
            None => return,
        };
        let ident = match find_child_by_kind(&call, "identifier") {
            Some(i) => i,
            None => return,
        };

        let attr_name = get_text(&ident, source);

        // Extract @type/@typep as TypeAlias entities.
        if attr_name == "type" || attr_name == "typep" {
            let parent_scope = scope_stack.join(".");
            if parent_scope == module_name {
                return;
            }
            if let Some(args) = find_child_by_kind(&call, "arguments") {
                let type_name = get_type_name(&args, source);
                if !type_name.is_empty() {
                    let qualified = format!("{parent_scope}.{type_name}");
                    let raw = get_text(&node, source);
                    let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_SMALL_LIMIT);
                    entities.push(ParsedEntity {
                        kind: NodeType::TypeAlias,
                        name: type_name,
                        qualified_name: qualified,
                        start_line: node.start_position().row,
                        end_line: node.end_position().row,
                        raw_text: raw_truncated.to_string(),
                        parent_qualified_name: Some(parent_scope),
                        signature: String::new(),
                        docstring: String::new(),
                        bases: Vec::new(),
                        imports: Vec::new(),
                        cyclomatic_complexity: None,
                    });
                }
            }
            return;
        }

        if SKIP_ATTRS.contains(&attr_name) {
            return;
        }

        // Only extract attributes inside a module scope.
        let parent_scope = scope_stack.join(".");
        if parent_scope == module_name {
            return;
        }

        let qualified = format!("{parent_scope}.@{attr_name}");
        let raw = get_text(&node, source);
        let raw_truncated = super::truncate_str(raw, super::RAW_TEXT_SMALL_LIMIT);

        entities.push(ParsedEntity {
            kind: NodeType::Constant,
            name: format!("@{attr_name}"),
            qualified_name: qualified,
            start_line: node.start_position().row,
            end_line: node.end_position().row,
            raw_text: raw_truncated.to_string(),
            parent_qualified_name: Some(parent_scope),
            signature: String::new(),
            docstring: String::new(),
            bases: Vec::new(),
            imports: Vec::new(),
            cyclomatic_complexity: None,
        });
    }
}

impl LanguageParser for ElixirParser {
    fn parse(&self, source: &str, file_path: &str) -> (Vec<ParsedEntity>, Vec<ParsedEdge>) {
        let source_bytes = source.as_bytes();

        let mut parser = Parser::new();
        let language = tree_sitter_elixir::LANGUAGE;
        parser.set_language(&language.into()).unwrap();

        let tree = match parser.parse(source_bytes, None) {
            Some(t) => t,
            None => return (Vec::new(), Vec::new()),
        };

        let mut entities = Vec::new();
        let mut edges = Vec::new();

        // Strip both .ex and .exs extensions.
        let dotted = file_path.replace('/', ".");
        let module_name = dotted
            .strip_suffix(".exs")
            .or_else(|| dotted.strip_suffix(".ex"))
            .unwrap_or(&dotted)
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
    fn parses_module() {
        let parser = ElixirParser::new();
        let source =
            "defmodule MyApp.Accounts.User do\n  def create(attrs) do\n    attrs\n  end\nend\n";
        let (entities, _) = parser.parse(source, "user.ex");
        let modules: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Class)
            .collect();
        assert!(modules.iter().any(|m| m.name == "MyApp.Accounts.User"));
    }

    #[test]
    fn parses_function() {
        let parser = ElixirParser::new();
        let source = "defmodule MyApp.Users do\n  def create(attrs) do\n    attrs\n  end\n\n  defp validate(data) do\n    data\n  end\nend\n";
        let (entities, _) = parser.parse(source, "users.ex");
        let methods: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Method)
            .collect();
        let names: std::collections::HashSet<_> = methods.iter().map(|m| m.name.as_str()).collect();
        assert!(names.contains("create"));
        assert!(names.contains("validate"));
    }

    #[test]
    fn cyclomatic_complexity() {
        let parser = ElixirParser::new();

        // Simple function — no branches → complexity 1.
        let source_simple =
            "defmodule Greeter do\n  def greet(name) do\n    \"Hello, #{name}\"\n  end\nend\n";
        let (entities, _) = parser.parse(source_simple, "greeter.ex");
        let func = entities.iter().find(|e| e.name == "greet").unwrap();
        assert_eq!(func.kind, NodeType::Method);
        assert_eq!(func.cyclomatic_complexity, Some(1));

        // Branching function — if + unless → complexity 3.
        let source_branch = "defmodule Processor do\n  def process(items) do\n    if length(items) > 0 do\n      unless hd(items) == :skip do\n        :ok\n      end\n    end\n  end\nend\n";
        let (entities2, _) = parser.parse(source_branch, "processor.ex");
        let func2 = entities2.iter().find(|e| e.name == "process").unwrap();
        assert_eq!(func2.kind, NodeType::Method);
        // 1 base + if + unless = 3
        assert_eq!(func2.cyclomatic_complexity, Some(3));

        // Module (class-like) should not have cyclomatic complexity.
        let module = entities2.iter().find(|e| e.name == "Processor").unwrap();
        assert_eq!(module.kind, NodeType::Class);
        assert_eq!(module.cyclomatic_complexity, None);
    }

    #[test]
    fn parses_imports() {
        let parser = ElixirParser::new();
        let source = "defmodule MyApp.Users do\n  use Ecto.Schema\n  import Ecto.Changeset\n  alias MyApp.Repo\nend\n";
        let (entities, _) = parser.parse(source, "users.ex");
        let imports: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Import)
            .collect();
        let names: std::collections::HashSet<_> = imports.iter().map(|i| i.name.as_str()).collect();
        assert!(names.contains("Ecto.Schema"));
        assert!(names.contains("Ecto.Changeset"));
        assert!(names.contains("MyApp.Repo"));
    }

    #[test]
    fn parses_macro() {
        let parser = ElixirParser::new();
        let source = "defmodule MyMacros do\n  defmacro my_macro(expr) do\n    quote do\n      unquote(expr)\n    end\n  end\nend\n";
        let (entities, _) = parser.parse(source, "macros.ex");
        assert!(entities
            .iter()
            .any(|e| e.name == "my_macro" && e.kind == NodeType::Function));
    }

    #[test]
    fn contains_edges() {
        let parser = ElixirParser::new();
        let source = "defmodule MyApp do\n  def hello do\n    :world\n  end\nend\n";
        let (_, edges) = parser.parse(source, "app.ex");
        let contains: Vec<_> = edges
            .iter()
            .filter(|e| e.relationship == "contains")
            .collect();
        assert!(contains.len() >= 2);
    }

    #[test]
    fn file_entity_created() {
        let parser = ElixirParser::new();
        let (entities, _) = parser.parse("# empty module", "lib/app.ex");
        let files: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::File)
            .collect();
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].name, "lib/app.ex");
    }

    #[test]
    fn module_attribute() {
        let parser = ElixirParser::new();
        let source = "defmodule MyWorker do\n  @behaviour GenServer\n  @max_retries 5\nend\n";
        let (entities, _) = parser.parse(source, "worker.ex");
        let constants: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Constant)
            .collect();
        let names: std::collections::HashSet<_> =
            constants.iter().map(|c| c.name.as_str()).collect();
        assert!(names.contains("@behaviour"));
        assert!(names.contains("@max_retries"));
    }

    #[test]
    fn exs_extension() {
        let parser = ElixirParser::new();
        let (entities, _) = parser.parse("# test", "test/my_test.exs");
        let files: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::File)
            .collect();
        assert_eq!(files[0].name, "test/my_test.exs");
    }

    #[test]
    fn import_has_contains_edge() {
        let parser = ElixirParser::new();
        let source = "defmodule MyApp do\n  use Ecto.Schema\nend\n";
        let (entities, edges) = parser.parse(source, "app.ex");
        let imports: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::Import)
            .collect();
        assert!(!imports.is_empty());
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
    fn doc_attribute_extracted_for_function() {
        let parser = ElixirParser::new();
        let source = "defmodule Mod do\n  @doc \"Fetches user\"\n  def fetch(id), do: id\nend\n";
        let (entities, _) = parser.parse(source, "mod.ex");
        let func = entities.iter().find(|e| e.name == "fetch").unwrap();
        assert!(
            func.docstring.contains("Fetches user"),
            "Expected docstring to contain 'Fetches user', got: {:?}",
            func.docstring
        );
    }

    #[test]
    fn type_attribute_extracted_as_type_alias() {
        let parser = ElixirParser::new();
        let source = "defmodule Mod do\n  @type name :: String.t()\nend\n";
        let (entities, _) = parser.parse(source, "mod.ex");
        let type_aliases: Vec<_> = entities
            .iter()
            .filter(|e| e.kind == NodeType::TypeAlias)
            .collect();
        assert!(
            type_aliases.iter().any(|t| t.name == "name"),
            "Expected TypeAlias named 'name', got: {:?}",
            type_aliases.iter().map(|t| &t.name).collect::<Vec<_>>()
        );
    }

    #[test]
    fn calls_edges_simple() {
        let parser = ElixirParser::new();
        let source = r#"
defmodule MyMod do
  def caller do
    helper()
    process(data)
  end
end
"#;
        let (_, edges) = parser.parse(source, "lib/my_mod.ex");
        let calls: Vec<&ParsedEdge> = edges.iter().filter(|e| e.relationship == "calls").collect();
        assert!(calls.iter().any(|e| e.target_qualified_name == "helper"));
        assert!(calls.iter().any(|e| e.target_qualified_name == "process"));
    }

    #[test]
    fn calls_edges_skip_keywords() {
        let parser = ElixirParser::new();
        let source = r#"
defmodule MyMod do
  def f do
    if true, do: nil
    case x do
      _ -> nil
    end
  end
end
"#;
        let (_, edges) = parser.parse(source, "lib/my_mod.ex");
        let calls: Vec<&ParsedEdge> = edges.iter().filter(|e| e.relationship == "calls").collect();
        assert!(
            calls.is_empty(),
            "Keyword calls should be filtered, got: {:?}",
            calls
                .iter()
                .map(|e| &e.target_qualified_name)
                .collect::<Vec<_>>()
        );
    }
}
