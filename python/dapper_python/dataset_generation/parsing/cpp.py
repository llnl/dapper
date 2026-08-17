from __future__ import annotations

import re
import itertools

import tree_sitter_cpp as ts_cpp

from dataclasses import dataclass, field
from contextlib import suppress
from enum import Enum, auto

from tree_sitter import Language, Parser, Query, QueryCursor
from tree_sitter import Tree, Node

from collections.abc import Generator
from typing import ClassVar, Literal
from typing import Union, Optional
from typing_extensions import Self

from dapper_python.dataset_generation.utils.ast import ancestors
from dapper_python.dataset_generation.datatypes.exceptions import ParseError


# In the generate_<xyz>_db, we try to use the database SQL models when possible
# These classes are predominantly the same as those of the databases,
# But are kept separate as they don't belong only to a single database
# TODO: Any better way to avoid duplication?


@dataclass(frozen=True)
class FunctionSymbol:
    return_type: str
    symbol_name: str = field(hash=False)
    qualified_symbol_name: str
    param_list: list[str]
    modifiers: list[str] = field(default_factory=list)

    source_text: Optional[str] = field(default=None, hash=False, compare=False)

    @property
    def params(self) -> str:
        return ", ".join(self.param_list)

    @property
    def full_signature(self) -> str:
        if self.modifiers:
            return f"{self.return_type} {self.qualified_symbol_name}({self.params}) {' '.join(self.modifiers)}"
        else:
            return f"{self.return_type} {self.qualified_symbol_name}({self.params})"


@dataclass(frozen=True)
class PreprocessDefine:
    name: str
    value: str


@dataclass(frozen=True)
class StringLiteral:
    value: str


@dataclass
class CPPTreeParser:
    """Parses tree-sitter AST for C/C++ source code"""
    tree: Tree

    @classmethod
    def from_source(cls, contents: Union[bytes, bytearray], *, encoding: Literal["utf8", "utf16", "utf16le", "utf16be"] = "utf8") -> Self:
        ts_parser = Parser(cls.__CPP_LANG)
        tree = ts_parser.parse(contents, encoding=encoding)
        return cls(tree)

    def parse_functions(self) -> Generator[FunctionSymbol, None, None]:
        """Extracts all function definitions from the source code"""
        cursor = QueryCursor(self._FUNCTION_QUERY)

        for pattern, elements in cursor.matches(self.tree.root_node):
            fn_definition_node = elements["function_definition"][0]

            # If there are errors, we're unlikely to get a satisfactory result, so skip
            error_cursor = QueryCursor(self._ERROR_QUERY)
            if error_cursor.matches(fn_definition_node):
                continue

            # Tree sitter picks out too many edge cases to filter on preemptively
            # Try to parse the "happy path" and skip failures as it was likely misidentified as a function or malformed
            with suppress(ParseError, UnicodeDecodeError):
                function_parser = CPPFunctionParser(fn_definition_node)
                function = function_parser.parse_function()
                yield function

    def parse_preproc_defs(self) -> Generator[PreprocessDefine, None, None]:
        """Extracts all preprocessor "define" macros from the source code

        This only includes ones that have a value associated with them, defines that do not are ignored
            #define PI 3.14159          <- Will be included
            #define INCLUDE_GUARD_H     <- Will be ignored
        """
        cursor = QueryCursor(self._PREPROC_QUERY)

        for pattern, elements in cursor.matches(self.tree.root_node):
            with suppress(UnicodeDecodeError):
                yield PreprocessDefine(
                    name=elements["name"][0].text.decode().strip(),
                    value=elements["value"][0].text.decode().strip(),
                )

    def parse_string_literals(self) -> Generator[StringLiteral, None, None]:
        """Extracts all lines from source code that contain string literal(s)

        Extracts an entire expression if the string literal is part of a declaration or expression statement
        Individual strings aren't that useful: ["version", "built"]
        But as part of an expression could be: cout << "version" << ns.VERSION_NUMBER << "built" << ns.DATE;
        """
        cursor = QueryCursor(self._STRING_LITERAL_QUERY)

        for pattern, elements in cursor.matches(self.tree.root_node):
            # Exclude any strings from preproc includes
            if any(node.type == "preproc_include" for node in ancestors(elements["string"][0])):
                continue

            with suppress(StopIteration, UnicodeDecodeError):
                parent_node = next((
                    x for x in ancestors(elements["string"][0])
                    if x.type in ("declaration", "expression_statement")
                ))

                yield StringLiteral(
                    value=parent_node.text.decode().strip(),
                )

    __CPP_LANG: ClassVar = Language(ts_cpp.language())

    _ERROR_QUERY: ClassVar[Query] = Query(
        __CPP_LANG,
        "[(ERROR) (MISSING)] @error",
    )

    _FUNCTION_QUERY: ClassVar[Query] = Query(
        __CPP_LANG,
        """
            (
                function_definition
                    (type_qualifier)* @type_qualifier
                    type: (_) @type
                    declarator: (_) @declarator
            ) @function_definition
        """,
    )

    _PREPROC_QUERY: ClassVar[Query] = Query(
        __CPP_LANG,
        """
            (
                preproc_def
                    name: (_) @name
                    value: (_) @value
            )
        """,
    )

    _STRING_LITERAL_QUERY: ClassVar[Query] = Query(
        __CPP_LANG,
        """
            (string_literal) @string
        """,
    )


@dataclass
class CPPFunctionParser:
    """Specific sub-parser for tree-sitter AST for C/C++ function definitions"""
    fn_node: Node

    class _TypeSpec(Enum):
        FUNCTION = auto()
        PARAMETER = auto()
        AUTO = auto()

    def parse_function(self) -> FunctionSymbol:
        """Parses the function definition node and returns a FunctionSymbol"""
        try:
            # Function return type
            return_type = self._parse_type(self.fn_node)
            return_type = self.normalize_string(return_type)

            # Function name/qualified name
            fn_declarator_cursor = QueryCursor(self._FUNCTION_DECLARATOR_QUERY)
            fn_declarator_node = fn_declarator_cursor.matches(self.fn_node)[0][1]["function_declarator"][0]
            qualified_name = fn_declarator_node.child_by_field_name("declarator").text.decode()

            # Check if the function is inside any classes/namespaces that would further add onto the name
            qualified_ancestors = reversed([
                x for x in ancestors(fn_declarator_node)
                if x.type in ("namespace_definition", "class_specifier")
            ])
            addon_qualifiers = [
                self.normalize_string(name_node.text.decode())
                for ancestor in qualified_ancestors
                if (name_node := ancestor.child_by_field_name("name"))
            ]
            if addon_qualifiers:
                qualified_name = f"{'::'.join(addon_qualifiers)}::{qualified_name}"
            qualified_name = self.normalize_string(qualified_name)

            # Sometimes field_identifier instead of identifier when part of a class in a header file
            fn_identifier_cursor = QueryCursor(self._FUNCTION_IDENTIFIER_QUERY)
            fn_identifier_node = fn_identifier_cursor.matches(fn_declarator_node)[0][1]["identifier"][0]
            name = fn_identifier_node.text.decode()
            name = self.normalize_string(name)

            # Anything that modifies the function, such as "const" applied to a method
            modifiers = [
                self.normalize_string(x.text.decode())
                for x in fn_declarator_node.children
                if x.type == "type_qualifier"
            ]

            # Function parameters
            param_cursor = QueryCursor(self._FUNCTION_PARAMETER_QUERY)
            param_nodes = param_cursor.matches(self.fn_node)[0][1]["parameter_list"][0]
            param_nodes = [
                x for x in param_nodes.children
                if x.type in ("parameter_declaration", "optional_parameter_declaration")
            ]
            parameters = [
                self.normalize_string(self._parse_type(p_node))
                for p_node in param_nodes
            ]

            source_text = self._signature_text(self.fn_node).strip()
            source_text = self._MULTI_WHITESPACE_REGEX.sub(" ", source_text).strip()

            return FunctionSymbol(
                return_type=return_type,
                symbol_name=name,
                qualified_symbol_name=qualified_name,
                param_list=parameters,
                modifiers=modifiers,
                source_text=source_text,
            )
        except IndexError as e:
            raise ParseError(text=self.fn_node.text.decode().strip()) from e

    @classmethod
    def _parse_type(cls, node: Node) -> str:
        """Parses the given node to extract the type

        Supports:
            Function_definition node to extract function return type
            Parameter_declaration (or optional_parameter_declaration) to extract parameter type
        """
        if node.type == "function_definition":
            type_spec = cls._TypeSpec.FUNCTION
            identifier_query = Query(cls.__CPP_LANG, "(function_declarator) @declarator")
        elif node.type in ("parameter_declaration", "optional_parameter_declaration"):
            type_spec = cls._TypeSpec.PARAMETER
            identifier_query = Query(cls.__CPP_LANG, "(identifier) @declarator")
        else:
            raise TypeError(f"Unexpected node type: {node.type}")
        identifier_cursor = QueryCursor(identifier_query)

        # Extract the full return type by combining any qualifiers, type, and modifiers that are part of the declarator
        qualifiers = [
            x.text.decode().strip()
            for x in node.children
            if x.type == "type_qualifier"
        ]

        base_type = node.child_by_field_name("type").text.decode().strip()

        modifiers = []
        with suppress(IndexError):
            decl_node = identifier_cursor.matches(node)[0][1]["declarator"][0]
            modifier_chain = reversed(list(
                itertools.takewhile(lambda x: x != node, ancestors(decl_node)),
            ))

            for modifier in modifier_chain:
                literals = [x for x in modifier.children if x.type in ("*", "&", "&&")]
                for literal in literals:
                    modifiers.append(literal.text.decode())

                # Decay array type to pointer
                if modifier.type == "array_declarator":
                    modifiers.append("*")

                if qualifier := next((x for x in modifier.children if x.type == "type_qualifier"), None):
                    modifiers.append(qualifier.text.decode())

        final_type = f"{' '.join(qualifiers)} {base_type}{' '.join(modifiers)}"
        final_type = cls.normalize_string(final_type)

        return final_type

    @classmethod
    def normalize_string(cls, string: str) -> str:
        """Normalizes the given signature to a uniform format"""
        for pattern, replacement in cls._CLEANUP_REGEXES.items():
            string = pattern.sub(replacement, string)
        return string.strip()

    @classmethod
    def _signature_text(cls, fn_node: Node) -> str:
        """Extracts the signature of the given function definition while discarding the body

        We want to be able to compare the source that we extracted the information from
        But don't actually care about the body contents of the function
        """
        if fn_node.type == "declaration":
            return fn_node.text.decode().strip()
        elif fn_node.type != "function_definition":
            raise TypeError(f"Expected function_definition node, got {fn_node.type}")

        function_bytes = fn_node.text
        function_start = fn_node.start_byte

        body = fn_node.child_by_field_name("body")
        if body is None:
            return function_bytes.decode()

        body_start = body.start_byte - function_start
        body_end = body.end_byte - function_start

        # Concatenate everything excluding the body
        signature_bytes = function_bytes[:body_start] + function_bytes[body_end:]
        return signature_bytes.decode().strip()

    __CPP_LANG: ClassVar = Language(ts_cpp.language())

    _FUNCTION_DECLARATOR_QUERY: ClassVar[Query] = Query(
        __CPP_LANG,
        "(function_declarator) @function_declarator",
    )
    _FUNCTION_IDENTIFIER_QUERY: ClassVar[Query] = Query(
        __CPP_LANG,
        "[(identifier) (field_identifier) (operator_name)] @identifier",
    )
    _FUNCTION_PARAMETER_QUERY: ClassVar[Query] = Query(
        __CPP_LANG,
        "parameters: (parameter_list) @parameter_list",
    )

    # Used to substitute strings matching the regex (key) with the provided string (value)
    # For normalization and consistent formatting in dataset
    # Processed in the order they are listed
    _CLEANUP_REGEXES: ClassVar[dict[re.Pattern, str]] = {
        re.compile(r"\n"): " ",  # Remove newlines: "a\nb\nc" -> "a b c"
        re.compile(r"\s+(?=[*&])"): "",  # Ensure spacing of pointers and references: "int *" or "float &" -> "int*" and "float&"
        re.compile(r",(?=\S)"): ", ",  # Ensure consistent spacing of lists: "a, b, c, d" -> "a,b, c,d, e"
        re.compile(r"(?<=<)\s+|\s+(?=>)"): "",  # Ensure no empty spaces around templates: "pair< int, float >" -> "pair<int, float>"
        re.compile(r"\s+"): " ",  # Remove multiple sequential spaces: "a   b       c" -> "a b c"
    }
    _MULTI_WHITESPACE_REGEX: ClassVar[re.Pattern] = re.compile(r"\s+")
