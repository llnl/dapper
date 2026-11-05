import pytest

from typing import Optional

from dapper_python.dataset_generation.parsing.cpp import CPPTreeParser
from dapper_python.dataset_generation.parsing.cpp import FunctionSymbol


@pytest.mark.parametrize(
    "test_input,expected,expected_signature",
    [
        # Simple int return type
        pytest.param(
            "int simple_int(int x){}",
            FunctionSymbol(
                return_type="int",
                symbol_name="simple_int",
                qualified_symbol_name="simple_int",
                param_list=["int"],
            ),
            "int simple_int(int)",
        ),
        # Pointer return type, namespaced
        pytest.param(
            "double** math::submodule::get_buffer(size_t n){}",
            FunctionSymbol(
                return_type="double**",
                symbol_name="get_buffer",
                qualified_symbol_name="math::submodule::get_buffer",
                param_list=["size_t"],
            ),
            "double** math::submodule::get_buffer(size_t)",
        ),
        # Reference return type, class scope
        pytest.param(
            "std::string& StringUtil::get_ref(std::string& s){}",
            FunctionSymbol(
                return_type="std::string&",
                symbol_name="get_ref",
                qualified_symbol_name="StringUtil::get_ref",
                param_list=["std::string&"],
            ),
            "std::string& StringUtil::get_ref(std::string&)",
        ),
        # Rvalue reference return type, template parameter
        pytest.param(
            "std::vector<int>&& VecUtil::move_vector(std::vector<int>&& v){}",
            FunctionSymbol(
                return_type="std::vector<int>&&",
                symbol_name="move_vector",
                qualified_symbol_name="VecUtil::move_vector",
                param_list=["std::vector<int>&&"],
            ),
            "std::vector<int>&& VecUtil::move_vector(std::vector<int>&&)",
        ),
        # Const reference return type, nested namespace
        pytest.param(
            "const std::map<std::string, std::vector<int>>& ns1::ns2::get_map(){}",
            FunctionSymbol(
                return_type="const std::map<std::string, std::vector<int>>&",
                symbol_name="get_map",
                qualified_symbol_name="ns1::ns2::get_map",
                param_list=[],
            ),
            "const std::map<std::string, std::vector<int>>& ns1::ns2::get_map()",
        ),
        # Function pointer return type, pointer param (SKIPPED)
        pytest.param(
            "void (*CallbackUtil::get_callback())(int){}",
            FunctionSymbol(
                return_type="void (*)(int)",
                symbol_name="get_callback",
                qualified_symbol_name="CallbackUtil::get_callback",
                param_list=[],
            ),
            "void (*)(int)",
            marks=pytest.mark.skip(reason="Not currently supported"),
        ),
        # Returning vector of pointers, pointer param
        pytest.param(
            "std::vector<int*> PtrVec::make_vector(int* arr[], size_t n){}",
            FunctionSymbol(
                return_type="std::vector<int*>",
                symbol_name="make_vector",
                qualified_symbol_name="PtrVec::make_vector",
                param_list=["int**", "size_t"],
            ),
            "std::vector<int*> PtrVec::make_vector(int**, size_t)",
        ),
        # Template type parameter, reference param
        pytest.param(
            "std::vector<std::map<std::string,\ndouble>> DataUtil::process(const std::vector<std::map<std::string,\ndouble >>& data){}",
            FunctionSymbol(
                return_type="std::vector<std::map<std::string, double>>",
                symbol_name="process",
                qualified_symbol_name="DataUtil::process",
                param_list=["const std::vector<std::map<std::string, double>>&"],
            ),
            "std::vector<std::map<std::string, double>> DataUtil::process(const std::vector<std::map<std::string, double>>&)",
        ),
        # Pointer to vector param, returning int
        pytest.param(
            "int VecStat::sum(const std::vector< int >* vec){}",
            FunctionSymbol(
                return_type="int",
                symbol_name="sum",
                qualified_symbol_name="VecStat::sum",
                param_list=["const std::vector<int>*"],
            ),
            "int VecStat::sum(const std::vector<int>*)",
        ),
        # Returning const pointer, pointer param
        pytest.param(
            "const char* StrUtil::find_char(const char* str, char c){}",
            FunctionSymbol(
                return_type="const char*",
                symbol_name="find_char",
                qualified_symbol_name="StrUtil::find_char",
                param_list=["const char*", "char"],
            ),
            "const char* StrUtil::find_char(const char*, char)",
        ),
        # Returning volatile pointer, volatile pointer param
        pytest.param(
            "volatile int* VolUtil::get_volatile(volatile int* p){}",
            FunctionSymbol(
                return_type="volatile int*",
                symbol_name="get_volatile",
                qualified_symbol_name="VolUtil::get_volatile",
                param_list=["volatile int*"],
            ),
            "volatile int* VolUtil::get_volatile(volatile int*)",
        ),
        # Returning restrict pointer, restrict pointer param (C only)
        pytest.param(
            "int* restrict RestrictUtil::restrict_op(int* restrict p){}",
            FunctionSymbol(
                return_type="int* restrict",
                symbol_name="restrict_op",
                qualified_symbol_name="RestrictUtil::restrict_op",
                param_list=["int* restrict"],
            ),
            "int* restrict RestrictUtil::restrict_op(int* restrict)",
        ),
        # Returning std::vector<std::string>, param is std::vector<int>
        pytest.param(
            "std::vector<std::string> VecUtil::int_to_string(const std::vector<int>& v){}",
            FunctionSymbol(
                return_type="std::vector<std::string>",
                symbol_name="int_to_string",
                qualified_symbol_name="VecUtil::int_to_string",
                param_list=["const std::vector<int>&"],
            ),
            "std::vector<std::string> VecUtil::int_to_string(const std::vector<int>&)",
        ),
        # Returning std::pair<int, std::string>, param is std::map<int, std::string>
        pytest.param(
            "std::pair<int, std::string> MapUtil::find_pair(const std::map<int, std::string>& m, int key){}",
            FunctionSymbol(
                return_type="std::pair<int, std::string>",
                symbol_name="find_pair",
                qualified_symbol_name="MapUtil::find_pair",
                param_list=["const std::map<int, std::string>&", "int"],
            ),
            "std::pair<int, std::string> MapUtil::find_pair(const std::map<int, std::string>&, int)",
        ),
        # Argument with no identifier
        pytest.param(
            "void no_ident(int, bool){}",
            FunctionSymbol(
                return_type="void",
                symbol_name="no_ident",
                qualified_symbol_name="no_ident",
                param_list=["int", "bool"],
            ),
            "void no_ident(int, bool)",
        ),
        # Const modifier on function
        pytest.param(
            "const std::string& error() const throw(){}",
            FunctionSymbol(
                return_type="const std::string&",
                symbol_name="error",
                qualified_symbol_name="error",
                param_list=[],
                modifiers=["const"],
            ),
            "const std::string& error() const",
        ),
        # Operator []
        pytest.param(
            "const int& operator [](int index){}",
            FunctionSymbol(
                return_type="const int&",
                symbol_name="operator []",
                qualified_symbol_name="operator []",
                param_list=["int"],
            ),
            "const int& operator [](int)",
        ),
        # Operator ->
        pytest.param(
            "myobj* operator ->(){}",
            FunctionSymbol(
                return_type="myobj*",
                symbol_name="operator ->",
                qualified_symbol_name="operator ->",
                param_list=[],
            ),
            "myobj* operator ->()",
        ),
        # Namespace + Class
        pytest.param(
          "namespace foo{class bar{int baz(){return 1;}};}",
            FunctionSymbol(
                return_type="int",
                symbol_name="baz",
                qualified_symbol_name="foo::bar::baz",
                param_list=[],
            ),
            "int foo::bar::baz()",
        ),
    ],
)
def test_function_parsing(test_input: str, expected: FunctionSymbol, expected_signature: Optional[str]):
    tree = CPPTreeParser.from_source(test_input.encode())
    actual = list(tree.parse_functions())
    assert len(actual) == 1
    assert actual[0] == expected

    if expected_signature is not None:
        assert actual[0].full_signature == expected_signature
