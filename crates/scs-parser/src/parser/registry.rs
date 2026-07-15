//! File extension to parser mapping.
//!
//! Maps source file extensions to their corresponding tree-sitter parser
//! implementations. Parsers are instantiated lazily on first use to avoid
//! loading tree-sitter grammars until they're actually needed.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

use super::bash::BashParser;
use super::css::CssParser;
use super::elixir::ElixirParser;
use super::python::PythonParser;
use super::rust::RustParser;
use super::swift::SwiftParser;
use super::typescript::TypeScriptParser;
use super::LanguageParser;

/// Extension → language mapping — single source of truth for all supported file types.
fn extension_map() -> &'static HashMap<&'static str, &'static str> {
    static MAP: OnceLock<HashMap<&str, &str>> = OnceLock::new();
    MAP.get_or_init(|| {
        let mut m = HashMap::new();
        m.insert(".py", "python");
        m.insert(".ts", "typescript");
        m.insert(".tsx", "tsx");
        m.insert(".js", "javascript");
        m.insert(".jsx", "tsx"); // JSX uses the TSX grammar.
        m.insert(".rs", "rust");
        m.insert(".swift", "swift");
        m.insert(".ex", "elixir");
        m.insert(".exs", "elixir");
        m.insert(".sh", "bash");
        m.insert(".bash", "bash");
        m.insert(".css", "css");
        m
    })
}

/// Lazily instantiated parser cache — one parser per language.
static PARSER_CACHE: OnceLock<Mutex<HashMap<String, Arc<dyn LanguageParser>>>> = OnceLock::new();

fn cache() -> &'static Mutex<HashMap<String, Arc<dyn LanguageParser>>> {
    PARSER_CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

/// Get a parser for the given file extension.
///
/// Returns `None` if the extension is not supported. Parsers are cached
/// after first instantiation to avoid repeated grammar loading.
pub fn get_parser(extension: &str) -> Option<Arc<dyn LanguageParser>> {
    let language = extension_map().get(extension)?;
    let mut parsers = cache().lock().unwrap();

    if let Some(parser) = parsers.get(*language) {
        return Some(Arc::clone(parser));
    }

    let parser: Arc<dyn LanguageParser> = match *language {
        "python" => Arc::new(PythonParser::new()),
        "typescript" => Arc::new(TypeScriptParser::new("typescript")),
        "tsx" => Arc::new(TypeScriptParser::new("tsx")),
        "javascript" => Arc::new(TypeScriptParser::new("typescript")), // TS superset of JS
        "rust" => Arc::new(RustParser::new()),
        "swift" => Arc::new(SwiftParser::new()),
        "elixir" => Arc::new(ElixirParser::new()),
        "bash" => Arc::new(BashParser::new()),
        "css" => Arc::new(CssParser::new()),
        _ => return None,
    };

    parsers.insert(language.to_string(), Arc::clone(&parser));
    Some(parser)
}

/// Get the set of file extensions we can parse.
pub fn supported_extensions() -> Vec<&'static str> {
    extension_map().keys().copied().collect()
}

/// Extension → language name for FileEntry.
pub fn extension_to_language(ext: &str) -> &'static str {
    static LANG_MAP: OnceLock<HashMap<&str, &str>> = OnceLock::new();
    let map = LANG_MAP.get_or_init(|| {
        let mut m = HashMap::new();
        m.insert(".py", "python");
        m.insert(".ts", "typescript");
        m.insert(".tsx", "tsx");
        m.insert(".js", "javascript");
        m.insert(".jsx", "jsx");
        m.insert(".rs", "rust");
        m.insert(".swift", "swift");
        m.insert(".ex", "elixir");
        m.insert(".exs", "elixir");
        m.insert(".sh", "bash");
        m.insert(".bash", "bash");
        m.insert(".css", "css");
        m
    });
    map.get(ext).copied().unwrap_or("unknown")
}
